import assert from 'node:assert/strict';
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { test } from 'node:test';
import { arm, reconcile } from '../src/overseer.mjs';

async function fixture(t) {
  const root = await mkdtemp(join(tmpdir(), 'mine-overseer-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const statePath = join(root, 'overseer.json');
  const seen = [];
  async function run(id, files = {}) {
    const directory = join(root, id);
    await mkdir(directory);
    for (const [file, content] of Object.entries(files)) {
      const path = join(directory, file);
      await mkdir(dirname(path), { recursive: true });
      await writeFile(path, content);
    }
  }
  return {
    arm: () => arm(root, statePath), run, seen,
    check: (status, wake = event => seen.push(event)) => reconcile({ root, statePath, observe: () => status, wake }),
  };
}

test('the overseer ignores history and live runs, then wakes once for an incomplete or stopped run', async t => {
  const f = await fixture(t);
  await f.run('111111111111', { 'result.json': '{"state":"incomplete"}' });
  await f.arm();
  await f.run('222222222222', { 'result.json': '{"state":"incomplete"}' });
  assert.equal(await f.check({ id: '222222222222', state: 'saving' }), 'waiting');
  assert.equal(await f.check(null), 'unknown');
  assert.equal(await f.check({ id: '222222222222', state: 'incomplete' }, () => false), 'chat busy');
  assert.equal(await f.check({ id: '222222222222', state: 'incomplete' }), 'incomplete');
  await f.run('333333333333', { 'result.json': '{"state":"stopped"}' });
  assert.equal(await f.check({ id: '333333333333', state: 'stopped' }), 'stopped');
  assert.equal(await f.check({ id: '999999999999', state: 'incomplete' }), 'incomplete');
  assert.equal(await f.check({ state: 'idle' }), 'waiting');
  assert.deepEqual(f.seen.map(event => event.id), ['222222222222', '333333333333', '999999999999']);
});

test('a crashed harness leaves a detectable run, and a failed Codex wake can retry', async t => {
  const f = await fixture(t);
  await f.arm();
  await f.run('444444444444', { 'run.json.tmp': '{"unfinished"', 'result.json': '{"unfinished"' });
  await assert.rejects(f.check({ state: 'offline' }, () => { throw new Error('Codex unavailable'); }), /Codex unavailable/);
  assert.equal(await f.check({ state: 'offline' }), 'interrupted');
  assert.equal(await f.check({ state: 'offline' }), 'waiting');
  assert.equal(f.seen[0].id, '444444444444');
});

test('the vanilla server win takes priority over pending failures and permanently disarms the watcher', async t => {
  const f = await fixture(t);
  await f.arm();
  await f.run('555555555555', { 'result.json': '{"state":"incomplete"}' });
  const proof = '[04:57:47] [Server thread/INFO]: Acacia has made the advancement [Free the End]';
  await f.run('666666666666', { 'server/logs/latest.log': proof + '\n', 'result.json': '{"state":"incomplete"}' });
  assert.equal(await f.check({ state: 'offline' }), 'won');
  assert.equal(await f.check({ state: 'offline' }), 'won');
  assert.deepEqual(f.seen.map(event => [event.id, event.proof]), [['666666666666', proof]]);
});

test('a model or unverified result claiming a win cannot disarm the watcher', async t => {
  const f = await fixture(t);
  await f.arm();
  await f.run('777777777777', {
    'result.json': '{"state":"won"}',
    'server.log': '[04:57:47] [Server thread/INFO]: <Acacia> Acacia has made the advancement [Free the End]\n',
  });
  assert.equal(await f.check({ state: 'offline' }), 'unverified');
  assert.equal(f.seen[0].proof, null);
});
