'use strict';

const crypto = require('node:crypto');

function createProgramRunner({ bot, isReady, remember, requireFn = require, globalObject = global }) {
  let activeProgram = null;

  function releaseControls(controlKeys = []) {
    if (bot.pathfinder && typeof bot.pathfinder.stop === 'function') bot.pathfinder.stop();
    for (const key of controlKeys) bot.setControlState(key, false);
    bot.clearControlStates();
  }

  async function runProgram(request) {
    if (activeProgram) throw new Error('another Mineflayer program is already running');
    if (!isReady()) throw new Error('bot is not ready');
    const source = request.source;
    const timeoutSeconds = Number(request.timeout_seconds ?? 60);
    if (typeof source !== 'string' || !source.trim() || Buffer.byteLength(source, 'utf8') > 256_000) throw new Error('program source must be non-empty and at most 256 KiB');
    if (!Number.isFinite(timeoutSeconds) || timeoutSeconds < 1 || timeoutSeconds > 43200) throw new Error('program timeout must be between 1 second and 12 hours');

    const controller = new AbortController();
    const record = { id: crypto.randomUUID(), script: String(request.script || 'inline.js').slice(0, 200), controller, timedOut: false };
    activeProgram = record;
    const logs = [];
    const scopedConsole = Object.freeze(Object.fromEntries(['log', 'info', 'warn', 'error', 'debug'].map((method) => [method, (...args) => {
      logs.push({ method, text: args.map((arg) => typeof arg === 'string' ? arg : JSON.stringify(arg)).join(' ').slice(0, 4000) });
      if (logs.length > 100) logs.shift();
    }])));
    const controlKeys = new Set(['forward', 'back', 'left', 'right', 'jump', 'sprint', 'sneak']);
    const assertActive = () => {
      if (controller.signal.aborted) throw new Error('program was cancelled');
    };
    const guardedPathfinder = bot.pathfinder && new Proxy(bot.pathfinder, {
      get(target, property) {
        const value = Reflect.get(target, property, target);
        if (typeof value !== 'function') return value;
        return (...args) => { assertActive(); return value.apply(target, args); };
      },
    });
    const programBot = new Proxy(bot, {
      get(target, property) {
        if (property === 'pathfinder' && guardedPathfinder) return guardedPathfinder;
        const value = Reflect.get(target, property, target);
        if (typeof value !== 'function') return value;
        if (property === 'setControlState') return (key, enabled) => {
          assertActive();
          const result = value.call(target, key, enabled);
          if (enabled) controlKeys.add(key); else controlKeys.delete(key);
          return result;
        };
        return (...args) => { assertActive(); return value.apply(target, args); };
      },
    });
    const started = performance.now();
    let timer;
    let status = 'completed';
    try {
      const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
      const moduleObject = { exports: {} };
      const executeProgram = new AsyncFunction('bot', 'require', 'global', 'console', 'signal', 'module', 'exports', `'use strict';\n${source}\n`);
      const cancellation = new Promise((_, reject) => {
        controller.signal.addEventListener('abort', () => {
          const error = new Error(record.timedOut ? 'program deadline exceeded' : 'program cancelled');
          error.name = record.timedOut ? 'TimeoutError' : 'AbortError';
          reject(error);
        }, { once: true });
      });
      const deadline = new Promise((_, reject) => {
        timer = setTimeout(() => {
          record.timedOut = true;
          controller.abort();
          const error = new Error('program deadline exceeded');
          error.name = 'TimeoutError';
          reject(error);
        }, timeoutSeconds * 1000);
      });
      const result = await Promise.race([
        (async () => {
          const bodyResult = await executeProgram(programBot, requireFn, globalObject, scopedConsole, controller.signal, moduleObject, moduleObject.exports);
          const exported = moduleObject.exports;
          if (typeof exported === 'function') return await exported(programBot);
          if (exported && typeof exported.run === 'function') return await exported.run(programBot);
          return bodyResult;
        })(),
        cancellation,
        deadline,
      ]);
      let serializableResult = null;
      if (result !== undefined) {
        try { serializableResult = JSON.parse(JSON.stringify(result)); } catch { serializableResult = String(result); }
      }
      return { status, program_id: record.id, script: record.script, sha256: crypto.createHash('sha256').update(source).digest('hex'), output: logs, result: serializableResult, duration_seconds: (performance.now() - started) / 1000, controls_released: true };
    } catch (error) {
      status = record.timedOut ? 'timeout' : controller.signal.aborted ? 'cancelled' : 'failed';
      if (!controller.signal.aborted) controller.abort();
      releaseControls(controlKeys);
      return { status, program_id: record.id, script: record.script, sha256: crypto.createHash('sha256').update(source).digest('hex'), output: logs, error_type: error.constructor?.name || 'Error', error: String(error.message || error).slice(0, 500), duration_seconds: (performance.now() - started) / 1000, controls_released: true };
    } finally {
      clearTimeout(timer);
      releaseControls(controlKeys);
      if (activeProgram === record) activeProgram = null;
      remember('program_finished', { program_id: record.id, script: record.script, status, controls_released: true });
    }
  }

  function cancelProgram() {
    const record = activeProgram;
    if (record && !record.controller.signal.aborted) record.controller.abort();
    releaseControls();
    return { status: record ? 'cancellation_requested' : 'idle', program_id: record?.id || null, controls_released: true };
  }

  return {
    runProgram,
    cancelProgram,
    get activeProgram() { return activeProgram; },
  };
}

module.exports = { createProgramRunner };
