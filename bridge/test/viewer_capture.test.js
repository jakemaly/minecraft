'use strict';

const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const { createCanvas } = require('@napi-rs/canvas');
const { captureViewerImage, handoffViewerImage, inspectViewerFrame, steerViewerImage, waitForPageTarget } = require('../src/viewer_capture.mjs');

const root = path.resolve(__dirname, '..');

test('rejects a valid PNG containing only the viewer sky', async () => {
  const canvas = createCanvas(384, 216);
  const context = canvas.getContext('2d');
  context.fillStyle = '#add8e6';
  context.fillRect(0, 0, 384, 216);
  await assert.rejects(inspectViewerFrame(canvas.toBuffer('image/png')), /incomplete/);
});

test('waits for Chrome to publish its first page target after DevTools starts', async () => {
  let calls = 0;
  const page = { type: 'page', url: 'about:blank', webSocketDebuggerUrl: 'ws://127.0.0.1/devtools/page/1' };
  const target = await waitForPageTarget(9222, {
    timeoutMs: 100,
    intervalMs: 1,
    fetchImpl: async () => ({ ok: true, json: async () => (++calls === 1 ? [] : [page]) }),
  });
  assert.equal(target, page);
  assert.equal(calls, 2);
});

test('reports a useful error when Chrome never publishes a page target', async () => {
  await assert.rejects(
    waitForPageTarget(9222, { timeoutMs: 5, intervalMs: 1, fetchImpl: async () => ({ ok: true, json: async () => [] }) }),
    /did not expose a page target: page target list was empty/,
  );
});

test('captures rendered terrain and hands each changing frame to Pi', { timeout: 120_000 }, async () => {
  const viewer = require('prismarine-viewer').mineflayer;
  const registry = require('prismarine-registry')('1.21.4');
  const Chunk = require('prismarine-chunk')(registry);
  const { Vec3 } = require('vec3');
  const chunk = new Chunk({ minY: -64, worldHeight: 384 });
  for (let x = 0; x < 16; x++) {
    for (let z = 0; z < 16; z++) {
      chunk.setBlockStateId(new Vec3(x, 61, z), registry.blocksByName.stone.defaultState);
      chunk.setBlockStateId(new Vec3(x, 62, z), registry.blocksByName.dirt.defaultState);
      chunk.setBlockStateId(new Vec3(x, 63, z), registry.blocksByName.grass_block.defaultState);
    }
  }
  for (let y = 64; y < 70; y++) chunk.setBlockStateId(new Vec3(12, y, 8), registry.blocksByName.redstone_block.defaultState);
  const world = { getColumnAt: async () => ({ toJson: () => chunk.toJson() }) };
  const bot = new EventEmitter();
  bot.version = '1.21.4';
  bot.username = 'CaptureFixture';
  bot.world = world;
  bot.entities = {};
  bot.entity = { position: new Vec3(8, 64, 8), yaw: 0, pitch: 0 };
  let listening;
  const listeningPromise = new Promise((resolve) => { listening = resolve; });
  const server = viewer(bot, {
    host: '127.0.0.1', port: 0, viewDistance: 1, firstPerson: true,
    onListening: listening,
  });
  await listeningPromise;
  const address = server.address();
  const temp = await fs.mkdtemp(path.join(os.tmpdir(), 'minecraft-viewer-test-'));
  const model = {
    received: [],
    agent: { state: { messages: [] } },
    async prompt(task, options) {
      this.received.push(Buffer.from(options.images[0].data, 'base64'));
      this.agent.state.messages.push({ role: 'user', content: [{ type: 'text', text: task }, ...options.images] });
    },
    async steer(task, images) {
      this.received.push(Buffer.from(images[0].data, 'base64'));
      this.agent.state.messages.push({ role: 'user', content: [{ type: 'text', text: task }, ...images] });
    },
  };
  try {
    const capture = async (name) => captureViewerImage({
      viewerUrl: `http://127.0.0.1:${address.port}/`,
      target: path.join(temp, name), chromeHome: path.join(temp, 'chrome'),
    });
    const first = await capture('first.png');
    await handoffViewerImage(model, 'inspect this frame', first);

    // A pose change models a completed Mineflayer action and must alter the next captured view.
    bot.entity.yaw = Math.PI / 2;
    bot.emit('move');
    const second = await capture('second.png');
    await steerViewerImage(model, 'inspect the updated frame', second);

    assert.ok(first.audit.non_sky_coverage >= 0.08, 'first frame must contain rendered terrain');
    assert.ok(second.audit.non_sky_coverage >= 0.08, 'updated frame must contain rendered terrain');
    assert.notEqual(first.sha256, second.sha256, 'frame must update after the bot pose changes');
    assert.deepEqual(model.received, [first.image, second.image], 'the exact captured PNG bytes must reach the model');
    assert.equal(model.agent.state.messages.flatMap((message) => message.content).filter((part) => part.type === 'image').length, 1, 'the model context must retain only the latest image pixels');
  } finally {
    bot.viewer?.close?.();
    await new Promise((resolve) => server.close(resolve));
    await fs.rm(temp, { recursive: true, force: true });
  }
});
