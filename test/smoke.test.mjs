import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { createConnection, createServer } from 'node:net';
import { mkdir, mkdtemp, readFile, readdir, rename, rm, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { test } from 'node:test';
import { Agent, fetch as socketFetch } from 'undici';
import { openai } from '../src/pi.mjs';
import { dashboard } from '../src/server.mjs';

const host = '127.0.0.1';
const send = (port, path) => fetch(`http://${host}:${port}${path}`, {
  method: 'POST', headers: { origin: `http://${host}:${port}` },
});

export async function scriptedInference(agentDir, scenario) {
  const { model, runtime } = await openai(agentDir);
  let step = 0;
  let actionId;
  runtime.streamSimple = (_model, context, { signal } = {}) => {
    const messages = context.messages ?? context;
    const lastResult = messages.findLast(message => message.role === 'toolResult');
    const stage = step;
    const earlyReply = [{ type: 'text', text: 'I defeated the dragon.' }];
    let content = earlyReply;
    if (['expired', 'logging'].includes(scenario) && stage === 1) content = [call('view_status', { rationale: 'Read the bot after reconnecting.' })];
    if (scenario === 'continue') {
      if (stage === 0) {
        content = [call('raw_action', {
          operation: 'control', state: 'jump', ticks: 30,
          submission_id: 'smoke-jump', rationale: 'Check the live viewer.',
          response_format: 'json',
        })];
      }
      if (stage === 2 || stage === 4 || stage === 6 || stage === 8) {
        assert.ok(messages.some(message => message.role === 'assistant' &&
          message.content.some(block => block.type === 'text' && block.text === earlyReply[0].text)));
        if (!actionId) {
          const result = JSON.parse(lastResult.content[0].text).response.data;
          assert.equal(result.state, 'accepted');
          actionId = result.actionId;
        }
        content = [call('wait_for_action', {
          action_id: actionId, timeout_ms: 120_000, rationale: 'Retrieve the existing jump.',
          ...(stage === 4 ? { response_format: 'json' } : {}),
        })];
      }
      if (stage === 3) {
        assert.ok(lastResult.content[0].text.startsWith(`Action ID: ${actionId}\n\n## `));
        assert.match(lastResult.content[0].text, /\*\*Status:\*\* succeeded/);
      }
    }
    const errorMessage = scenario === 'provider' && stage > 0 ? '401: OAuth session expired' : undefined;
    const stopReason = errorMessage ? 'error' : content[0].type === 'toolCall' ? 'toolUse' : 'stop';
    step++;
    const message = {
      role: 'assistant', api: model.api, provider: model.provider, model: model.id, content, stopReason,
      timestamp: Date.now(), errorMessage,
      usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
    };
    return {
      async *[Symbol.asyncIterator]() {
        if (stage === 0) await delay(3000, undefined, { signal });
        if (stage > 0 && ['stop', 'win', 'disconnect'].includes(scenario)) await delay(90_000, undefined, { signal });
        if (stage === 1 && ['expired', 'logging'].includes(scenario)) await delay(3000, undefined, { signal });
        yield { type: 'start', partial: message };
        if (errorMessage) yield { type: 'error', reason: 'error', error: message };
        else yield { type: 'done', reason: stopReason, message };
      },
      async result() { return message; },
    };
  };
  return { model, runtime };

  function call(name, args) {
    return { type: 'toolCall', id: `call_${step}`, name: `mcp__minecraft__${name}`, arguments: args };
  }
}

async function sparePort() {
  const server = createServer().listen(0, host);
  await new Promise(ready => server.once('listening', ready));
  const { port } = server.address();
  await new Promise(ready => server.close(ready));
  return port;
}

export async function smokeDashboard(root, viewerPort, gamePort, scenario = 'continue') {
  const agentDir = join(root, 'pi');
  await mkdir(agentDir);
  // This fixture exercises Pi’s real session and tool execution with a scripted provider stream.
  await writeFile(join(agentDir, 'auth.json'), JSON.stringify({
    openai: { type: 'oauth', access: 'smoke-test', refresh: 'smoke-test', expires: Date.now() + 3_600_000 },
  }));
  return dashboard({
    host, port: 0, root, viewerPort, gamePort, allowed: new Set([host]), agentDir,
    jar: process.env.MINECRAFT_SERVER_JAR,
    inference: agentDir => scriptedInference(agentDir, scenario),
  });
}

const smokeOptions = { skip: process.env.MINECRAFT_SMOKE !== '1', timeout: 140_000 };

async function attempt(scenario, verify) {
  const root = await mkdtemp(join(tmpdir(), 'mine-ai-'));
  const viewerPort = await sparePort();
  const gamePort = await sparePort();
  const app = await smokeDashboard(root, viewerPort, gamePort, scenario);
  const status = async () => (await fetch(`http://${host}:${app.port}/api/run`)).json();
  const waitFor = async condition => {
    for (let n = 0; n < 180; n++) {
      const run = await status();
      if (condition(run)) return run;
      await delay(500);
    }
    assert.fail(`Timed out with ${JSON.stringify(await status())}`);
  };
  try {
    assert.equal((await fetch(`http://${host}:${app.port}/api/start`, { method: 'POST' })).status, 404);
    assert.equal((await send(app.port, '/api/start')).status, 202);
    assert.equal((await send(app.port, '/api/start')).status, 409);
    const run = await waitFor(status => status.state === 'running' || status.state === 'incomplete');
    assert.equal(run.state, 'running', run.detail);
    const terminal = () => waitFor(status => ['won', 'incomplete', 'stopped'].includes(status.state));
    const directory = join(root, run.id);
    const consoleCommand = command => execFileSync('docker', ['exec', `mineai-${run.id}-server`,
      'sh', '-c', 'printf "%s\n" "$1" > /proc/1/fd/0', 'sh', command], { timeout: 5000 });
    await verify({ run, app, gamePort, directory, waitFor, terminal, consoleCommand });
    await stat(join(directory, 'server/world/level.dat'));
    assert.ok((await readdir(directory)).some(name => name.endsWith('.jsonl') && name !== 'activity.jsonl'));
    const serverLog = await readFile(join(directory, 'server.log'), 'utf8');
    assert.match(serverLog, /Acacia joined the game/);
    assert.match(serverLog, /Set Acacia's game mode to Survival Mode/);
    assert.match(serverLog, /All dimensions are saved/);
  } finally {
    await app.close();
    if (process.env.MINECRAFT_KEEP_SMOKE === '1') console.log(root);
    else await rm(root, { recursive: true, force: true });
  }
}

test('a premature final reply preserves a pending action and stale results do not extend retries', smokeOptions, () =>
  attempt('continue', async ({ run, gamePort, terminal }) => {
    assert.equal((await fetch(run.viewer)).status, 200);
    assert.equal(run.player.health, 20);
    assert.ok(Array.isArray(run.player.inventory));
    await new Promise((resolve, reject) => {
      const client = createConnection({ host, port: gamePort });
      client.on('error', reject);
      client.on('connect', () => { client.end(); resolve(); });
    });
    await assert.rejects(fetch(`http://${host}:25575/health`));
    const final = await terminal();
    assert.equal(final.state, 'incomplete');
    const earlyFinal = final.activity.findIndex(event => event.type === 'assistant' && event.stopReason === 'stop');
    const settled = final.activity.findIndex(event => event.type === 'tool_execution_end' &&
      event.tool === 'mcp__minecraft__wait_for_action' && event.outcome === 'settled' && !event.isError);
    assert.ok(earlyFinal >= 0 && settled > earlyFinal);
    assert.deepEqual(final.activity.filter(event => event.type === 'continuation').map(event => event.attempt), [1, 1, 2, 3]);
    assert.match(final.detail, /3 follow-ups produced no completed Minecraft action/);
  }));

test('a terminal provider error during continuation saves and reports the error', smokeOptions, () =>
  attempt('provider', async ({ terminal }) => {
    const final = await terminal();
    assert.equal(final.state, 'incomplete');
    assert.equal(final.detail, '401: OAuth session expired');
    assert.equal(final.activity.filter(event => event.type === 'continuation').length, 1);
  }));

test('operator stop interrupts continuation and saves the world', smokeOptions, () =>
  attempt('stop', async ({ app, waitFor, terminal }) => {
    await waitFor(run => run.activity.some(event => event.type === 'continuation'));
    assert.equal((await send(app.port, '/api/stop')).status, 202);
    const final = await terminal();
    assert.equal(final.state, 'stopped');
    assert.equal(final.detail, 'Stopped by operator.');
  }));

test('the server advancement ends continuation and proves the win', smokeOptions, () =>
  attempt('win', async ({ waitFor, terminal, consoleCommand }) => {
    await waitFor(run => run.activity.some(event => event.type === 'continuation'));
    // Exercise Minecraft's real advancement log without a full playthrough.
    consoleCommand('advancement grant Acacia only minecraft:end/kill_dragon');
    const final = await terminal();
    assert.equal(final.state, 'won', final.detail);
    assert.match(final.detail, /Acacia has made the advancement \[Free the End\]/);
  }));

test('death and a temporary game-mode change keep the run live until an actual disconnect', smokeOptions, () =>
  attempt('disconnect', async ({ waitFor, terminal, consoleCommand }) => {
    await waitFor(run => run.activity.some(event => event.type === 'continuation'));
    consoleCommand('kill Acacia');
    consoleCommand('gamemode spectator Acacia');
    await delay(15_000);
    await waitFor(run => run.state === 'running');
    consoleCommand('kick Acacia smoke test disconnect');
    const final = await terminal();
    assert.equal(final.state, 'incomplete');
    assert.match(final.detail, /Mine AI lost its Minecraft connection/);
  }));

test('an expired MCP session reconnects without restarting Pi or Minecraft', smokeOptions, () =>
  attempt('expired', async ({ directory, waitFor, terminal }) => {
    await waitFor(run => run.activity.some(event => event.type === 'continuation'));
    const log = await readFile(join(directory, 'mine-ai.log'), 'utf8');
    const sessionId = log.match(/MCP client session connected: ([a-f0-9-]+)/)?.[1];
    assert.ok(sessionId);
    const dispatcher = new Agent({ connect: { socketPath: join(directory, 'mcp.sock') } });
    try {
      const closed = await socketFetch('http://127.0.0.1:25575/mcp', {
        method: 'DELETE', headers: { 'mcp-session-id': sessionId }, dispatcher,
      });
      assert.ok(closed.ok);
      await closed.body?.cancel();
    } finally {
      await dispatcher.destroy();
    }
    const final = await terminal();
    assert.equal(final.state, 'incomplete');
    assert.ok(final.activity.some(event => event.type === 'tool_execution_end' &&
      event.tool === 'mcp__minecraft__view_status' && !event.isError));
    const after = await readFile(join(directory, 'mine-ai.log'), 'utf8');
    assert.equal([...after.matchAll(/MCP client session connected:/g)].length, 2);
    assert.equal([...after.matchAll(/runtime ready on port/g)].length, 1);
  }));

test('activity logging failure still saves the world and reports the error', smokeOptions, () =>
  attempt('logging', async ({ directory, waitFor, terminal }) => {
    await waitFor(run => run.activity.some(event => event.type === 'continuation'));
    const activity = join(directory, 'activity.jsonl');
    await rename(activity, join(directory, 'activity-before-failure.jsonl'));
    await mkdir(activity);
    const final = await terminal();
    assert.equal(final.state, 'incomplete');
    assert.match(final.detail, /Could not record run activity/);
  }));
