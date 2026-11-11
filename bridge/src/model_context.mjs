const scalar = value => {
  if (typeof value !== 'string') return JSON.stringify(value ?? null);
  return /^[a-zA-Z_][a-zA-Z0-9_]*$/.test(value) && !['true', 'false', 'null'].includes(value)
    ? value : JSON.stringify(value);
};

function table(name, fields, rows) {
  if (!rows.length) return `${name}[0]:`;
  return [`${name}[${rows.length}]{${fields.join(',')}}:`, ...rows.map(row => `  ${row.map(scalar).join(',')}`)].join('\n');
}

const coordinate = n => Number.isFinite(n) ? Math.round(n * 10) / 10 : null;
const coordinates = p => [p?.x, p?.y, p?.z].map(coordinate);

export function observationText(state, view = 'status') {
  const entities = state.nearby_entities || [];
  const blocks = state.nearby_blocks || [];
  const locations = (name, values, exact = false) => table(name, ['name', 'x', 'y', 'z', 'distance'],
    values.map(v => [v.name, ...(exact ? [v.position?.x, v.position?.y, v.position?.z] : coordinates(v.position)), exact ? v.distance : coordinate(v.distance)]));
  if (view === 'blocks') return locations('blocks', [...blocks, ...(state.nearby_logs || [])], true);
  if (view === 'entities') return locations('entities', entities, true);
  if (view === 'inventory') return table('slots', ['slot', 'item', 'count'],
    (state.inventory || []).map(v => [v.slot, v.name, v.count]));
  if (view === 'events') return JSON.stringify({ events: state.recent_events || [], deaths: state.recent_deaths || [] });
  const p = state.player || {};
  const w = state.world || {};
  return [
    `dimension: ${scalar(w.dimension)}`, `day: ${scalar(w.is_day)}`,
    `position[3]: ${coordinates(p.position).map(scalar).join(',')}`,
    `health: ${scalar(p.health)}`, `food: ${scalar(p.food)}`, `air: ${scalar(p.oxygen)}`,
    table('gear', ['slot', 'item'], Object.entries(p.equipment || {})
      .filter(([, item]) => item).map(([slot, item]) => [slot, item.name])),
    table('inventory', ['item', 'count'], Object.entries(state.inventory_summary || {})),
    locations('threats', entities.filter(e => ['hostile', 'mob'].includes(e.type)).slice(0, 6)),
    locations('hazards', blocks.filter(b => /lava|fire|magma_block|powder_snow|water/.test(b.name)).slice(0, 6)),
  ].join('\n');
}

export function gameplayResult(stdout) {
  try {
    const value = JSON.parse(stdout);
    if (value?.program_id && value?.script && ['completed', 'cancelled', 'timeout', 'failed'].includes(value.status)) return value;
  } catch {}
  return null;
}

export function shellText(result, ref, gameplay = gameplayResult(result.stdout)) {
  const lines = [
    `shell_status: ${result.status || 'unknown'}`,
    `exit_code: ${result.exit_code ?? 'unknown'}`,
    ...(gameplay ? [`script: ${gameplay.script}`, `gameplay_status: ${gameplay.status}`] : []),
  ];
  const stdout = gameplay ? [
    ...(Array.isArray(gameplay.output) ? gameplay.output : []).map(item => item?.text),
    ...(gameplay.result == null ? [] : [JSON.stringify(gameplay.result)]),
    gameplay.error,
  ].filter(Boolean).join('\n') : result.stdout;
  for (const [label, output, limit, capped] of [
    ['output', stdout, 750, result.stdout_truncated],
    ['stderr', result.stderr, 350, result.stderr_truncated],
  ]) {
    if (output) lines.push(`${label}:\n${output.slice(0, limit)}`);
    if (output?.length > limit || capped) lines.push(`${label} truncated (captured ${output?.length || 0} chars)`);
  }
  if (ref) lines.push(`Details: tool_output(ref=${JSON.stringify(ref)}, offset=0)`);
  return lines.join('\n');
}

export function stateChangeText(before, after) {
  const oldPlayer = before.player || {};
  const player = after.player || {};
  const changed = ['health', 'food', 'oxygen']
    .filter(key => oldPlayer[key] != null && player[key] != null && oldPlayer[key] !== player[key])
    .map(key => `${key}: ${oldPlayer[key]} -> ${player[key]}`);
  const position = coordinates(player.position).join(',');
  if (player.position && position !== coordinates(oldPlayer.position).join(',')) changed.push(`position: ${position}`);
  const oldItems = before.inventory_summary || {};
  const items = after.inventory_summary || {};
  const deltas = [...new Set([...Object.keys(oldItems), ...Object.keys(items)])]
    .map(item => [item, (items[item] || 0) - (oldItems[item] || 0)])
    .filter(([, delta]) => delta);
  if (deltas.length) changed.push(table('inventory_changes', ['item', 'delta'], deltas.slice(0, 8)));
  if (deltas.length > 8) changed.push(`${deltas.length - 8} more inventory changes. See minecraft_observe.`);
  return changed.join('\n');
}
