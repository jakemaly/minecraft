import assert from 'node:assert/strict';
import { test } from 'node:test';
import { dragonAdvancement } from '../src/run.mjs';

test('only the bot’s server advancement proves a win', () => {
  const prefix = '[04:39:50] [Server thread/INFO]: ';
  const message = 'Acacia has made the advancement [Free the End]';
  const log = `${prefix}<Acacia> ${message}\n${prefix}AI_M1KE has made the advancement [Free the End]\n${prefix}Spectator has made the advancement [Free the End]\n`;
  assert.equal(dragonAdvancement(log), null);
  assert.equal(dragonAdvancement(`${log}${prefix}${message}\n`), `${prefix}${message}`);
});
