'use strict';

// Runs as a bounded worker and exposes only the per-invocation capability API to helper code.
const net = require('node:net');
const fs = require('node:fs');
const path = require('node:path');

async function main() {
  const socketPath = process.env.CAPABILITY_SOCKET || '/run/capability.sock';
  const timeoutMs = Math.max(100, Math.min(Number(process.argv[2] || 10000), 30000));
  const helperFile = process.argv[3] || 'helper.js';
  if (helperFile.includes('/') || helperFile.startsWith('.')) throw new Error('invalid helper filename');
  const helperDirectory = process.env.HELPER_DIRECTORY || '/work';
  const source = fs.readFileSync(path.join(helperDirectory, helperFile), 'utf8');
  if (Buffer.byteLength(source, 'utf8') > 32000) throw new Error('helper source exceeds size cap');
  const socket = net.createConnection(socketPath);
  socket.setTimeout(timeoutMs);
  let nextId = 0;
  const pending = new Map();
  const pendingCalls = new Set();
  let buffered = '';
  socket.on('data', (chunk) => {
    buffered += chunk.toString('utf8');
    if (Buffer.byteLength(buffered, 'utf8') > 1000000) socket.destroy(new Error('capability response too large'));
    let newline;
    while ((newline = buffered.indexOf('\n')) >= 0) {
      const line = buffered.slice(0, newline); buffered = buffered.slice(newline + 1);
      let response;
      try { response = JSON.parse(line); } catch { socket.destroy(new Error('invalid capability response')); return; }
      const resolve = pending.get(response.request_id);
      if (resolve) { pending.delete(response.request_id); resolve(response); }
    }
  });
  await new Promise((resolve, reject) => { socket.once('connect', resolve); socket.once('error', reject); });
  const call = (method, params = {}) => {
    const request_id = String(++nextId);
    const request = new Promise((resolve, reject) => {
      const timer = setTimeout(() => { pending.delete(request_id); reject(new Error('capability call timed out')); }, timeoutMs);
      pending.set(request_id, (response) => { clearTimeout(timer); response.error ? reject(new Error(response.error.message)) : resolve(response.result); });
      socket.write(`${JSON.stringify({ version: 1, request_id, method, params })}\n`);
    });
    pendingCalls.add(request);
    request.then(() => pendingCalls.delete(request), () => pendingCalls.delete(request));
    request.catch(() => {});
    return request;
  };
  const api = Object.freeze({ observe: () => call('observe'), act: (action) => call('execute', { action_id: `helper-${Date.now()}-${nextId}`, action, timeout_seconds: 3 }) });
  const helper = new Function('api', `'use strict'; return (async () => { ${source}\n })();`);
  await Promise.race([(async () => { await helper(api); await Promise.all(pendingCalls); })(), new Promise((_, reject) => setTimeout(() => reject(new Error('helper time budget exceeded')), timeoutMs))]);
  socket.end();
}

main().then(() => process.exit(0), (error) => { process.stderr.write(`${String(error && error.message || error).slice(0, 500)}\n`); process.exit(1); });
