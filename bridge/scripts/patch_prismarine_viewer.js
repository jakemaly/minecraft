#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const path = require('node:path');

const packagePath = require.resolve('prismarine-viewer/package.json');
const metadata = JSON.parse(fs.readFileSync(packagePath, 'utf8'));
if (metadata.version !== '1.33.0') throw new Error(`expected prismarine-viewer@1.33.0, found ${metadata.version}`);
const sourcePath = path.join(path.dirname(packagePath), 'lib/mineflayer.js');
let source = fs.readFileSync(sourcePath, 'utf8');
const entitiesPath = path.join(path.dirname(packagePath), 'viewer/lib/entities.js');
let entitiesSource = fs.readFileSync(entitiesPath, 'utf8');
const originalSignature = "module.exports = (bot, { viewDistance = 6, firstPerson = false, port = 3000, prefix = '' }) => {";
const patchedSignature = "module.exports = (bot, { viewDistance = 6, firstPerson = false, port = 3000, prefix = '', host = '127.0.0.1', onListening }) => {";
if (source.includes(originalSignature)) source = source.replace(originalSignature, patchedSignature);
if (source.includes('http.listen(port, () => {')) {
  source = source.replace(
    "http.listen(port, () => {\n    console.log(`Prismarine viewer web server running on *:${port}`)\n  })",
    "http.listen(port, host, () => {\n    process.stderr.write(`Prismarine viewer listening on ${host}:${port}\\n`)\n    if (onListening) onListening()\n  })",
  );
}
if (!source.includes('http.listen(port, host, () => {') || !source.includes('onListening')) {
  throw new Error('could not apply the pinned Prismarine Viewer host-binding patch');
}
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
fs.writeFileSync(entitiesPath, entitiesSource);
