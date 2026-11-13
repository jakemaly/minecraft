import { createHash } from 'node:crypto';
import { createServer } from 'node:http';
import { createServer as createSocket } from 'node:net';
import { mkdir, readFile, realpath } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { recoverRuns, startRun } from './run.mjs';
import { conversation, runId, savedRuns } from './history.mjs';
import { modelId, thinkingLevel } from './pi.mjs';

export async function dashboard({ host, port, root, agentDir, viewerPort, gamePort, allowed, jar, inference }) {
  await mkdir(root, { recursive: true, mode: 0o700 });
  const config = { host, root: await realpath(root), agentDir, viewerPort, gamePort, allowed, jar, inference };
  const assets = new Map(await Promise.all([
    ['/', 'index.html', 'text/html; charset=utf-8'],
    ['/dashboard.mjs', 'dashboard.mjs', 'text/javascript; charset=utf-8'],
    ['/assets/dark-theme-enabled.svg', 'assets/dark-theme-enabled.svg', 'image/svg+xml'],
  ].map(async ([route, file, type]) => [route, { body: await readFile(new URL(`../web/${file}`, import.meta.url)), type }])));
  let run;
  const status = () => run ? {
    id: run.id, state: run.state, detail: run.detail, activity: run.activity ?? [], player: run.player,
    model: modelId, thinkingLevel,
    viewer: run.state === 'running' ? run.viewer : null,
    spectator: run.state === 'running' ? run.spectator : null,
  } : { state: 'idle', activity: [], model: modelId, thinkingLevel };
  const active = () => ['starting', 'running', 'saving'].includes(run?.state);

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
        if (active()) return json(409, { error: 'A run is already active.' });
        const started = startRun(config);
        run = started;
        started.done.catch(error => {
          started.state = 'incomplete';
          started.detail = error.message;
          console.error(error);
        });
        return json(202, status());
      }
      if (request.url === '/api/stop') {
        if (active()) void run.stop().catch(() => {});
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
    await new Promise((ready, reject) => server.once('error', reject).listen(port, host, ready));
  } catch (error) {
    lock.close();
    throw error;
  }
  return {
    port: server.address().port,
    async close() {
      const closed = new Promise(ready => server.close(ready));
      if (active()) await run.stop().catch(() => {});
      await closed;
      lock.close();
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
  });
  console.log(`Dashboard at http://${host}:${app.port}`);
  for (const signal of ['SIGTERM', 'SIGINT']) process.once(signal, () => { void app.close(); });
}
