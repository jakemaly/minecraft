#!/usr/bin/env node
'use strict';

const readline = require('node:readline');
const net = require('node:net');
const fs = require('node:fs');
const path = require('node:path');
const mineflayer = require('mineflayer');
const { Vec3 } = require('vec3');
const { pathfinder } = require('mineflayer-pathfinder');
const { createProgramRunner } = require('./program_runner');
const { mineflayer: startViewer } = require('prismarine-viewer');

const host = process.env.MINECRAFT_HOST || '127.0.0.1';
const port = Number(process.env.MINECRAFT_PORT || 25565);
const username = process.env.MINECRAFT_USERNAME || 'Agent';
const version = process.env.MINECRAFT_VERSION || '1.21.4';
const runId = process.env.RUN_ID || 'local-smoke';
const maxActionMs = Number(process.env.MAX_ACTION_MS || 2000);
const maxObservationEntities = 32;
const maxViewerBlocks = 512;
const programSocketPath = process.env.MINECRAFT_AGENT_SOCKET;

const isTailnetHost = (() => {
  if (host.startsWith('fd7a:115c:a1e0:')) return true;
  const parts = host.split('.').map(Number);
  if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) return false;
  const value = (((parts[0] << 24) >>> 0) | (parts[1] << 16) | (parts[2] << 8) | parts[3]) >>> 0;
  return value >= 0x64400000 && value <= 0x647fffff;
})();
const isPrivateDockerServer = host === 'minecraft-server' && process.env.MINECRAFT_ALLOW_INTERNAL_SERVER === 'true';
if (!['127.0.0.1', 'localhost', '::1'].includes(host) && !isPrivateDockerServer && !(process.env.MINECRAFT_ALLOW_TAILSCALE_HOST === 'true' && isTailnetHost)) {
  process.stderr.write('Refusing a non-loopback Minecraft host outside the Tailscale address range.\n');
  process.exit(2);
}
if (!Number.isInteger(port) || port < 1 || port > 65535 || !Number.isInteger(maxActionMs) || maxActionMs < 1 || maxActionMs > 5000) {
  process.stderr.write('Invalid bridge port or action limit.\n');
  process.exit(2);
}

const bot = mineflayer.createBot({ host, port, username, version, auth: 'offline', hideErrors: false });
bot.loadPlugin(pathfinder);
const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
const recentEvents = [];
const completedActions = new Map();
let ready = false;
let viewerSequence = Number(process.env.MINECRAFT_VIEWER_SEQUENCE_OFFSET || 0);
if (!Number.isSafeInteger(viewerSequence) || viewerSequence < 0) viewerSequence = 0;
let handling = Promise.resolve();
let lastPhysicsTick = performance.now();
const physicsTickIntervals = [];
const placedDiamondBlocks = [];
const completedAdvancements = new Set();
let programServer = null;
let viewerServer = null;
let viewerReady = false;

function releaseControls() {
  bot.clearControlStates();
  if (bot.pathfinder && typeof bot.pathfinder.stop === 'function') bot.pathfinder.stop();
}

function rememberDiamondPlacement(oldBlock, newBlock) {
  if (!newBlock || newBlock.name !== 'diamond_block' || (oldBlock && oldBlock.name === 'diamond_block')) return;
  const position = newBlock.position || oldBlock?.position;
  if (!position) return;
  const placed = { x: position.x, y: position.y, z: position.z, at: new Date().toISOString(), source: programRunner.activeProgram?.script || 'mineflayer-control' };
  placedDiamondBlocks.push(placed);
  if (placedDiamondBlocks.length > 100) placedDiamondBlocks.shift();
  remember('diamond_block_placed', placed);
}

function emit(value) {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

function remember(type, data = {}) {
  recentEvents.push({ at: new Date().toISOString(), type, ...data });
  if (recentEvents.length > 50) recentEvents.shift();
}

bot.on('login', () => remember('login', { username }));
bot.on('health', () => remember('health', { health: bot.health, food: bot.food }));
bot.on('blockUpdate', rememberDiamondPlacement);
bot._client.on('advancements', (packet) => {
  const entries = Array.isArray(packet.progressMapping)
    ? packet.progressMapping
    : Object.entries(packet.progressMapping || {}).map(([key, value]) => ({ key, value }));
  for (const entry of entries) {
    const key = entry && (entry.key || entry.id);
    if (key !== 'minecraft:end/kill_dragon') continue;
    const values = Array.isArray(entry.value) ? entry.value : Object.values(entry.value || {});
    if (values.some((progress) => progress && progress.criterionProgress !== null && progress.criterionProgress !== undefined)) {
      if (!completedAdvancements.has(key)) remember('advancement_completed', { id: key, title: 'Free the End' });
      completedAdvancements.add(key);
    }
  }
});
bot.on('physicsTick', () => {
  const now = performance.now();
  physicsTickIntervals.push(now - lastPhysicsTick);
  lastPhysicsTick = now;
  while (physicsTickIntervals.length > 200) physicsTickIntervals.shift();
});
bot.on('death', () => remember('death'));
bot.on('end', () => {
  ready = false;
  releaseControls();
  remember('disconnect');
});
bot.on('kicked', (reason) => remember('kicked', { reason: String(reason).slice(0, 300) }));
bot.on('error', (error) => {
  process.stderr.write(`Mineflayer error: ${String(error && error.message || error)}\n`);
  remember('error', { message: String(error && error.message || error).slice(0, 300) });
});
bot.once('spawn', () => {
  ready = true;
  remember('spawn', { dimension: bot.game.dimension });
  if (process.env.MINECRAFT_VIEWER_ENABLED === 'true') {
    const viewerHost = process.env.MINECRAFT_VIEWER_HOST || '127.0.0.1';
    const viewerPort = Number(process.env.MINECRAFT_VIEWER_PORT || 3000);
    viewerServer = startViewer(bot, {
      firstPerson: true,
      viewDistance: Number(process.env.MINECRAFT_VIEWER_DISTANCE || 6),
      port: viewerPort,
      host: viewerHost,
      onListening: () => {
        viewerReady = true;
        emit({ event: 'viewer_ready', run_id: runId, host: viewerHost, port: viewerPort, renderer: 'prismarine-viewer@1.33.0', first_person: true });
      },
    });
  }
  emit({ event: 'ready', run_id: runId, minecraft_version: bot.version, username, player_uuid: bot.player?.uuid || bot.entity?.uuid || null, pathfinder: true, first_person_capture: 'prismarine-viewer' });
});

function boundedObservation() {
  const position = bot.entity.position;
  const entities = Object.values(bot.entities)
    .filter((entity) => entity && entity.id !== bot.entity.id && entity.position)
    .map((entity) => ({ id: entity.id, type: entity.type, name: entity.name || null, username: entity.username || null, position: { x: entity.position.x, y: entity.position.y, z: entity.position.z }, distance: entity.position.distanceTo(position) }))
    .sort((a, b) => a.distance - b.distance)
    .slice(0, maxObservationEntities);
  const blocks = bot.findBlocks({ matching: (block) => block && block.name !== 'air', maxDistance: 8, count: 80 })
    .map((point) => {
      const block = bot.blockAt(point);
      return block && { name: block.name, position: { x: point.x, y: point.y, z: point.z }, distance: point.distanceTo(position), source: 'mineflayer_loaded_region' };
    }).filter(Boolean);
  const nearbyLogs = bot.findBlocks({ matching: (block) => block && /_(log|stem)$/.test(block.name), maxDistance: 24, count: 16 })
    .map((point) => {
      const block = bot.blockAt(point);
      return block && { name: block.name, position: { x: point.x, y: point.y, z: point.z }, distance: point.distanceTo(position), source: 'mineflayer_loaded_region' };
    }).filter(Boolean);
  const inventory = bot.inventory.items().map((item) => ({ name: item.name, count: item.count, slot: item.slot }));
  return {
    schema_version: 2,
    observed_at: new Date().toISOString(),
    world: { dimension: bot.game.dimension, game_mode: bot.game.gameMode, difficulty: bot.game.difficulty },
    player: { username, position: { x: position.x, y: position.y, z: position.z }, yaw: bot.entity.yaw, pitch: bot.entity.pitch, health: bot.health, food: bot.food, experience: bot.experience, on_ground: bot.entity.onGround },
    inventory,
    inventory_summary: inventory.reduce((summary, item) => { summary[item.name] = (summary[item.name] || 0) + item.count; return summary; }, {}),
    nearby_entities: entities,
    nearby_blocks: blocks,
    nearby_logs: nearbyLogs,
    data_access: { blocks: 'Mineflayer loaded region; may include occluded blocks', entities: 'Mineflayer loaded entities' },
    units: { position: 'world blocks (x,y,z)', distance: 'blocks', yaw: 'radians', pitch: 'radians', health: 'hearts', food: 'points', experience: 'points' },
    empty_states: { inventory: inventory.length === 0, nearby_entities: entities.length === 0, nearby_blocks: blocks.length === 0, nearby_logs: nearbyLogs.length === 0 },
    pathfinder: { loaded: Boolean(bot.pathfinder), goal: bot.pathfinder?.goal?.constructor?.name || null, is_moving: Boolean(bot.pathfinder?.isMoving?.()) },
    recent_events: recentEvents.slice(-20),
    available_primitives: ['observe', 'look', 'move', 'wait', 'dig', 'place', 'equip', 'use_item', 'craft', 'interact', 'attack']
  };
}

function verifyGoal(goal = 'diamond_block_placed') {
  if (goal === 'diamond_pilot') {
    const diamonds = bot.inventory.items().filter((item) => item.name === 'diamond').reduce((sum, item) => sum + item.count, 0);
    return { status: diamonds > 0 ? 'passed' : 'incomplete', criterion: 'at least one diamond in the agent inventory', inventory_diamonds: diamonds, source: 'Mineflayer inventory synchronized from server state' };
  }
  if (goal === 'dragon_kill') {
    const completed = completedAdvancements.has('minecraft:end/kill_dragon');
    return { status: completed ? 'passed' : 'incomplete', criterion: 'minecraft:end/kill_dragon advancement completed', advancement: completed ? 'minecraft:end/kill_dragon' : null, source: 'server advancement packet' };
  }
  const inventoryBlocks = bot.inventory.items().filter((item) => item.name === 'diamond_block').reduce((sum, item) => sum + item.count, 0);
  const placed = [...placedDiamondBlocks].reverse().find((entry) => {
    const current = bot.blockAt(new Vec3(entry.x, entry.y, entry.z));
    return current && current.name === 'diamond_block';
  }) || null;
  return { status: placed ? 'passed' : 'incomplete', criterion: 'a diamond block placed by the agent is present in the world', placed_block: placed, inventory_diamond_blocks: inventoryBlocks };
}

const programRunner = createProgramRunner({ bot, isReady: () => ready, remember });

function startProgramSocket() {
  if (!programSocketPath) return;
  fs.mkdirSync(path.dirname(programSocketPath), { recursive: true, mode: 0o700 });
  try { fs.unlinkSync(programSocketPath); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  const server = net.createServer((connection) => {
    let buffer = '';
    connection.setEncoding('utf8');
    connection.on('data', async (chunk) => {
      buffer += chunk;
      if (Buffer.byteLength(buffer, 'utf8') > 300_000) { connection.end(JSON.stringify({ status: 'error', error: 'request exceeds 300 KiB' })); return; }
      const newline = buffer.indexOf('\n');
      if (newline < 0) return;
      const line = buffer.slice(0, newline);
      buffer = '';
      let request;
      try {
        request = JSON.parse(line);
        if (request.version !== 1 || typeof request.method !== 'string') throw new Error('invalid bridge socket request');
        if (request.method === 'run_program' && !request.request_id) {
          const result = await programRunner.runProgram(request);
          connection.end(JSON.stringify(result));
          return;
        }
        const result = await handle(request);
        connection.end(JSON.stringify({ version: 1, request_id: request.request_id, result }));
      } catch (error) {
        if (request?.method === 'run_program' && !request.request_id) {
          connection.end(JSON.stringify({ status: 'error', error_type: error.constructor?.name || 'Error', error: String(error.message || error).slice(0, 500) }));
        } else {
          connection.end(JSON.stringify({ version: 1, request_id: request?.request_id || null, error: { code: error.constructor?.name || 'Error', message: String(error.message || error).slice(0, 500) } }));
        }
      }
    });
  });
  server.listen(programSocketPath, () => fs.chmodSync(programSocketPath, 0o600));
  server.on('close', () => { try { fs.unlinkSync(programSocketPath); } catch {} });
  return server;
}

programServer = startProgramSocket();

function durationMs(value) {
  const ms = Number(value);
  if (!Number.isFinite(ms) || ms < 1 || ms > maxActionMs) throw new Error(`duration_ms must be between 1 and ${maxActionMs}`);
  return Math.floor(ms);
}

async function execute(action, { includeObservation = true } = {}) {
  if (!action || typeof action !== 'object' || typeof action.type !== 'string') throw new Error('action must have a type');
  switch (action.type) {
    case 'look': {
      const yaw = Number(action.yaw); const pitch = Number(action.pitch);
      if (!Number.isFinite(yaw) || !Number.isFinite(pitch) || Math.abs(yaw) > Math.PI || Math.abs(pitch) > Math.PI / 2) throw new Error('look requires bounded radians yaw and pitch');
      await bot.look(yaw, pitch, true);
      break;
    }
    case 'move': {
      const keys = action.keys;
      const allowed = new Set(['forward', 'back', 'left', 'right', 'jump', 'sprint', 'sneak']);
      if (!Array.isArray(keys) || keys.length < 1 || keys.some((key) => !allowed.has(key))) throw new Error('move keys must be a non-empty list of supported controls');
      const ms = durationMs(action.duration_ms);
      try {
        for (const key of keys) bot.setControlState(key, true);
        await new Promise((resolve) => setTimeout(resolve, ms));
      } finally {
        for (const key of allowed) bot.setControlState(key, false);
      }
      break;
    }
    case 'wait':
      await new Promise((resolve) => setTimeout(resolve, durationMs(action.duration_ms)));
      break;
    case 'dig': {
      const pos = action.position;
      if (![pos && pos.x, pos && pos.y, pos && pos.z].every(Number.isInteger)) throw new Error('dig position must contain integer x, y, z');
      const point = bot.blockAt(new Vec3(pos.x, pos.y, pos.z));
      if (!point || point.name === 'air' || bot.entity.position.distanceTo(point.position) > 5) throw new Error('target block is unavailable or out of reach');
      await bot.dig(point, true);
      break;
    }
    case 'place': {
      const target = action.target;
      if (!target || ![target.x, target.y, target.z].every(Number.isInteger)) throw new Error('place target must contain integer x, y, z');
      if (bot.entity.position.distanceTo(new Vec3(target.x, target.y, target.z)) > 4) throw new Error('place target is out of range');
      const reference = bot.blockAt(new Vec3(target.x, target.y - 1, target.z));
      const item = bot.inventory.items().find((entry) => entry.name === action.item);
      if (!reference || !item) throw new Error('reference block or requested item is unavailable');
      await bot.equip(item, 'hand');
      await bot.placeBlock(reference, new (require('vec3').Vec3)(0, 1, 0));
      break;
    }
    case 'equip': {
      const item = bot.inventory.items().find((entry) => entry.name === action.item);
      if (!item) throw new Error('requested item is not in inventory');
      await bot.equip(item, action.destination || 'hand');
      break;
    }
    case 'use_item': {
      if (!bot.heldItem || (action.item && bot.heldItem.name !== action.item)) throw new Error('requested item is not held');
      bot.activateItem(Boolean(action.offhand));
      await new Promise((resolve) => setTimeout(resolve, durationMs(action.duration_ms || 500)));
      bot.deactivateItem();
      break;
    }
    case 'craft': {
      const mcData = require('minecraft-data')(bot.version);
      const item = mcData.itemsByName[action.item];
      if (!item) throw new Error('unknown recipe output');
      const recipes = bot.recipesFor(item.id, null, Number(action.count || 1), null);
      if (!recipes.length) throw new Error('no available recipe with current inventory and crafting table');
      await bot.craft(recipes[0], Number(action.count || 1), null);
      break;
    }
    case 'interact': {
      const entity = bot.entities[Number(action.entity_id)];
      if (!entity || !entity.position || entity.position.distanceTo(bot.entity.position) > 4) throw new Error('entity is not currently available and in range');
      await bot.activateEntity(entity);
      break;
    }
    case 'attack': {
      const entity = bot.entities[Number(action.entity_id)];
      if (!entity || !entity.position || entity.position.distanceTo(bot.entity.position) > 4) throw new Error('entity is not currently available and in range');
      bot.attack(entity);
      await new Promise((resolve) => setTimeout(resolve, 250));
      bot.clearControlStates();
      break;
    }
    default:
      throw new Error(`unsupported action type: ${action.type}`);
  }
  const result = { status: 'completed' };
  if (includeObservation) result.observation = boundedObservation();
  return result;
}

function viewerSnapshot() {
  const started = performance.now();
  const center = bot.entity.position.floored();
  const position = bot.entity.position;
  const blocks = [];
  for (let y = -2; y <= 3 && blocks.length < maxViewerBlocks; y++) {
    for (let x = -4; x <= 4 && blocks.length < maxViewerBlocks; x++) {
      for (let z = -4; z <= 4 && blocks.length < maxViewerBlocks; z++) {
        const pos = center.offset(x, y, z);
        const block = bot.blockAt(pos);
        if (block && block.name !== 'air') blocks.push({ name: block.name, x: pos.x, y: pos.y, z: pos.z });
      }
    }
  }
  viewerSequence += 1;
  const sortedTicks = physicsTickIntervals.slice().sort((a, b) => a - b);
  const tickP95 = sortedTicks.length ? sortedTicks[Math.floor((sortedTicks.length - 1) * 0.95)] : null;
  return { sequence: viewerSequence, full_resync: true, center: { x: center.x, y: center.y, z: center.z }, player: { position: { x: position.x, y: position.y, z: position.z }, yaw: bot.entity.yaw, pitch: bot.entity.pitch, sneaking: Boolean(bot.getControlState('sneak')) }, blocks, dropped_stale_updates: 0, source: 'mineflayer_loaded_region', capture_ms: performance.now() - started, physics_tick_samples: sortedTicks.length, physics_tick_p95_ms: tickP95 };
}

async function handle(request) {
  if (!request || request.version !== 1 || typeof request.request_id !== 'string' || typeof request.method !== 'string' || !request.params || typeof request.params !== 'object') throw new Error('invalid version 1 request envelope');
  if (request.method === 'execute' && typeof request.params.action_id === 'string' && completedActions.has(request.params.action_id)) {
    return await completedActions.get(request.params.action_id);
  }
  if (!ready && request.method !== 'stop') throw new Error('bot is not ready');
  if (request.method === 'observe') return boundedObservation();
  if (request.method === 'viewer_snapshot') return viewerSnapshot();
  if (request.method === 'capture_image') return { status: 'not_available_in_bridge', source: 'marimo_prismarine_viewer_widget', message: 'The current JPEG frame is supplied by the embedded notebook widget.' };
  if (request.method === 'verify_goal') return verifyGoal(request.params.goal || 'diamond_block_placed');
  if (request.method === 'release_controls') {
    const program = programRunner.cancelProgram();
    releaseControls();
    return { ...program, controls_released: true };
  }
  if (request.method === 'cancel_program') {
    return programRunner.cancelProgram();
  }
  if (request.method === 'execute') {
    if (programRunner.activeProgram) throw new Error('a Mineflayer program is already running');
    const { action_id, action, timeout_seconds } = request.params;
    if (request.params.include_observation !== undefined && typeof request.params.include_observation !== 'boolean') throw new Error('include_observation must be boolean');
    const includeObservation = request.params.include_observation !== false;
    if (typeof action_id !== 'string' || action_id.length > 128) throw new Error('valid action_id required');
    const timeout = Number(timeout_seconds);
    if (!Number.isFinite(timeout) || timeout > 5 || timeout < 1) throw new Error('action deadline must be 1–5 seconds');
    const actionPromise = execute(action, { includeObservation });
    completedActions.set(action_id, actionPromise);
    let result;
    try {
      result = await Promise.race([actionPromise, new Promise((_, reject) => setTimeout(() => reject(new Error('action deadline exceeded')), timeout * 1000))]);
      result.action_id = action_id;
    } catch (error) {
      result = { status: 'unknown', action_id, error: String(error && error.message || error).slice(0, 300) };
      if (result.error === 'action deadline exceeded') {
        ready = false;
        bot.clearControlStates();
        if (bot.targetDigBlock) bot.stopDigging();
        bot.quit('action deadline exceeded; outcome recorded as unknown');
        setTimeout(() => process.exit(1), 100);
      }
    } finally {
      bot.clearControlStates();
    }
    result.controls_released = true;
    remember('action_result', { action_id, status: result.status, controls_released: true });
    completedActions.set(action_id, Promise.resolve(result));
    if (completedActions.size > 1024) completedActions.delete(completedActions.keys().next().value);
    return result;
  }
  if (request.method === 'stop') {
    ready = false;
    programRunner.cancelProgram();
    releaseControls();
    if (programServer) programServer.close();
    if (viewerServer && typeof viewerServer.close === 'function') viewerServer.close();
    bot.quit('operator shutdown');
    setTimeout(() => process.exit(0), 100);
    return { status: 'stopping' };
  }
  throw new Error(`unsupported method: ${request.method}`);
}

rl.on('line', (line) => {
  handling = handling.then(async () => {
    let request;
    try {
      if (Buffer.byteLength(line, 'utf8') > 1000000) throw new Error('request exceeds maximum size');
      request = JSON.parse(line);
      const result = await handle(request);
      emit({ version: 1, request_id: request.request_id, result });
    } catch (error) {
      emit({ version: 1, request_id: request && request.request_id || null, error: { code: 'request_failed', message: String(error && error.message || error).slice(0, 500) } });
      bot.clearControlStates();
    }
  });
});

process.on('SIGTERM', () => {
  programRunner.cancelProgram();
  releaseControls();
  if (programServer) programServer.close();
  if (viewerServer && typeof viewerServer.close === 'function') viewerServer.close();
  bot.quit('supervisor shutdown');
  setTimeout(() => process.exit(0), 150);
});
