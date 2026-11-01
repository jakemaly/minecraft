import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import fs from 'node:fs/promises';
import path from 'node:path';
import { performance } from 'node:perf_hooks';

const require = createRequire(import.meta.url);
const { createCanvas, loadImage } = require('@napi-rs/canvas');
const SKY = [173, 216, 230];
const MIN_SCENE_COVERAGE = 0.08;

export async function waitForPageTarget(debugPort, { fetchImpl = fetch, timeoutMs = 10_000, intervalMs = 50 } = {}) {
  const deadline = Date.now() + timeoutMs;
  let lastError = 'page target list was empty';
  while (Date.now() < deadline) {
    try {
      const response = await fetchImpl(`http://127.0.0.1:${debugPort}/json/list`);
      if (!response.ok) throw new Error(`DevTools returned HTTP ${response.status}`);
      const pages = await response.json();
      const page = pages.find((entry) => entry.type === 'page' && entry.url === 'about:blank')
        || pages.find((entry) => entry.type === 'page' && typeof entry.url === 'string' && !entry.url.startsWith('chrome-extension:'));
      if (page?.webSocketDebuggerUrl) return page;
    } catch (error) {
      lastError = error.message;
    }
    await new Promise((resolve) => setTimeout(resolve, Math.min(intervalMs, Math.max(1, deadline - Date.now()))));
  }
  throw new Error(`headless browser did not expose a page target: ${lastError}`);
}

export async function inspectViewerFrame(image) {
  const pngSignature = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
  if (!Buffer.isBuffer(image) || image.length < 64 || !image.subarray(0, 8).equals(pngSignature)) {
    throw new Error('Prismarine Viewer returned an empty or invalid PNG');
  }
  const bitmap = await loadImage(image);
  const canvas = createCanvas(bitmap.width, bitmap.height);
  const context = canvas.getContext('2d');
  context.drawImage(bitmap, 0, 0);
  const pixels = context.getImageData(0, 0, bitmap.width, bitmap.height).data;
  let nonSkyPixels = 0;
  for (let i = 0; i < pixels.length; i += 4) {
    const difference = Math.abs(pixels[i] - SKY[0]) + Math.abs(pixels[i + 1] - SKY[1]) + Math.abs(pixels[i + 2] - SKY[2]);
    if (difference > 45) nonSkyPixels++;
  }
  const coverage = nonSkyPixels / (bitmap.width * bitmap.height);
  if (coverage < MIN_SCENE_COVERAGE) throw new Error(`Prismarine Viewer frame is incomplete (${(coverage * 100).toFixed(3)}% non-sky pixels)`);
  return { width: bitmap.width, height: bitmap.height, non_sky_pixels: nonSkyPixels, non_sky_coverage: coverage };
}

export async function captureViewerImage({ viewerUrl, target, chromeHome, chrome = process.env.MINECRAFT_CHROME || 'google-chrome' }) {
  await fs.mkdir(path.dirname(target), { recursive: true, mode: 0o700 });
  await fs.mkdir(chromeHome || '/tmp', { recursive: true, mode: 0o700 });
  await fs.rm(path.join(chromeHome, 'DevToolsActivePort'), { force: true });
  const args = [
    '--headless=new', '--no-sandbox', '--disable-gpu-sandbox', '--disable-dev-shm-usage', '--disable-background-networking',
    '--hide-scrollbars', '--enable-webgl', '--enable-unsafe-swiftshader', '--use-gl=angle', '--use-angle=swiftshader',
    '--window-size=384,216', '--remote-debugging-port=0', `--user-data-dir=${chromeHome}`, 'about:blank',
  ];
  const begin = performance.now();
  const child = spawn(chrome, args, { stdio: ['ignore', 'ignore', 'pipe'], env: { PATH: process.env.PATH || '/usr/bin:/bin', HOME: chromeHome } });
  const childClosed = new Promise((resolve) => child.once('close', resolve));
  let tail = '';
  child.stderr.on('data', (chunk) => { tail = (tail + chunk.toString()).slice(-1500); });
  let socket;
  try {
    const activePortPath = path.join(chromeHome, 'DevToolsActivePort');
    let debugPort;
    const launchDeadline = Date.now() + 15_000;
    while (Date.now() < launchDeadline) {
      try { debugPort = Number((await fs.readFile(activePortPath, 'utf8')).split('\n')[0]); } catch {}
      if (debugPort) break;
      if (child.exitCode !== null) throw new Error(`headless browser exited ${child.exitCode}: ${tail.slice(-700)}`);
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    if (!debugPort) throw new Error(`headless browser did not start DevTools: ${tail.slice(-700)}`);
    const page = await waitForPageTarget(debugPort);
    const deadline = Date.now() + 45_000;
    socket = await openDevTools(page.webSocketDebuggerUrl);
    const send = createDevToolsSender(socket);
    await send('Page.enable');
    await send('Runtime.enable');
    await send('Emulation.setDeviceMetricsOverride', { width: 384, height: 216, deviceScaleFactor: 1, mobile: false });
    const navigation = await send('Page.navigate', { url: viewerUrl });
    if (navigation.error) throw new Error(`could not navigate to the Prismarine Viewer: ${navigation.error.message}`);
    let lastError = 'Viewer page did not finish loading';
    while (Date.now() < deadline) {
      const status = await send('Runtime.evaluate', { expression: 'document.readyState', returnByValue: true });
      const rendered = await send('Runtime.evaluate', { expression: 'window.__minecraftViewerReady === true', returnByValue: true });
      if (['interactive', 'complete'].includes(status.result?.result?.value) && rendered.result?.result?.value === true) {
        try {
          const screenshot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
          const image = Buffer.from(screenshot.result.data, 'base64');
          const audit = await inspectViewerFrame(image);
          await fs.writeFile(target, image, { mode: 0o600 });
          return { image, audit, capture_ms: Math.round(performance.now() - begin), sha256: createHash('sha256').update(image).digest('hex') };
        } catch (error) { lastError = error.message; }
      }
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    throw new Error(`Prismarine Viewer did not produce a rendered terrain frame within 45 seconds: ${lastError}; ${tail.slice(-500)}`);
  } finally {
    socket?.close();
    child.kill('SIGTERM');
    await Promise.race([childClosed, new Promise((resolve) => setTimeout(resolve, 5_000))]);
  }
}

async function openDevTools(url) {
  const socket = new WebSocket(url);
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('timed out connecting to Chrome DevTools')), 10_000);
    socket.addEventListener('open', () => { clearTimeout(timer); resolve(); }, { once: true });
    socket.addEventListener('error', () => { clearTimeout(timer); reject(new Error('could not connect to Chrome DevTools')); }, { once: true });
  });
  return socket;
}

function createDevToolsSender(socket) {
  let nextId = 0;
  const pending = new Map();
  socket.addEventListener('message', (event) => {
    const response = JSON.parse(event.data);
    const current = pending.get(response.id);
    if (!current) return;
    pending.delete(response.id);
    if (response.error) current.reject(new Error(response.error.message));
    else current.resolve(response);
  });
  return (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++nextId;
    pending.set(id, { resolve, reject });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

export async function handoffViewerImage(session, task, frame) {
  const image = { type: 'image', data: frame.image.toString('base64'), mimeType: 'image/png' };
  await session.prompt(task, { images: [image], source: 'rpc' });
  return frame.sha256;
}

export async function steerViewerImage(session, task, frame) {
  for (const message of session.agent.state.messages || []) {
    if (message.role !== 'user' || !Array.isArray(message.content)) continue;
    message.content = message.content.map((part) => part?.type === 'image'
      ? { type: 'text', text: '[Earlier viewer frame omitted; replaced with a fresh frame below.]' }
      : part);
  }
  const image = { type: 'image', data: frame.image.toString('base64'), mimeType: 'image/png' };
  await session.steer(task, [image], { source: 'rpc' });
  return frame.sha256;
}
