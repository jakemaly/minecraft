#!/usr/bin/env node
'use strict';

const readline = require('node:readline');
const net = require('node:net');
const fs = require('node:fs');
const path = require('node:path');
const mineflayer = require('mineflayer');
const { pathfinder } = require('mineflayer-pathfinder');
const { createProgramRunner } = require('./program_runner');
const { deathEvidence } = require('./death_context');
const { mineflayer: startViewer } = require('prismarine-viewer');

const host = process.env.MINECRAFT_HOST || '127.0.0.1';
const port = Number(process.env.MINECRAFT_PORT || 25565);
const username = process.env.MINECRAFT_USERNAME || 'Agent';
const version = process.env.MINECRAFT_VERSION || '1.21.4';
const runId = process.env.RUN_ID || 'local-smoke';
const maxObservationEntities = 32;
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
if (!Number.isInteger(port) || port < 1 || port > 65535) {
  process.stderr.write('Invalid bridge port.\n');
  process.exit(2);
}

const bot = mineflayer.createBot({ host, port, username, version, auth: 'offline', hideErrors: false });
bot.loadPlugin(pathfinder);
const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
const recentEvents = [];
let lastObservedState = null;
let ready = false;
let handling = Promise.resolve();
const completedAdvancements = new Set();
let programServer = null;
let viewerServer = null;
let viewerReady = false;

function releaseControls() {
  bot.clearControlStates();
  if (bot.pathfinder && typeof bot.pathfinder.stop === 'function') bot.pathfinder.stop();
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
bot.on('death', () => remember('death', deathEvidence(lastObservedState)));
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
  const equipmentSlots = ['hand', 'off_hand', 'feet', 'legs', 'torso', 'head'];
  const equipment = Object.fromEntries(equipmentSlots.map((slot, index) => {
    const item = bot.entity.equipment?.[index];
    return [slot, item ? { name: item.name, count: item.count } : null];
  }));
  const timeOfDay = bot.time?.timeOfDay ?? null;
  const observation = {
    schema_version: 3,
    observed_at: new Date().toISOString(),
    world: {
      dimension: bot.game.dimension, game_mode: bot.game.gameMode, difficulty: bot.game.difficulty,
      time_of_day_ticks: timeOfDay, is_day: timeOfDay === null ? null : timeOfDay < 12_000,
    },
    player: {
      username, position: { x: position.x, y: position.y, z: position.z }, yaw: bot.entity.yaw, pitch: bot.entity.pitch,
      health: bot.health, food: bot.food, oxygen: bot.oxygenLevel, experience: bot.experience,
      on_ground: bot.entity.onGround, equipment,
    },
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
  lastObservedState = observation;
  return observation;
}

function verifyGoal(goal = 'diamond_pilot') {
  if (goal === 'diamond_pilot') {
    const diamonds = bot.inventory.items().filter((item) => item.name === 'diamond').reduce((sum, item) => sum + item.count, 0);
    return { status: diamonds > 0 ? 'passed' : 'incomplete', criterion: 'at least one diamond in the agent inventory', inventory_diamonds: diamonds, source: 'Mineflayer inventory synchronized from server state' };
  }
  if (goal === 'dragon_kill') {
    const completed = completedAdvancements.has('minecraft:end/kill_dragon');
    return { status: completed ? 'passed' : 'incomplete', criterion: 'minecraft:end/kill_dragon advancement completed', advancement: completed ? 'minecraft:end/kill_dragon' : null, source: 'server advancement packet' };
  }
  throw new Error('unsupported goal');
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

async function handle(request) {
  if (!request || request.version !== 1 || typeof request.request_id !== 'string' || typeof request.method !== 'string' || !request.params || typeof request.params !== 'object') throw new Error('invalid version 1 request envelope');
  if (!ready && request.method !== 'stop') throw new Error('bot is not ready');
  if (request.method === 'observe') return boundedObservation();
  if (request.method === 'verify_goal') return verifyGoal(request.params.goal || 'diamond_pilot');
  if (request.method === 'release_controls') {
    const program = programRunner.cancelProgram();
    releaseControls();
    return { ...program, controls_released: true };
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
