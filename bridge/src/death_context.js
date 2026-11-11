'use strict';

function deathEvidence(observation) {
  if (!observation) return {
    cause: 'unknown',
    cause_evidence: 'Mineflayer death event does not identify a cause',
    last_observed: null,
  };
  const player = observation.player || {};
  return {
    cause: 'unknown',
    cause_evidence: 'Mineflayer death event does not identify a cause',
    last_observed: {
      observed_at: observation.observed_at || null,
      dimension: observation.world?.dimension || null,
      position: player.position || null,
      health: player.health ?? null,
      food: player.food ?? null,
      oxygen: player.oxygen ?? null,
      equipment: player.equipment || {},
      inventory_summary: observation.inventory_summary || {},
      nearby_entities: (observation.nearby_entities || []).slice(0, 8),
      nearby_hazards: (observation.nearby_blocks || [])
        .filter((block) => /lava|fire|magma|cactus|berry|powder_snow|water/.test(block.name || ''))
        .slice(0, 12),
    },
  };
}

module.exports = { deathEvidence };
