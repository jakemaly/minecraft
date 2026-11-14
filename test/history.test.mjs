import assert from 'node:assert/strict';
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { conversation, savedRuns } from '../src/history.mjs';

test('saved run and conversation retain older messages and keep internal Pi messages private', async () => {
  const root = await mkdtemp(join(tmpdir(), 'acacia-history-'));
  const id = 'abcdef123456';
  const directory = join(root, id);
  try {
    await mkdir(directory);
    await writeFile(join(directory, 'run.json'), '{"seed":"97996358","model":"gpt-6.1-terra"}');
    await writeFile(join(directory, 'result.json'), '{"state":"incomplete","detail":"Early final answer"}');
    const entries = [
      { message: { role: 'system', content: 'internal' } },
      ...Array.from({ length: 80 }, (_, index) => ({ message: {
        role: 'assistant', timestamp: index, content: [
          { type: 'text', text: `Reply ${index} ${'⛏'.repeat(index === 40 ? 30_000 : 1)}` },
          { type: 'thinking', thinking: 'internal reasoning' },
        ],
      } })),
    ];
    await writeFile(join(directory, '2026-11-13T04-52-26-299Z_01a1d33f-f43a-76af-9db0-c84c6ee95f8e.jsonl'),
      entries.map(entry => JSON.stringify(entry)).join('\n') + '\n');
    const latest = await conversation(directory);
    const older = await conversation(directory, latest.before);
    assert.deepEqual([...older.messages, ...latest.messages].map(message => message.timestamp), Array.from({ length: 80 }, (_, i) => i));
    assert.equal(latest.messages[0].text, `Reply 40 ${'⛏'.repeat(30_000)}`);
    assert.equal(JSON.stringify(latest.messages).includes('internal reasoning'), false);
    assert.deepEqual((await savedRuns(root)).map(({ id, state }) => ({ id, state })), [{ id, state: 'incomplete' }]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
