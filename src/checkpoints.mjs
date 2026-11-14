import { execFile, spawn } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { createReadStream, createWriteStream } from 'node:fs';
import { copyFile, lstat, mkdir, readFile, readdir, rename, rm, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { Transform } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { setTimeout as delay } from 'node:timers/promises';
import { promisify } from 'node:util';
import { piSessionFile, runId } from './history.mjs';

const exec = promisify(execFile);
const diskBudget = 2 * 1024 ** 3;
const compressor = 'zstd -q -1 -T1';
const capacity = new Error('Checkpoint disk budget reached.');
const snapshotName = /^[a-f0-9-]{36}$/;

export function offlinePlayerId(username) {
  const id = createHash('md5').update(`OfflinePlayer:${username}`).digest();
  id[6] = (id[6] & 0x0f) | 0x30;
  id[8] = (id[8] & 0x3f) | 0x80;
  return id.toString('hex').replace(/^(........)(....)(....)(....)/, '$1-$2-$3-$4-');
}

async function scan(directory, visit, { skipLock = false, prefix = '' } = {}) {
  const entries = await readdir(directory, { withFileTypes: true });
  entries.sort((a, b) => a.name.localeCompare(b.name));
  for (const entry of entries) {
    if (skipLock && !prefix && entry.name === 'session.lock') continue;
    const path = join(directory, entry.name);
    const name = join(prefix, entry.name);
    const info = await lstat(path, { bigint: true });
    if (info.isDirectory()) {
      await scan(path, visit, { skipLock, prefix: name });
    } else {
      if (skipLock && !info.isFile()) throw new Error(`Unexpected entry in Minecraft world: ${name}`);
      visit(name, info);
    }
  }
}

async function diskUsage(directory) {
  let bytes = 0;
  await scan(directory, (_name, info) => { bytes += Number(info.size); }).catch(error => {
    if (error.code !== 'ENOENT') throw error;
  });
  return bytes;
}

async function fingerprint(world) {
  const hash = createHash('sha256');
  await scan(world, (name, info) => {
    hash.update(JSON.stringify([name, fileVersion(info)]));
  }, { skipLock: true });
  return hash.digest('hex');
}

function fileVersion(info) {
  return `${info.ino}:${info.size}:${info.mtimeNs}:${info.ctimeNs}`;
}

async function saveMarkers(world, player) {
  const names = ['level.dat', `playerdata/${player}.dat`];
  const [level, state] = await Promise.all(names.map(name => lstat(join(world, name), { bigint: true })))
    .catch(error => {
      if (error.code !== 'ENOENT') throw error;
      return [];
    });
  if (!level?.isFile() || !state?.isFile() || Math.abs(Number(level.mtimeMs - state.mtimeMs)) > 2000) return null;
  return { level: fileVersion(level), player: fileVersion(state), at: Number(state.mtimeNs) / 1e6 };
}

async function boundedCopy(source, destination, maxBytes, signal) {
  let bytes = 0;
  const limit = new Transform({
    transform(chunk, _encoding, callback) {
      bytes += chunk.length;
      if (bytes > maxBytes) return callback(capacity);
      callback(null, chunk);
    },
  });
  await pipeline(source, limit, createWriteStream(destination, { mode: 0o600, flags: 'wx' }), { signal });
  return bytes;
}

async function archive(world, destination, limit, signal) {
  const child = spawn('nice', ['-n', '10', 'ionice', '-c', '3', 'tar', '-I', compressor,
    '--exclude=./session.lock', '-cf', '-', '-C', world, '.'], { signal, stdio: ['ignore', 'pipe', 'pipe'] });
  let failure;
  let stderr = '';
  child.on('error', error => { failure = error; });
  child.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-2048); });
  const done = new Promise(resolve => child.once('close', resolve));
  try {
    const bytes = await boundedCopy(child.stdout, destination, limit, signal);
    const code = await done;
    if (failure) throw failure;
    if (code === 1) return null;
    if (code !== 0) throw new Error(stderr.trim() || `World archive exited (${code}).`);
    return bytes;
  } finally {
    if (child.exitCode === null) child.kill();
    await done;
  }
}

async function copyContext(run, destination, at, limit, signal) {
  const file = await piSessionFile(run);
  if (!file) return null;
  const path = join(run, file);
  const source = createReadStream(path, { signal });
  const lines = createInterface({ input: source, crlfDelay: Infinity });
  let bytes = 0;
  let boundary = 0;
  let leaf;
  const pending = new Set();
  try {
    for await (const line of lines) {
      let entry;
      try { entry = JSON.parse(line); } catch { break; }
      if (!(Date.parse(entry.timestamp) <= at)) break;
      bytes += Buffer.byteLength(line) + 1;
      if (entry.type === 'session') continue;
      if (entry.type === 'message') {
        const { role, content, toolCallId } = entry.message;
        if (role === 'assistant' && Array.isArray(content)) {
          for (const block of content) if (block.type === 'toolCall') pending.add(block.id);
        }
        if (role === 'toolResult') pending.delete(toolCallId);
        if (pending.size || !['user', 'toolResult', 'assistant'].includes(role)) continue;
        leaf = entry;
        boundary = bytes;
      }
    }
  } finally {
    lines.close();
    source.destroy();
  }
  if (!leaf) return null;
  await boundedCopy(createReadStream(path, { end: boundary - 1 }), destination, limit, signal);
  return { file: 'pi.jsonl', leaf: leaf.id, timestamp: leaf.timestamp, ageMs: at - Date.parse(leaf.timestamp), bytes: boundary };
}

export async function recoverCheckpoints(root, latestRun) {
  const store = join(root, 'checkpoints');
  const directories = async path => (await readdir(path, { withFileTypes: true }).catch(error => {
    if (error.code !== 'ENOENT') throw error;
    return [];
  })).filter(entry => entry.isDirectory() && runId.test(entry.name));
  for (const { name: session } of await directories(store)) {
    for (const { name: id } of await directories(join(store, session))) {
      const directory = join(store, session, id);
      const file = join(directory, 'index.json');
      const index = await readFile(file, 'utf8').then(JSON.parse, error => {
        if (error.code !== 'ENOENT') throw error;
        return null;
      });
      const recoveredDeath = latestRun?.id === id && latestRun.death;
      let changed = false;
      if (recoveredDeath && index && Object.values(index.slots).some(Boolean)) {
        const at = Date.parse(recoveredDeath.observedAt) || Date.now();
        const active = index.incidents.findLast(event => !event.recoveredAt);
        if (active && active.outcome !== 'death') {
          Object.assign(active, { outcome: 'death', deathAt: at });
          changed = true;
        } else if (!active) {
          const times = {};
          for (const [slot, checkpoint] of Object.entries(index.slots)) {
            if (!snapshotName.test(checkpoint ?? '')) continue;
            const saved = JSON.parse(await readFile(join(directory, checkpoint, 'checkpoint.json'), 'utf8'));
            if (saved.at < at) times[slot] = at - saved.at;
          }
          const slots = Object.fromEntries(Object.entries(index.slots).map(([slot, checkpoint]) =>
            [slot, times[slot] === undefined ? null : checkpoint]));
          index.incidents.push({ outcome: 'death', occurredAt: at, dangerAt: null, slots, ageMs: times });
          changed = true;
        }
      }
      // Keep the last indexed pair for an interrupted run even if no death
      // information survived. Published incident archives remain immutable.
      if (changed) {
        index.slots = { recent: null, anchor: null };
        await replaceIndex(directory, JSON.stringify(index, null, 2));
      }
      const keep = new Set([
        ...Object.values(index?.slots ?? {}),
        ...(index?.incidents.flatMap(event => Object.values(event.slots)) ?? []),
      ]);
      for (const entry of await readdir(directory, { withFileTypes: true })) {
        if ((entry.name === '.index.tmp' || entry.name.startsWith('.') && entry.name.endsWith('.tmp')) ||
            (entry.isDirectory() && snapshotName.test(entry.name) && !keep.has(entry.name))) {
          await rm(join(directory, entry.name), { recursive: true, force: true });
        }
      }
    }
  }
}

async function replaceIndex(directory, contents) {
  const temporary = join(directory, '.index.tmp');
  try {
    await writeFile(temporary, contents, { mode: 0o600 });
    await rename(temporary, join(directory, 'index.json'));
  } finally {
    await rm(temporary, { force: true });
  }
}

export function createCheckpointCollector({ root, run, id, session, bot, collection = {},
  budgetBytes = diskBudget, calmMs = 90_000, recoveryMs = 30_000, settleMs = 200, captureWindowMs = 12_000 }) {
  const store = join(root, 'checkpoints');
  const directory = join(store, session, id);
  const player = offlinePlayerId(bot);
  const server = join(run, 'server');
  const world = join(server, 'world');
  const emptySlots = () => ({ recent: null, anchor: null });
  const index = { version: 1, session, run: id, slots: emptySlots(), incidents: [], observed: 0, captured: 0, skipped: {} };
  const checkpoints = new Map();
  let queue = Promise.resolve();
  let published = { captured: 0, incidents: 0 };
  let generation, health, calmSince, dangerAt, recoverySince, incident, error, lastSample;
  let stopped = false;
  let capturePending = false;
  let nextProbe = 0;
  let halfHealth = false;
  const closing = new AbortController();

  return {
    status: () => ({ session, state: collection.full ? 'full' : error ? 'error' : 'collecting', error,
      observed: index.observed, ...published }),
    sample(status, at = Date.now()) {
      if (stopped || !Number.isFinite(status.health)) return queue;
      if (lastSample !== undefined && at - lastSample > 6_000) {
        calmSince = undefined;
        recoverySince = undefined;
      }
      lastSample = at;
      const danger = status.health < health || status.health <= 10 || Boolean(status.combat ||
        status.survival?.dangers?.length || status.survival?.response || status.survival?.policy?.encounter);
      health = status.health;
      const calm = !danger && health >= 18 && Array.isArray(status.survival?.dangers);
      calmSince = calm ? calmSince ?? at : undefined;
      if (danger) {
        dangerAt ??= at;
        recoverySince = undefined;
      } else if (calm && dangerAt !== undefined) {
        recoverySince ??= at;
        if (at - recoverySince >= recoveryMs) {
          halfHealth = false;
          dangerAt = undefined;
          recoverySince = undefined;
          // The queued event may still be written when a later encounter begins.
          enqueue(async () => {
            if (!incident) return;
            incident.recoveredAt = at;
            incident = undefined;
            await publish();
          });
        }
      } else if (!calm) {
        recoverySince = undefined;
      }
      if (health <= 10 && !halfHealth) {
        halfHealth = true;
        const started = dangerAt ?? at;
        enqueue(() => retain('half_health', started, at));
      }
      if (capturePending || collection.full || at < nextProbe) return queue;
      nextProbe = at + 750;
      capturePending = true;
      return enqueue(async () => {
        try { await capture(); } finally { capturePending = false; }
      });
    },
    death(death) {
      return enqueue(() => retainDeath(death));
    },
    stop() {
      stopped = true;
      closing.abort();
    },
    finish(death) {
      stopped = true;
      closing.abort();
      return enqueue(async () => {
        if (death) await retainDeath(death, false);
        if (!index.observed && !index.incidents.length) return;
        const previous = index.slots;
        index.slots = emptySlots();
        try {
          await publish();
        } catch (failure) {
          index.slots = previous;
          throw failure;
        }
        await prune();
      });
    },
  };

  function enqueue(operation) {
    queue = queue.then(operation).catch(failure => {
      error = failure.message;
    });
    return queue;
  }

  function skip(reason) {
    index.skipped[reason] = (index.skipped[reason] ?? 0) + 1;
  }

  async function publish() {
    await mkdir(directory, { recursive: true, mode: 0o700 });
    const contents = JSON.stringify(index, null, 2);
    if (await diskUsage(store) + Buffer.byteLength(contents) > budgetBytes) {
      collection.full = true;
      throw capacity;
    }
    await replaceIndex(directory, contents);
    published = { captured: index.captured, incidents: index.incidents.length };
  }

  async function prune() {
    const referenced = new Set([
      ...Object.values(index.slots),
      ...index.incidents.flatMap(event => Object.values(event.slots)),
    ]);
    for (const checkpoint of checkpoints.keys()) {
      if (referenced.has(checkpoint)) continue;
      await rm(join(directory, checkpoint), { recursive: true, force: true });
      checkpoints.delete(checkpoint);
    }
  }

  async function retainDeath(death, durable = true) {
    const at = Date.parse(death.observedAt);
    const endedAt = Number.isFinite(at) ? at : Date.now();
    dangerAt = Math.min(dangerAt ?? endedAt, endedAt);
    recoverySince = undefined;
    await retain('death', dangerAt, endedAt, durable);
  }

  async function retain(outcome, started, at, durable = true) {
    if (incident) {
      if (outcome !== 'death' || incident.outcome === 'death') return;
      Object.assign(incident, { outcome, deathAt: at });
    } else {
      const slots = Object.fromEntries(Object.entries(index.slots).map(([slot, checkpoint]) =>
        [slot, (checkpoints.get(checkpoint)?.at ?? Infinity) < started ? checkpoint : null]));
      const ageMs = Object.fromEntries(Object.entries(slots).map(([slot, checkpoint]) =>
        [slot, checkpoint ? started - checkpoints.get(checkpoint).at : null]));
      incident = { outcome, dangerAt: started, occurredAt: at, slots, ageMs };
      index.incidents.push(incident);
    }
    if (durable) await publish();
  }

  async function copyStableWorld(marker, destination, limit, signal) {
    const deadline = Date.now() + captureWindowMs;
    // The server can leave writes queued after a natural autosave.
    do {
      if (dangerAt !== undefined || calmSince === undefined) return null;
      const before = await fingerprint(world);
      await delay(settleMs, undefined, { signal });
      if (before === await fingerprint(world)) {
        const bytes = await archive(world, destination, limit, signal);
        if (bytes && before === await fingerprint(world)) return bytes;
        await rm(destination, { force: true });
      }
      const current = await saveMarkers(world, player);
      if (marker.level !== current?.level || marker.player !== current?.player) return null;
    } while (Date.now() < deadline);
    return null;
  }

  async function capture() {
    if (stopped) return;
    const marker = await saveMarkers(world, player);
    if (!marker || generation?.level === marker.level || generation?.player === marker.player) return;
    if (generation) {
      const gap = marker.at - generation.at;
      const previous = index.autosaves;
      index.autosaves = { ...previous, lastAt: marker.at,
        minIntervalMs: Math.min(previous.minIntervalMs ?? gap, gap),
        maxIntervalMs: Math.max(previous.maxIntervalMs ?? gap, gap) };
    } else {
      index.autosaves = { firstAt: marker.at, lastAt: marker.at };
    }
    generation = marker;
    index.observed++;
    if (dangerAt !== undefined || calmSince === undefined) {
      skip(incident ? 'incident' : 'danger');
      await publish();
      return;
    }
    const signal = AbortSignal.any([closing.signal, AbortSignal.timeout(captureWindowMs + 5_000)]);
    const checkpoint = randomUUID();
    const stage = join(directory, `.${checkpoint}.tmp`);
    try {
      // Leave room to publish the index and to retain an incident after a cap stop.
      let remaining = budgetBytes - await diskUsage(store) - Math.min(65_536, Math.floor(budgetBytes / 4));
      if (remaining <= 0) throw capacity;
      await mkdir(stage, { recursive: true, mode: 0o700 });
      const bytes = await copyStableWorld(marker, join(stage, 'world.tar.zst'), remaining, signal);
      const end = await saveMarkers(world, player);
      if (!bytes || marker.level !== end?.level || marker.player !== end?.player) {
        skip(dangerAt === undefined ? 'changed' : 'danger');
        await publish();
        return;
      }
      remaining -= bytes;
      const [properties, runtime] = await Promise.all([
        readFile(join(server, 'server.properties'), 'utf8'),
        readFile(join(run, 'run.json'), 'utf8').then(JSON.parse),
      ]);
      const base = { version: 1, session, run: id, player, at: marker.at, capturedAt: Date.now(), runtime };
      let metadata = JSON.stringify({ ...base, context: null }, null, 2);
      const metadataBytes = Buffer.byteLength(metadata) + Buffer.byteLength(properties);
      if (metadataBytes > remaining) throw capacity;
      const transcript = join(stage, 'pi.jsonl');
      try {
        const settings = await readFile(join(run, 'checkpoint-context.json'), 'utf8').catch(error => {
          if (error.code !== 'ENOENT') throw error;
          return null;
        });
        const context = await copyContext(run, transcript, marker.at, remaining - metadataBytes, signal);
        if (context) {
          const withContext = JSON.stringify({ ...base, context, contextSettings: settings && JSON.parse(settings) }, null, 2);
          if (Buffer.byteLength(withContext) + Buffer.byteLength(properties) + context.bytes > remaining) throw capacity;
          metadata = withContext;
        }
      } catch (failure) {
        signal.throwIfAborted();
        await rm(transcript, { force: true });
        skip(failure === capacity ? 'context_full' : 'context_error');
      }
      await writeFile(join(stage, 'checkpoint.json'), metadata, { mode: 0o600 });
      await writeFile(join(stage, 'server.properties'), properties, { mode: 0o600 });
      signal.throwIfAborted();
      if (dangerAt !== undefined || calmSince === undefined) {
        skip('danger');
        await publish();
        return;
      }
      await rename(stage, join(directory, checkpoint));
      checkpoints.set(checkpoint, { at: marker.at, calm: marker.at - calmSince >= calmMs });
      const previous = { ...index.slots };
      if (checkpoints.get(previous.recent)?.calm) index.slots.anchor = previous.recent;
      index.slots.recent = checkpoint;
      index.captured++;
      try {
        await publish();
      } catch (failure) {
        index.slots = previous;
        index.captured--;
        await prune();
        throw failure;
      }
      await prune();
      error = undefined;
      return;
    } catch (failure) {
      if (stopped) return;
      if (failure === capacity || failure.code === 'ENOSPC') {
        collection.full = true;
        skip('full');
      } else {
        error = failure.message;
        skip(failure.code === 'ENOENT' ? 'changed' : 'error');
      }
    } finally {
      await rm(stage, { recursive: true, force: true });
    }
    // Free the abandoned archive before checking room for the index.
    await publish();
  }
}

export async function restoreCheckpoint(checkpoint, destination) {
  const metadata = JSON.parse(await readFile(join(checkpoint, 'checkpoint.json'), 'utf8'));
  if (metadata.version !== 1) throw new Error('Unsupported Minecraft checkpoint format.');
  await mkdir(destination, { mode: 0o700 });
  try {
    const world = join(destination, 'world');
    await mkdir(world);
    await exec('tar', ['-I', compressor, '-xf', join(checkpoint, 'world.tar.zst'), '-C', world], { timeout: 60_000 });
    const original = await readFile(join(checkpoint, 'server.properties'), 'utf8');
    const properties = original.split(/\r?\n/).filter(line => !/^(force-gamemode|server-ip|enable-rcon)=/.test(line));
    properties.push('force-gamemode=false', 'server-ip=127.0.0.1', 'enable-rcon=false', '');
    await writeFile(join(destination, 'server.properties'), properties.join('\n'), { mode: 0o600 });
    await copyFile(join(checkpoint, 'checkpoint.json'), join(destination, 'checkpoint.json'));
    if (metadata.context) await copyFile(join(checkpoint, 'pi.jsonl'), join(destination, 'pi.jsonl'));
    return metadata;
  } catch (error) {
    await rm(destination, { recursive: true, force: true });
    throw error;
  }
}

if (import.meta.main) {
  const [command, checkpoint, destination] = process.argv.slice(2);
  if (command !== 'restore' || !checkpoint || !destination || process.argv.length !== 5) {
    console.error('Usage: node src/checkpoints.mjs restore CHECKPOINT NEW_DIRECTORY');
    process.exitCode = 1;
  } else {
    try {
      await restoreCheckpoint(resolve(checkpoint), resolve(destination));
      console.log(resolve(destination));
    } catch (error) {
      console.error(error.message);
      process.exitCode = 1;
    }
  }
}
