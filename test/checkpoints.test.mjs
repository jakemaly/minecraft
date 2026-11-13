import assert from 'node:assert/strict';
import { mkdir, mkdtemp, readFile, readdir, rm, utimes, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { test } from 'node:test';
import { createCheckpointCollector, offlinePlayerId, recoverCheckpoints, restoreCheckpoint } from '../src/checkpoints.mjs';

const session = '12abcdef3456';
const playerFile = `playerdata/${offlinePlayerId('Acacia')}.dat`;
const transcript = '2026-11-13T18-00-00-000Z_01234567-89ab-cdef-0123-456789abcdef.jsonl';

async function fixture(t, options) {
  const root = await mkdtemp(join(tmpdir(), 'acacia-checkpoints-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const run = join(root, session);
  const world = join(run, 'server/world');
  const store = join(root, 'checkpoints', session, session);
  await mkdir(join(world, 'playerdata'), { recursive: true });
  await mkdir(join(world, 'DIM-1', 'region'), { recursive: true });
  await writeFile(join(run, 'run.json'), JSON.stringify({ id: session, bot: 'Acacia', version: '1.21.4' }));
  await writeFile(join(run, 'server/server.properties'), 'online-mode=false\nforce-gamemode=true\nhardcore=true\n');
  await writeFile(join(world, 'session.lock'), 'locked live world');
  const collector = createCheckpointCollector({ root, run, id: session, session, bot: 'Acacia', ...options });
  const start = Math.floor(Date.now() / 1_000) * 1_000 - 1_000;
  const calm = { health: 20, survival: { dangers: [] } };
  await collector.sample(calm, start);
  let sequence = 0;
  return {
    root, run, store, collector, calm, start,
    async save(label, status = calm, at = start + ++sequence * 1_000, sample = true) {
      await Promise.all([
        [playerFile, 'player'], ['level.dat', 'level'], ['DIM-1/region/r.0.0.mca', 'region'],
      ].map(async ([file, kind]) => {
        const path = join(world, file);
        await writeFile(path, `${label} ${kind}`);
        await utimes(path, at / 1000, at / 1000);
      }));
      if (sample) await collector.sample(status, at);
    },
    async index() { return JSON.parse(await readFile(join(store, 'index.json'), 'utf8')); },
  };
}

test('passive autosaves rotate two slots, preserve incidents, and restore without future context', async t => {
  const f = await fixture(t, { calmMs: 1000, recoveryMs: 1000 });
  const preDecision = { type: 'message', id: 'before', parentId: null, timestamp: new Date(f.start).toISOString(), message: { role: 'user', content: 'observed' } };
  const future = { type: 'message', id: 'future', parentId: 'before', timestamp: new Date(f.start + 9000).toISOString(), message: { role: 'assistant', content: 'outcome' } };
  await writeFile(join(f.run, transcript), [
    JSON.stringify({ type: 'session', timestamp: preDecision.timestamp }),
    JSON.stringify(preDecision), JSON.stringify(future), '',
  ].join('\n'));
  await f.collector.sample(f.calm, f.start + 250);
  await assert.rejects(f.index(), { code: 'ENOENT' });
  await f.save('anchor');
  await f.save('recent');
  const first = await f.index();
  assert.notEqual(first.slots.recent, first.slots.anchor);
  assert.ok(Math.abs(first.autosaves.minIntervalMs - 1000) < 1);
  await f.collector.sample({ ...f.calm, combat: { targetId: 1 } }, f.start + 2100);
  await f.collector.sample({ ...f.calm, health: 10 }, f.start + 2200);
  await f.save('injured', { ...f.calm, health: 8 });
  await f.collector.sample(f.calm, f.start + 3300);
  await f.collector.sample({ ...f.calm, health: 9 }, f.start + 3500);
  await f.collector.finish({ observedAt: new Date(f.start + 3800).toISOString(), cause: 'test death' });
  const index = await f.index();
  assert.equal(index.incidents.length, 1);
  const [incident] = index.incidents;
  assert.equal(incident.outcome, 'death');
  assert.equal(incident.dangerAt, f.start + 2100);
  assert.deepEqual(incident.slots, first.slots);
  assert.ok(Math.abs(incident.ageMs.recent - 100) < 1);
  assert.equal(index.skipped.incident, 1);
  const destination = join(f.root, 'restore');
  const result = await restoreCheckpoint(join(f.store, incident.slots.recent), destination);
  assert.equal(await readFile(join(destination, 'world', playerFile), 'utf8'), 'recent player');
  assert.equal(await readFile(join(destination, 'world/DIM-1/region/r.0.0.mca'), 'utf8'), 'recent region');
  assert.equal((await readdir(join(destination, 'world'))).includes('session.lock'), false);
  assert.match(await readFile(join(destination, 'server.properties'), 'utf8'), /^force-gamemode=false$/m);
  assert.deepEqual((await readFile(join(destination, 'pi.jsonl'), 'utf8')).trimEnd().split('\n').map(JSON.parse).slice(1), [preDecision]);
  assert.equal(result.context.leaf, 'before');
  await assert.rejects(restoreCheckpoint(join(f.store, incident.slots.anchor), destination), { code: 'EEXIST' });
});

test('the shared disk cap leaves earlier saves in place and abandons staging', async t => {
  const f = await fixture(t, { budgetBytes: 15000, calmMs: 0 });
  await f.save('first');
  const first = await f.index();
  await writeFile(join(f.root, 'checkpoints', 'previous-session'), Buffer.alloc(12000));
  await f.save('second');
  assert.equal(f.collector.status().state, 'full');
  await f.collector.finish({ observedAt: new Date(f.start + 3000).toISOString() });
  const index = await f.index();
  assert.equal(index.incidents[0].slots.recent, first.slots.recent);
  assert.deepEqual((await readdir(f.store)).sort(), [first.slots.recent, 'index.json'].sort());
});

test('a live file change during a capture attempt leaves both slots intact', async t => {
  const f = await fixture(t, { settleMs: 150, captureWindowMs: 0 });
  await f.save('old');
  const before = await f.index();
  await f.save('new', f.calm, f.start + 3000, false);
  const sample = f.collector.sample(f.calm, f.start + 3000);
  await delay(50);
  await writeFile(join(f.run, 'server/world/DIM-1/region/r.0.0.mca'), 'changed while capturing');
  await sample;
  const after = await f.index();
  assert.deepEqual(after.slots, before.slots);
  assert.equal(after.skipped.changed, 1);
});

test('after service interruption retained saves survive and stale staging is removed', async t => {
  const f = await fixture(t, { calmMs: 0 });
  await f.save('first');
  await f.save('second');
  const before = await f.index();
  await mkdir(join(f.store, '.capture.tmp'));
  await recoverCheckpoints(f.root, { id: session, interrupted: true, death: {
    observedAt: new Date(f.start + 3000).toISOString(),
  } });
  const after = await f.index();
  assert.deepEqual(after.incidents[0].slots, before.slots);
  assert.equal(after.incidents[0].dangerAt, null);
  assert.deepEqual(after.slots, { recent: null, anchor: null });
  assert.deepEqual((await readdir(f.store)).sort(), [before.slots.recent, before.slots.anchor, 'index.json'].sort());
});

test('a failed final index update leaves every published archive available', async t => {
  const f = await fixture(t, { budgetBytes: 15_000 });
  await f.save('published');
  const before = await f.index();
  await writeFile(join(f.root, 'checkpoints', 'pressure'), Buffer.alloc(15_000));
  await f.collector.finish();
  assert.deepEqual(await f.index(), before);
  assert.ok((await readdir(f.store)).includes(before.slots.recent));
});

test('a world fits even when the optional Pi transcript exceeds the capture budget', async t => {
  const f = await fixture(t, { budgetBytes: 15_000 });
  await writeFile(join(f.run, transcript), JSON.stringify({
    type: 'message', id: 'large', timestamp: new Date(f.start).toISOString(),
    message: { role: 'user', content: 'x'.repeat(30_000) },
  }) + '\n');
  await f.save('world');
  const index = await f.index();
  assert.equal(index.captured, 1);
  assert.equal(index.skipped.context_full, 1);
  const restored = await restoreCheckpoint(join(f.store, index.slots.recent), join(f.root, 'restore'));
  assert.equal(restored.context, null);
});
