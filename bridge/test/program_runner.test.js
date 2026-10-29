'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { createProgramRunner } = require('../src/program_runner');

function fixture() {
  const bot = {
    controls: [],
    clearCount: 0,
    pathfinder: { stopped: 0, stop() { this.stopped += 1; } },
    setControlState(key, enabled) { this.controls.push([key, enabled]); },
    clearControlStates() { this.clearCount += 1; },
    position() { return { x: 3, y: 64, z: -2 }; },
  };
  const events = [];
  const runner = createProgramRunner({ bot, isReady: () => true, remember: (...event) => events.push(event) });
  return { bot, events, runner };
}

test('runs a multi-step async program with persistent global state and bounded logs', async () => {
  const { bot, runner } = fixture();
  const first = await runner.runProgram({
    script: 'scripts/step.js',
    source: "global.__runnerCount = (global.__runnerCount || 0) + 1; bot.setControlState('forward', true); await new Promise(resolve => setTimeout(resolve, 5)); bot.setControlState('forward', false); console.log('step', global.__runnerCount); return bot.position();",
  });
  const second = await runner.runProgram({ source: "return global.__runnerCount;" });
  assert.equal(first.status, 'completed');
  assert.deepEqual(first.result, { x: 3, y: 64, z: -2 });
  assert.equal(first.output[0].text, 'step 1');
  assert.equal(second.result, 1);
  assert.deepEqual(bot.controls.slice(0, 2), [['forward', true], ['forward', false]]);
  assert.ok(bot.clearCount >= 2);
});

test('supports an exported async Mineflayer program with the live bot', async () => {
  const { bot, runner } = fixture();
  const result = await runner.runProgram({
    script: 'scripts/exported.js',
    source: "module.exports = async (bot) => { bot.setControlState('forward', true); await Promise.resolve(); bot.setControlState('forward', false); console.log('exported program', bot.position()); return 9; };",
  });
  assert.equal(result.status, 'completed');
  assert.equal(result.result, 9);
  assert.equal(result.output[0].text, 'exported program {\"x\":3,\"y\":64,\"z\":-2}');
  assert.deepEqual(bot.controls.slice(0, 2), [['forward', true], ['forward', false]]);
  assert.equal(result.controls_released, true);
});

test('stop cancels a running program promptly and releases controls', async () => {
  const { bot, runner } = fixture();
  const running = runner.runProgram({
    source: "bot.setControlState('forward', true); await new Promise(resolve => setTimeout(resolve, 100));",
  });
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(runner.cancelProgram().status, 'cancellation_requested');
  const result = await running;
  assert.equal(result.status, 'cancelled');
  assert.equal(result.controls_released, true);
  assert.equal(runner.activeProgram, null);
  assert.ok(bot.controls.some(([key, enabled]) => key === 'forward' && enabled === false));
});

test('deadline and program errors have distinct statuses and release controls', async () => {
  const { bot, runner } = fixture();
  const timeout = await runner.runProgram({
    source: "bot.setControlState('jump', true); await new Promise(resolve => setTimeout(resolve, 2000));",
    timeout_seconds: 1,
  });
  const failure = await runner.runProgram({ source: "throw new Error('script bug');" });
  assert.equal(timeout.status, 'timeout');
  assert.equal(failure.status, 'failed');
  assert.equal(timeout.controls_released, true);
  assert.equal(failure.error, 'script bug');
  assert.ok(bot.controls.some(([key, enabled]) => key === 'jump' && enabled === false));
});
