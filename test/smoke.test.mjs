import assert from 'node:assert/strict';
import { createConnection, createServer } from 'node:net';
import { mkdir, mkdtemp, readFile, readdir, rm, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { test } from 'node:test';
import { openai } from '../src/pi.mjs';
import { dashboard } from '../src/server.mjs';

const host = '127.0.0.1';
const send = (port, path) => fetch(`http://${host}:${port}${path}`, {
  method: 'POST', headers: { origin: `http://${host}:${port}` },
});

export async function scriptedInference(agentDir) {
  const { model, runtime } = await openai(agentDir);
  let step = 0;
  let actionId;
  runtime.streamSimple = (_model, context, { signal } = {}) => {
    const messages = context.messages ?? context;
    const lastResult = messages.findLast(message => message.role === 'toolResult');
    let content;
    if (step === 0) {
      content = [call('raw_action', {
        operation: 'control', state: 'jump', ticks: 30,
        submission_id: 'smoke-jump', rationale: 'Check the live viewer.',
        response_format: 'json',
      })];
    } else if (step === 1) {
      const result = JSON.parse(lastResult.content[0].text).response.data;
      assert.equal(result.state, 'accepted');
      actionId = result.actionId;
      content = [call('wait_for_action', { action_id: actionId, timeout_ms: 120_000, rationale: 'Wait for the jump.', response_format: 'json' })];
    } else {
      const result = JSON.parse(lastResult.content[0].text).response.data;
      assert.equal(result.state, 'settled');
      assert.equal(result.actionId, actionId);
      content = [{ type: 'text', text: 'I defeated the dragon.' }];
    }
    const stopReason = content[0].type === 'toolCall' ? 'toolUse' : 'stop';
    step++;
    const message = {
      role: 'assistant', api: model.api, provider: model.provider, model: model.id, content, stopReason,
      timestamp: Date.now(),
      usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
    };
    return {
      async *[Symbol.asyncIterator]() {
        if (step === 1) await delay(8000, undefined, { signal });
        if (step === 3 && process.env.MINECRAFT_BROWSER_SMOKE === '1') await delay(90_000, undefined, { signal });
        yield { type: 'start', partial: message };
        yield { type: 'done', reason: stopReason, message };
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

export async function smokeDashboard(root, viewerPort, gamePort) {
  const agentDir = join(root, 'pi');
  await mkdir(agentDir);
  // This fixture exercises Pi’s real session and tool execution with a scripted provider stream.
  await writeFile(join(agentDir, 'auth.json'), JSON.stringify({
    openai: { type: 'oauth', access: 'smoke-test', refresh: 'smoke-test', expires: Date.now() + 3_600_000 },
  }));
  return dashboard({
    host, port: 0, root, viewerPort, gamePort, allowed: new Set([host]), agentDir,
    jar: process.env.MINECRAFT_SERVER_JAR,
    inference: scriptedInference,
  });
}

test('Pi settles a real Mine AI action and an early victory claim leaves the attempt incomplete', {
  skip: process.env.MINECRAFT_SMOKE !== '1', timeout: 140_000,
}, async () => {
  const root = await mkdtemp(join(tmpdir(), 'mine-ai-'));
  const viewerPort = await sparePort();
  const gamePort = await sparePort();
  const app = await smokeDashboard(root, viewerPort, gamePort);
  const status = async () => (await fetch(`http://${host}:${app.port}/api/run`)).json();
  try {
    assert.equal((await fetch(`http://${host}:${app.port}/api/start`, { method: 'POST' })).status, 404);
    assert.equal((await send(app.port, '/api/start')).status, 202);
    assert.equal((await send(app.port, '/api/start')).status, 409);
    let run;
    for (let n = 0; n < 230; n++) {
      run = await status();
      if (run.state === 'running') {
        assert.equal((await fetch(run.viewer)).status, 200);
        await new Promise((resolve, reject) => {
          const client = createConnection({ host, port: gamePort });
          client.on('error', reject);
          client.on('connect', () => { client.end(); resolve(); });
        });
        await assert.rejects(fetch(`http://${host}:25575/health`));
        break;
      }
      if (run.state === 'incomplete') break;
      await delay(500);
    }
    assert.equal(run.state, 'running', run.detail);
    for (let n = 0; n < 80 && ['running', 'saving'].includes(run.state); n++) {
      await delay(500);
      run = await status();
    }
    assert.equal(run.state, 'incomplete');
    assert.equal(run.detail, 'Pi ended its session before the server recorded a dragon kill.');
    assert.ok(run.activity.some(event => event.type === 'tool_execution_end' && event.tool === 'mcp__minecraft__wait_for_action' && event.outcome === 'settled'));
    const directory = join(root, run.id);
    await stat(join(directory, 'server/world/level.dat'));
    assert.ok((await readdir(directory)).some(name => name.endsWith('.jsonl') && name !== 'activity.jsonl'));
    assert.match(await readFile(join(directory, 'server.log'), 'utf8'), /All dimensions are saved/);
  } finally {
    await app.close();
    if (process.env.MINECRAFT_KEEP_SMOKE === '1') console.log(root);
    else await rm(root, { recursive: true, force: true });
  }
});
