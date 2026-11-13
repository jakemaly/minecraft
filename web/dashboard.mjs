const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, character =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]);
const isNumber = value => Number.isFinite(value);
const when = value => new Date(value).toLocaleTimeString();
const title = value => value ? value[0].toUpperCase() + value.slice(1) : '';
const toolLabel = value => (value ?? 'tool').replace(/^mcp__minecraft__/, '').replaceAll('_', ' ');
const timeline = $('timeline');
let run = {};
let selected = null;
let before = null;
let messages = new Map();
let signature = '';
let controlPending = false;
let revision = 0;

async function api(path, options) {
  const response = await fetch(path, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error);
  return body;
}

function showError(message = '') {
  $('error').textContent = message;
  $('error').style.display = message ? 'block' : 'none';
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  document.documentElement.dataset.webtuiTheme = theme;
  $('theme-label').textContent = title(theme);
  $('theme-toggle').setAttribute('aria-label', `Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`);
}

function setAgent(mode, label) {
  const card = $('agent-card');
  const image = $('steve');
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const imageMode = reducedMotion && mode === 'walking' ? 'idle' : mode;
  const source = {
    thinking: 'https://skinrender.dev/render/steve/body?size=384&pose=think',
    walking: 'https://skinrender.dev/render/steve/body?pose=walk&format=apng',
    acting: 'https://skinrender.dev/render/steve/body?size=384',
    idle: 'https://skinrender.dev/render/steve/body?size=384',
  }[imageMode] || 'https://skinrender.dev/render/steve/body?size=384';
  card.dataset.mode = reducedMotion && mode === 'walking' ? 'acting' : mode;
  $('activity-line').hidden = mode === 'idle';
  if (image.dataset.source !== source) { image.src = source; image.dataset.source = source; }
  image.alt = `Steve ${imageMode === 'thinking' ? 'thinking' : imageMode === 'walking' ? 'walking' : 'standing'}`;
  $('activity').textContent = label;
}

function renderMeter(container, value, sprite, empty) {
  const points = isNumber(value) ? Math.max(0, Math.min(20, Number(value))) : null;
  const signature = points === null ? 'unknown' : String(points);
  if (container.dataset.points === signature) return points;
  container.dataset.points = signature;
  container.innerHTML = Array.from({ length: 10 }, (_, index) => {
    const amount = points === null ? 0 : points / 2 - index;
    const image = points === null ? empty : amount >= 1 ? sprite('full') : amount > 0 ? sprite('half') : empty;
    return `<img class="${container.id === 'hearts' ? 'heart' : 'food'}" alt="" src="${image}">`;
  }).join('');
  return points;
}

function renderHealth() {
  const player = run.player ?? {};
  const health = renderMeter($('hearts'), player.health, (part) => `https://raw.githubusercontent.com/PrismarineJS/minecraft-assets/master/data/1.21.4/gui/sprites/hud/heart/${part}.png`, 'https://raw.githubusercontent.com/PrismarineJS/minecraft-assets/master/data/1.21.4/gui/sprites/hud/heart/container.png');
  const food = renderMeter($('foods'), player.food, (part) => `https://raw.githubusercontent.com/PrismarineJS/minecraft-assets/master/data/1.21.4/gui/sprites/hud/food_${part}.png`, 'https://raw.githubusercontent.com/PrismarineJS/minecraft-assets/master/data/1.21.4/gui/sprites/hud/food_empty.png');
  $('health-value').textContent = health === null ? '— / 20' : `${health} / 20`;
  $('food-value').textContent = food === null ? '— / 20' : `${food} / 20`;
  $('hearts').setAttribute('aria-label', health === null ? 'Health not yet observed' : `${health} health points`);
  $('foods').setAttribute('aria-label', food === null ? 'Hunger not yet observed' : `${food} hunger points`);
  renderInventory(player.inventory);
}

function renderInventory(items) {
  const list = $('inventory-items');
  const signature = items ? JSON.stringify(items) : 'unknown';
  if (list.dataset.signature === signature) return;
  list.dataset.signature = signature;
  if (!items) {
    $('inventory-observed').textContent = 'Waiting for bot data';
    list.innerHTML = '<span class="inventory-empty">Direct inventory data is not available</span>';
    return;
  }
  $('inventory-observed').textContent = `${items.reduce((total, item) => total + item.count, 0)} items`;
  list.innerHTML = items.filter((item) => item.name && item.count > 0).map((item) => {
    const name = String(item.name).replace(/^minecraft:/, '').replace(/[^a-zA-Z0-9_]/g, '');
    const label = name.replaceAll('_', ' ');
    return `<span class="inventory-item" role="listitem" aria-label="${esc(label)} × ${esc(item.count)}" title="${esc(label)} × ${esc(item.count)}"><img class="inventory-icon" alt="" loading="lazy" src="https://raw.githubusercontent.com/PrismarineJS/minecraft-assets/master/data/1.21.4/items/${name}.png"><span class="inventory-item-name">${esc(label)}</span><span class="inventory-count">×${esc(item.count)}</span></span>`;
  }).join('') || '<span class="inventory-empty">Empty</span>';
  for (const image of list.querySelectorAll('img')) image.onerror = () => {
    image.onerror = () => { image.hidden = true; };
    image.src = image.src.replace('/items/', '/blocks/');
  };
}

function renderStatus() {
  const active = ['starting', 'running', 'saving'].includes(run.state);
  $('start').disabled = !run.state || active || controlPending;
  $('stop').hidden = !active;
  $('stop').disabled = run.state === 'saving' || controlPending;
  $('run-state').textContent = run.state === 'won' ? 'Dragon defeated' : run.id ? title(run.state) : 'No run';
  $('run-state').classList.toggle('ready', run.state === 'running' || run.state === 'won');
  $('run-detail').textContent = run.detail ?? '';
  $('run-detail').hidden = !run.detail;
  $('current-task').textContent = run.spectator ?? '';
  $('current-task-panel').hidden = !run.spectator;
  $('reasoning-value').textContent = title(run.thinkingLevel);
  const viewer = $('viewer');
  if (run.viewer) {
    if (viewer.querySelector('iframe')?.getAttribute('src') !== run.viewer) {
      const frame = document.createElement('iframe');
      frame.title = 'Live Prismarine Viewer';
      frame.src = run.viewer;
      frame.allowFullscreen = true;
      viewer.replaceChildren(frame);
    }
  } else {
    const text = run.state === 'starting' ? 'Starting the Minecraft world…'
      : run.id ? 'Run finished. Its transcript is available in Conversation and Runs.'
      : 'Start a run to open the live first-person world view.';
    if (viewer.textContent !== text) viewer.innerHTML = `<div class="viewer-empty">${text}</div>`;
  }
  const activity = run.activity ?? [];
  const pending = activity.findLast(event => event.type === 'tool_execution_start' &&
    !activity.some(other => other.type === 'tool_execution_end' && other.id === event.id));
  setAgent(run.state === 'running' ? pending ? pending.tool.endsWith('__navigate') ? 'walking' : 'acting' : 'thinking' : 'idle',
    pending ? title(toolLabel(pending.tool)) : 'Thinking');
  renderHealth();
}

function traceMarkup(entries, live = []) {
  const replies = new Map(entries.filter(message => message.role === 'toolResult').map(message => [message.toolCallId, message]));
  const finished = new Set(live.filter(event => event.type === 'tool_execution_end').map(event => event.id));
  const pending = live.filter(event => event.type === 'tool_execution_start' && !finished.has(event.id));
  const markup = [];
  const toolCard = (id, name, args, timestamp) => {
    const result = replies.get(id);
    const state = result ? result.isError ? 'Failed' : 'Finished'
      : pending.some(event => event.id === id) ? 'Running' : 'Awaiting result';
    return `<article class="trace-event trace-tool" data-key="${esc(id)}"><details>
      <summary>${esc(toolLabel(name))} · ${state}</summary>
      <pre class="trace-output ${result?.isError ? 'error-text' : ''}">${esc(args ? JSON.stringify(args, null, 2) + '\n\n' : '')}${esc(result?.text)}</pre></details>
      <div class="trace-meta"><span>${esc(when(timestamp))}</span><span>${result?.durationMs != null ? `${Math.round(result.durationMs)} ms` : ''}</span></div>
      </article>`;
  };
  const callIds = new Set(entries.flatMap(message => message.calls.map(call => call.id)));
  for (const message of entries) {
    if (message.role === 'toolResult') {
      if (!callIds.has(message.toolCallId)) markup.push(toolCard(message.toolCallId, message.toolName, null, message.timestamp));
      continue;
    }
    if (message.text || message.error) {
      markup.push(`<article class="trace-event message" data-key="${message.offset}">
        <div class="trace-meta"><span class="trace-kind">${message.role === 'user' ? 'Goal' : 'Assistant'}</span><span>${esc(when(message.timestamp))}</span></div>
        <p class="trace-text">${esc(message.text || message.error)}</p></article>`);
    }
    for (const call of message.calls) markup.push(toolCard(call.id, call.name, call.args, message.timestamp));
  }
  for (const event of pending) {
    if (!callIds.has(event.id) && !replies.has(event.id)) markup.push(toolCard(event.id, event.tool, null, event.time));
  }
  return markup.join('');
}

function atBottom() {
  return timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 48;
}

function renderConversation(older = false) {
  const markup = traceMarkup([...messages.values()].sort((a, b) => a.offset - b.offset), run.activity);
  if (markup === signature && !older) return;
  signature = markup;
  const height = timeline.scrollHeight;
  const position = timeline.scrollTop;
  const follow = atBottom();
  const list = $('trace-events');
  const open = new Set([...list.querySelectorAll('details[open]')].map(details => details.parentElement.dataset.key));
  list.innerHTML = markup;
  for (const details of list.querySelectorAll('details')) details.open = open.has(details.parentElement.dataset.key);
  $('trace-empty').hidden = Boolean(markup);
  $('more').hidden = !before;
  if (older) timeline.scrollTop = position + timeline.scrollHeight - height;
  else if (follow) timeline.scrollTop = timeline.scrollHeight;
  $('trace-bottom').hidden = atBottom();
}

async function loadConversation(id, cursor) {
  return api(`/api/conversation?id=${encodeURIComponent(id)}${cursor ? `&before=${cursor}` : ''}`);
}

async function refresh() {
  const requestRevision = revision;
  try {
    const status = await api('/api/run');
    if (revision !== requestRevision) return;
    if (status.id !== run.id) {
      messages = new Map();
      before = null;
    }
    run = status;
    renderStatus();
    if (!run.id || run.state === 'starting') return;
    const page = await loadConversation(run.id);
    if (revision !== requestRevision) return;
    const first = page.messages[0]?.offset;
    if (!messages.size || (first !== undefined && !page.messages.some(message => messages.has(message.offset)))) {
      before = page.before;
      messages.clear();
    }
    for (const message of page.messages) messages.set(message.offset, message);
    renderConversation();
  } catch (error) {
    showError(error.message);
  } finally {
    setTimeout(refresh, 1500);
  }
}

async function control(action) {
  revision++;
  controlPending = true;
  renderStatus();
  try {
    const status = await api(`/api/${action}`, { method: 'POST' });
    if (status.id !== run.id) {
      messages.clear();
      before = null;
    }
    run = status;
    renderConversation();
    showError();
  } catch (error) {
    showError(error.message);
  } finally {
    controlPending = false;
    renderStatus();
  }
}

function showTab(name) {
  for (const tab of ['conversation', 'runs']) {
    $(`${tab}-tab`).setAttribute('aria-selected', String(tab === name));
    $(tab === 'conversation' ? 'trace-panel' : 'runs-panel').hidden = tab !== name;
  }
  if (name === 'runs' && !selected) void showRuns();
}

async function showRuns() {
  selected = null;
  $('runs-list-view').hidden = false;
  $('run-detail-view').hidden = true;
  try {
    const runs = await api('/api/runs');
    $('runs-status').textContent = runs.length ? '' : 'No saved runs';
    $('runs-list').replaceChildren(...runs.map(saved => {
      const button = document.createElement('button');
      button.className = 'run-library-item';
      button.innerHTML = `<span class="run-library-title"><span>Beat the Ender Dragon</span><span>${esc(title(saved.state))}</span></span>
        <span class="run-library-meta">${esc(new Date(saved.date).toLocaleString())} · Seed ${esc(saved.seed)}</span>`;
      button.onclick = () => showRun(saved);
      return button;
    }));
  } catch (error) {
    $('runs-status').textContent = error.message;
  }
}

async function showRun(saved) {
  selected = saved.id;
  $('runs-list-view').hidden = true;
  $('run-detail-view').hidden = false;
  $('run-detail-title').textContent = saved.id;
  const panel = $('run-detail-content');
  panel.innerHTML = `<h2 class="run-detail-heading">Beat the Ender Dragon</h2>
    <dl class="run-detail-facts"><dt>Status</dt><dd>${esc(title(saved.state))}</dd>
      <dt>Seed</dt><dd>${esc(saved.seed)}</dd><dt>Model</dt><dd>${esc(saved.model)}</dd>
      <dt>Reasoning</dt><dd>${esc(title(saved.thinkingLevel))}</dd></dl>
    <p class="run-library-meta">${esc(saved.detail)}</p><button id="saved-more" class="load-more" hidden>Load older activity</button>
    <div id="saved-messages" class="run-detail-timeline"></div>`;
  const entries = [];
  async function load(cursor) {
    try {
      const page = await loadConversation(saved.id, cursor);
      if (selected !== saved.id) return;
      entries.unshift(...page.messages);
      $('saved-messages').innerHTML = traceMarkup(entries);
      $('saved-more').hidden = !page.before;
      $('saved-more').onclick = () => load(page.before);
    } catch (error) {
      if (selected === saved.id) showError(error.message);
    }
  }
  await load();
}

$('theme-toggle').onclick = () => {
  const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  setTheme(theme);
  try { localStorage.setItem('minecraft-agent-theme', theme); } catch {}
};
$('start').onclick = () => control('start');
$('stop').onclick = () => control('stop');
$('conversation-tab').onclick = () => showTab('conversation');
$('runs-tab').onclick = () => showTab('runs');
$('refresh-runs').onclick = showRuns;
$('back-to-runs').onclick = showRuns;
$('trace-bottom').onclick = () => { timeline.scrollTop = timeline.scrollHeight; };
timeline.addEventListener('scroll', () => { $('trace-bottom').hidden = atBottom(); });
$('more').onclick = async () => {
  const id = run.id;
  $('more').disabled = true;
  try {
    const page = await loadConversation(id, before);
    if (id !== run.id) return;
    before = page.before;
    for (const message of page.messages) messages.set(message.offset, message);
    renderConversation(true);
  } catch (error) {
    showError(error.message);
  } finally {
    $('more').disabled = false;
  }
};
setTheme(document.documentElement.dataset.theme === 'light' ? 'light' : 'dark');
refresh();
