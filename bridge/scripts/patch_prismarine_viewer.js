#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const path = require('node:path');

const packagePath = require.resolve('prismarine-viewer/package.json');
const metadata = JSON.parse(fs.readFileSync(packagePath, 'utf8'));
if (metadata.version !== '1.33.0') throw new Error(`expected prismarine-viewer@1.33.0, found ${metadata.version}`);
const sourcePath = path.join(path.dirname(packagePath), 'lib/mineflayer.js');
let source = fs.readFileSync(sourcePath, 'utf8');
const clientSourcePath = path.join(path.dirname(packagePath), 'lib/index.js');
let clientSource = fs.readFileSync(clientSourcePath, 'utf8');
const entitiesPath = path.join(path.dirname(packagePath), 'viewer/lib/entities.js');
let entitiesSource = fs.readFileSync(entitiesPath, 'utf8');
const rendererPath = path.join(path.dirname(packagePath), 'viewer/lib/worldrenderer.js');
let rendererSource = fs.readFileSync(rendererPath, 'utf8');
const workerPath = path.join(path.dirname(packagePath), 'viewer/lib/worker.js');
let workerSource = fs.readFileSync(workerPath, 'utf8');
const workerBundlePath = path.join(path.dirname(packagePath), 'public/worker.js');
let workerBundle = fs.readFileSync(workerBundlePath, 'utf8');
const browserBundlePath = path.join(path.dirname(packagePath), 'public/index.js');
let browserBundle = fs.readFileSync(browserBundlePath, 'utf8');
const originalSignature = "module.exports = (bot, { viewDistance = 6, firstPerson = false, port = 3000, prefix = '' }) => {";
const patchedSignature = "module.exports = (bot, { viewDistance = 6, firstPerson = false, port = 3000, prefix = '', host = '127.0.0.1', onListening }) => {";
if (source.includes(originalSignature)) source = source.replace(originalSignature, patchedSignature);
const moveListener = "    bot.on('move', botPosition)";
if (source.includes(`${moveListener}\n    botPosition()`)) {
  // The browser otherwise stays at the origin until the bot's next movement event.
} else if (source.includes(moveListener)) {
  source = source.replace(moveListener, `${moveListener}\n    botPosition()`);
}
if (source.includes('http.listen(port, () => {')) {
  source = source.replace(
    "http.listen(port, () => {\n    console.log(`Prismarine viewer web server running on *:${port}`)\n  })",
    "http.listen(port, host, () => {\n    process.stderr.write(`Prismarine viewer listening on ${host}:${port}\\n`)\n    if (onListening) onListening()\n  })",
  );
}
if (!source.includes('http.listen(port, host, () => {') || !source.includes('onListening') || !source.includes(`${moveListener}\n    botPosition()`)) {
  throw new Error('could not apply the pinned Prismarine Viewer host-binding patch');
}
const originalWorldInit = 'worldView.init(bot.entity.position)';
const patchedWorldInit = "worldView.init(bot.entity.position).then(() => socket.emit('worldReady'))";
if (source.includes(originalWorldInit)) source = source.replace(originalWorldInit, patchedWorldInit);
if (!source.includes(patchedWorldInit)) throw new Error('could not add Prismarine Viewer world readiness event');
const clientViewer = 'const viewer = new Viewer(renderer)';
const clientReadiness = `${clientViewer}\nwindow.__minecraftViewerReady = false\nsocket.on('worldReady', () => {\n  viewer.waitForChunksToRender().then(() => {\n    window.requestAnimationFrame(() => window.requestAnimationFrame(() => { window.__minecraftViewerReady = true }))\n  })\n})`;
if (clientSource.includes(clientViewer) && !clientSource.includes('window.__minecraftViewerReady')) {
  clientSource = clientSource.replace(clientViewer, clientReadiness);
}
if (!clientSource.includes('viewer.waitForChunksToRender()') || !clientSource.includes('window.__minecraftViewerReady = true')) {
  throw new Error('could not make the pinned Prismarine browser client report rendered chunks');
}
const yRange = 'const minY = Number(this.version.split(\'.\')[1]) >= 18 ? -64 : 0\n    const maxY = minY < 0 ? 320 : 256\n    for (let y = minY; y < maxY; y += 16) {';
const originalYRange = 'for (let y = 0; y < 256; y += 16) {';
if (rendererSource.includes(originalYRange)) rendererSource = rendererSource.replaceAll(originalYRange, yRange);
if ((rendererSource.match(/for \(let y = minY; y < maxY; y \+= 16\) \{/g) || []).length !== 2) {
  throw new Error('could not patch pinned Prismarine Viewer world-height section scheduling');
}
const originalSectionIndex = 'chunk.sections[Math.floor(y / 16)]';
const patchedSectionIndex = 'chunk.sections[Math.floor(y / 16) - Math.floor((chunk.minY || 0) / 16)]';
if (workerSource.includes(originalSectionIndex)) workerSource = workerSource.replaceAll(originalSectionIndex, patchedSectionIndex);
if ((workerSource.match(/chunk\.sections\[Math\.floor\(y \/ 16\) - Math\.floor\(\(chunk\.minY \|\| 0\) \/ 16\)\]/g) || []).length !== 2) {
  throw new Error('could not patch pinned Prismarine Viewer worker source section indexing');
}
const originalWorkerIndexes = [
  ['i&&i.sections[Math.floor(a/16)]', 'i&&i.sections[Math.floor(a/16)-Math.floor((i.minY||0)/16)]'],
  ['l&&l.sections[Math.floor(n/16)]', 'l&&l.sections[Math.floor(n/16)-Math.floor((l.minY||0)/16)]'],
];
for (const [before, after] of originalWorkerIndexes) {
  if (workerBundle.includes(before)) workerBundle = workerBundle.replace(before, after);
  if (!workerBundle.includes(after)) throw new Error('could not patch pinned Prismarine Viewer worker bundle section indexing');
}
const originalChunkSchedule = 'for(let i=0;i<256;i+=16){const n=new r(t,i,e);';
const patchedChunkSchedule = 'const o=Number(this.version.split(".")[1])>=18?-64:0;for(let i=o;i<(o<0?320:256);i+=16){const n=new r(t,i,e);';
if (browserBundle.includes(originalChunkSchedule)) browserBundle = browserBundle.replaceAll(originalChunkSchedule, patchedChunkSchedule);
if (!browserBundle.includes(patchedChunkSchedule) || browserBundle.includes(originalChunkSchedule)) {
  throw new Error('could not patch pinned Prismarine Viewer browser bundle world-height scheduling');
}
const originalReadyHook = 'u.listen(o),o.on("position",';
const patchedReadyHook = 'u.listen(o),o.on("worldReady",(()=>{u.waitForChunksToRender().then((()=>{window.requestAnimationFrame((()=>window.requestAnimationFrame((()=>{window.__minecraftViewerReady=!0}))))}))})),o.on("position",';
if (browserBundle.includes(originalReadyHook)) browserBundle = browserBundle.replace(originalReadyHook, patchedReadyHook);
if (!browserBundle.includes(patchedReadyHook)) throw new Error('could not patch pinned Prismarine browser readiness bundle');
if (entitiesSource.includes("require('canvas')")) {
  const canvasMetadata = JSON.parse(fs.readFileSync(require.resolve('@napi-rs/canvas/package.json'), 'utf8'));
  if (canvasMetadata.version !== '1.0.13') throw new Error(`expected @napi-rs/canvas@1.0.13, found ${canvasMetadata.version}`);
  entitiesSource = entitiesSource.replaceAll("require('canvas')", "require('@napi-rs/canvas')");
}
if (!entitiesSource.includes("require('@napi-rs/canvas')")) {
  throw new Error('could not apply the pinned canvas compatibility patch');
}
if (!source.includes('return http\n')) {
  source = source.replace("  bot.viewer.close = () => {\n    http.close()\n    for (const socket of sockets) {\n      socket.disconnect()\n    }\n  }\n}", "  bot.viewer.close = () => {\n    http.close()\n    for (const socket of sockets) {\n      socket.disconnect()\n    }\n  }\n  return http\n}");
}
fs.writeFileSync(sourcePath, source);
fs.writeFileSync(clientSourcePath, clientSource);
fs.writeFileSync(entitiesPath, entitiesSource);
fs.writeFileSync(rendererPath, rendererSource);
fs.writeFileSync(workerPath, workerSource);
fs.writeFileSync(workerBundlePath, workerBundle);
fs.writeFileSync(browserBundlePath, browserBundle);
