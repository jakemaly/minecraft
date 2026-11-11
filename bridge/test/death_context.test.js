const test = require('node:test');
const assert = require('node:assert/strict');
const { deathEvidence } = require('../src/death_context.js');

test('death evidence preserves last observed risk context without inventing a cause', () => {
  const evidence = deathEvidence({
    observed_at: '2026-11-10T12:00:00Z',
    world: { dimension: 'the_nether' },
    player: { position: { x: 2, y: 64, z: 3 }, health: 4, food: 7, oxygen: 300, equipment: { torso: null } },
    nearby_entities: [{ type: 'mob', name: 'piglin', distance: 2 }],
    nearby_blocks: [
      { name: 'lava', position: { x: 4, y: 63, z: 3 }, distance: 2 },
      { name: 'netherrack', position: { x: 2, y: 64, z: 4 }, distance: 1 },
    ],
  });

  assert.equal(evidence.cause, 'unknown');
  assert.equal(evidence.last_observed.observed_at, '2026-11-10T12:00:00Z');
  assert.equal(evidence.last_observed.health, 4);
  assert.deepEqual(evidence.last_observed.nearby_entities, [{ type: 'mob', name: 'piglin', distance: 2 }]);
  assert.deepEqual(evidence.last_observed.nearby_hazards.map(({ name }) => name), ['lava']);
});

test('death without a prior observation remains explicitly unknown', () => {
  assert.deepEqual(deathEvidence(null), {
    cause: 'unknown',
    cause_evidence: 'Mineflayer death event does not identify a cause',
    last_observed: null,
  });
});
