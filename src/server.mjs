import { createHash } from 'node:crypto';
import { createServer } from 'node:http';
import { createServer as createSocket } from 'node:net';
import { mkdir, readFile, realpath } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { dragonAdvancement, recoverRuns, startRun } from './run.mjs';
import { conversation, runId, savedRuns } from './history.mjs';
import { modelId, thinkingLevel } from './pi.mjs';
import { deathReview, readReflection, recordedDeath, reflectDeath, writeReflection } from './primitives.mjs';

export async function dashboard({ host, port, root, agentDir, viewerPort, gamePort, allowed, jar, inference, hardcore = false, reflect = reflectDeath }) {
  await mkdir(root, { recursive: true, mode: 0o700 });
  const config = { host, root: await realpath(root), agentDir, viewerPort, gamePort, allowed, jar, inference, hardcore };
  const assets = new Map(await Promise.all([
    ['/', 'index.html', 'text/html; charset=utf-8'],
    ['/dashboard.mjs', 'dashboard.mjs', 'text/javascript; charset=utf-8'],
    ['/assets/dark-theme-enabled.svg', 'assets/dark-theme-enabled.svg', 'image/svg+xml'],
  ].map(async ([route, file, type]) => [route, { body: await readFile(new URL(`../web/${file}`, import.meta.url)), type }])));
  let run;
  let review;
  let closing = false;
  const status = () => run ? {
    id: run.id, state: run.state, detail: run.detail, hardcore: run.hardcore, profile: run.profile, parent: run.parent, death: run.death, activity: run.activity ?? [], player: run.player,
    model: modelId, thinkingLevel,
    viewer: run.state === 'running' ? run.viewer : null,
    spectator: run.state === 'running' ? run.spectator : null,
  } : { state: 'idle', activity: [], model: modelId, thinkingLevel };
  const active = () => Boolean(review) || ['starting', 'running', 'saving'].includes(run?.state);

  function begin(parent) {
    const started = startRun({ ...config, parent });
    run = started;
    started.done.then(() => {
      if (started.state === 'dead' && started.hardcore && !closing) schedule(started);
    }).catch(error => {
      started.state = 'incomplete';
      started.detail = error.message;
      console.error(error);
    });
  }

  function schedule(ended) {
    const directory = join(config.root, ended.id);
    const controller = new AbortController();
    ended.state = 'reflecting';
    review = {
      controller,
      done: (async () => {
        let reflection = await readReflection(directory);
        if (!reflection) {
          const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(90_000)]);
          try {
            reflection = deathReview(await reflect({ directory, death: ended.death, agentDir, signal, inference }));
          } catch (error) {
            if (controller.signal.aborted) return;
            reflection = { error: String(error?.message ?? error) };
          }
          if (controller.signal.aborted) return;
          await writeReflection(directory, reflection);
        }
        if (controller.signal.aborted || closing || reflection.stopped) return;
        // Saved reviews are human-readable records, including older files with a proposed profile.
        begin(ended.id);
      })().catch(error => {
        ended.detail = `Reflection could not continue: ${error.message}`;
        console.error(error);
      }).finally(() => {
        if (run === ended && ended.state === 'reflecting') ended.state = 'dead';
        if (!review?.stop) review = undefined;
      }),
    };
  }

  const server = createServer(async (request, response) => {
    const json = (statusCode, value) => {
      response.writeHead(statusCode, { 'content-type': 'application/json', 'cache-control': 'no-store' });
      response.end(JSON.stringify(value));
    };
    const origin = `http://${host}:${server.address().port}`;
    if (!allowed.has(request.socket.remoteAddress) || request.headers.host !== new URL(origin).host) {
      return json(403, { error: 'Dashboard access is restricted.' });
    }
    if (request.method === 'GET') {
      const asset = assets.get(request.url);
      if (asset) {
        response.writeHead(200, {
          'content-type': asset.type, 'cache-control': 'no-store',
          'x-frame-options': 'DENY', 'referrer-policy': 'no-referrer',
        });
        return response.end(asset.body);
      }
      if (request.url === '/favicon.ico') return response.writeHead(204).end();
      if (request.url === '/api/run') return json(200, status());
      try {
        const url = new URL(request.url, origin);
        if (url.pathname === '/api/runs') {
          const runs = await savedRuns(config.root);
          if (active()) return json(200, runs.map(saved => saved.id === run.id ? { ...saved, ...status() } : saved));
          return json(200, runs);
        }
        if (url.pathname === '/api/conversation') {
          const id = url.searchParams.get('id');
          const before = url.searchParams.has('before') ? Number(url.searchParams.get('before')) : undefined;
          if (!runId.test(id) || (before !== undefined && (!Number.isSafeInteger(before) || before < 1))) {
            return json(400, { error: 'Invalid conversation request.' });
          }
          return json(200, await conversation(join(config.root, id), before));
        }
      } catch (error) {
        if (error.code === 'ENOENT') return json(404, { error: 'Run unavailable.' });
        console.error(error);
        return json(500, { error: 'Could not read saved run data.' });
      }
    }
    if (request.method === 'POST' && request.headers.origin === origin) {
      if (request.url === '/api/start') {
        if (closing || active()) return json(409, { error: 'A run is already active.' });
        if (hardcore && run?.state === 'won') return json(409, { error: 'The Dragon has already been defeated.' });
        begin();
        return json(202, status());
      }
      if (request.url === '/api/stop') {
        if (review) {
          const ended = run;
          const pending = review;
          pending.controller.abort(new Error('Stopped by operator.'));
          // Keep the lifecycle owned until the stop marker is durable.
          pending.stop ??= pending.done.then(async () => {
            const directory = join(config.root, ended.id);
            await writeReflection(directory, { ...await readReflection(directory), stopped: true });
            review = undefined;
          });
          await pending.stop.catch(error => {
            ended.detail = `Could not record stop: ${error.message}`;
            review = undefined;
            console.error(error);
          });
        } else if (active()) {
          void run.stop().catch(() => {});
        }
        return json(202, status());
      }
    }
    return json(404, { error: 'Route unavailable.' });
  });
  // An abstract socket releases ownership even when the host process is killed.
  const lock = createSocket(client => client.destroy());
  const lockPath = `\0mine-ai-${createHash('sha256').update(config.root).digest('hex')}`;
  await new Promise((ready, reject) => lock.once('error', reject).listen(lockPath, ready));
  try {
    await recoverRuns(config.root);
    [run] = await savedRuns(config.root);
    if (hardcore && run?.hardcore && run.state !== 'won') {
      const directory = join(config.root, run.id);
      // Recover a win written by vanilla before the launcher saved its result.
      for (const file of ['server/logs/latest.log', 'server.log']) {
        const log = await readFile(join(directory, file), 'utf8').catch(error => {
          if (error.code !== 'ENOENT') throw error;
          return '';
        });
        const proof = dragonAdvancement(log);
        if (!proof) continue;
        Object.assign(run, { state: 'won', detail: proof });
        break;
      }
      if (run.state === 'incomplete' && run.interrupted) {
        const death = await recordedDeath(directory).catch(error => {
          console.error(`Could not recover death evidence for ${run.id}: ${error.message}`);
          return null;
        });
        if (death) Object.assign(run, { state: 'dead', death, detail: death.cause ?? 'Minecraft recorded Acacia’s death.' });
      }
    }
    const resumeReview = hardcore && run?.hardcore && run.state === 'dead' &&
      !(await readReflection(join(config.root, run.id)))?.stopped;
    await new Promise((ready, reject) => server.once('error', reject).listen(port, host, ready));
    if (resumeReview) {
      schedule(run);
    }
  } catch (error) {
    lock.close();
    throw error;
  }
  return {
    port: server.address().port,
    async close() {
      closing = true;
      const closed = new Promise(ready => server.close(ready));
      if (review) {
        review.controller.abort(new Error('Harness service shut down.'));
        await (review.stop ?? review.done);
      } else if (active()) await run.stop(new Error('Harness service shut down.')).catch(() => {});
      await closed;
      await new Promise(ready => lock.close(ready));
    },
  };
}

if (import.meta.main) {
  process.umask(0o077);
  const host = process.env.MINECRAFT_HOST ?? '127.0.0.1';
  const app = await dashboard({
    host, port: Number(process.env.MINECRAFT_PORT ?? 2718),
    viewerPort: Number(process.env.MINECRAFT_VIEWER_PORT ?? 3000),
    gamePort: Number(process.env.MINECRAFT_GAME_PORT ?? 25565),
    allowed: new Set([host, '127.0.0.1', process.env.MINECRAFT_CLIENT_TAILSCALE_IP].filter(Boolean)),
    root: resolve(process.env.MINECRAFT_RUNS ?? join(homedir(), '.local/share/mine-ai')),
    agentDir: resolve(process.env.PI_AGENT_DIR ?? join(homedir(), '.local/share/minecraft-agent/pi')),
    jar: process.env.MINECRAFT_SERVER_JAR,
    hardcore: process.env.MINECRAFT_HARDCORE === '1',
  });
  console.log(`Dashboard at http://${host}:${app.port}`);
  for (const signal of ['SIGTERM', 'SIGINT']) process.once(signal, () => { void app.close(); });
}
