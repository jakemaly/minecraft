import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { deathReview, reflectDeath } from '../src/primitives.mjs';

test('death reflection retains analysis as data and discards gameplay changes', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'minecraft-review-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const expected = {
    category: 'decision',
    summary: 'Acacia stayed exposed after the shield broke while continuing an optional fight.',
    evidence: ['Request 12 continued the fight after the shield broke.'],
    uncertainty: 'The trace does not show whether a covered route was reachable.',
    reviewQuestion: 'Did the model see the shield break before committing to another attack?',
  };
  const result = await reflectDeath({
    directory, death: { cause: 'Acacia was slain by Zombie' },
    signal: new AbortController().signal,
    inference: async () => ({
      model: {},
      runtime: { completeSimple: async (_model, context) => {
        assert.match(context.messages[0].content, /slain by Zombie/);
        assert.equal(context.tools?.length ?? 0, 0);
        const response = { ...expected, profile: { criticalHealth: 14 }, changes: { hostileAvoidance: 3 }, stopped: true };
        return { stopReason: 'stop', content: [{ type: 'text', text: JSON.stringify(response) }] };
      } },
    }),
  });
  assert.deepEqual(result, expected);
  assert.throws(() => deathReview({ ...expected, category: 'rewrite_tools' }), /valid death review/);
});
