import assert from 'node:assert/strict';
import test from 'node:test';
import { observationText, shellText, stateChangeText } from '../src/model_context.mjs';

test('default observation exposes urgent state and leaves block scans available on demand', () => {
  const state = {
    player: { health: 6, oxygen: 2, position: { x: 1, y: 2, z: 3 } },
    inventory_summary: { iron_ingot: 2 },
    nearby_blocks: [{ name: 'lava', distance: 2, position: { x: 1, y: 1, z: 3 } }, { name: 'dirt' }],
  };
  const text = observationText(state);
  assert.match(text, /health: 6/);
  assert.match(text, /air: 2/);
  assert.match(text, /inventory\[1\]\{item,count\}:\n  iron_ingot,2/);
  assert.match(text, /lava/);
  assert.doesNotMatch(text, /dirt/);
  assert.match(observationText(state, 'blocks'), /dirt/);
  assert.match(observationText({}, 'blocks'), /blocks\[0\]:/);
});

test('shell results distinguish cancellation from shell success and expose captured detail', () => {
  const text = shellText({
    status: 'completed', exit_code: 0,
    stdout: JSON.stringify({ program_id: '123', script: 'scripts/swim.js', status: 'cancelled', output: [] }),
  }, 'saved-result');
  assert.match(text, /gameplay_status: cancelled/);
  assert.match(text, /exit_code: 0/);
  assert.match(text, /saved-result/);
  assert.doesNotMatch(text, /program_id/);
  assert.match(shellText({ stdout: 'x'.repeat(8000) }, 'saved-result'), /truncated/);
});

test('gameplay changes retain inventory gains and losses without repeating the entire inventory', () => {
  assert.match(stateChangeText(
    { player: { health: 20 }, inventory_summary: { coal: 2, bucket: 1 } },
    { player: { health: 18 }, inventory_summary: { coal: 4 } },
  ), /^health: 20 -> 18\ninventory_changes\[2\]\{item,delta\}:\n  coal,2\n  bucket,-1$/);
});
