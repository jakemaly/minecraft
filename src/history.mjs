import { open, readFile, readdir, stat } from 'node:fs/promises';
import { join } from 'node:path';

export const runId = /^[a-f0-9]{12}$/;
const sessionFile = /^\d{4}-\d\d-\d\dT[\d-Z]+_[a-f0-9-]+\.jsonl$/i;

export async function savedRuns(root) {
  const entries = await readdir(root, { withFileTypes: true });
  const runs = await Promise.all(entries.filter(entry => entry.isDirectory() && runId.test(entry.name)).map(async ({ name }) => {
    const directory = join(root, name);
    try {
      const manifest = join(directory, 'run.json');
      const { seed, model, thinkingLevel } = JSON.parse(await readFile(manifest, 'utf8'));
      const date = (await stat(manifest)).mtime.toISOString();
      const result = await readFile(join(directory, 'result.json'), 'utf8').then(JSON.parse, error => {
        if (error.code !== 'ENOENT') throw error;
        return { state: 'incomplete', detail: 'The service stopped before recording an outcome.' };
      });
      return { id: name, date, seed, model, thinkingLevel, state: result.state, detail: result.detail, player: result.player };
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
  }));
  return runs.filter(Boolean).sort((left, right) => right.date.localeCompare(left.date));
}

export async function conversation(directory, before) {
  const file = (await readdir(directory)).find(name => sessionFile.test(name));
  if (!file) return { messages: [], before: null };
  const handle = await open(join(directory, file), 'r');
  try {
    const { size } = await handle.stat();
    let end = Math.min(before ?? size, size);
    let carry = Buffer.alloc(0);
    const messages = [];
    const maxMessages = 40;
    while (end && messages.length < maxMessages) {
      const length = Math.min(end, 64 * 1024);
      const start = end - length;
      const chunk = Buffer.alloc(length);
      await handle.read(chunk, 0, length, start);
      const buffer = Buffer.concat([chunk, carry]);
      let lineEnd = buffer.length;
      while (messages.length < maxMessages) {
        const newline = lineEnd ? buffer.lastIndexOf(10, lineEnd - 1) : -1;
        if (newline === -1 && start !== 0) break;
        const offset = start + newline + 1;
        const line = buffer.subarray(newline + 1, lineEnd).toString('utf8');
        if (line) {
          const message = displayMessage(line, offset);
          if (message) messages.push(message);
        }
        lineEnd = newline;
        if (newline < 0) break;
      }
      if (messages.length === maxMessages) return { messages: messages.reverse(), before: start + lineEnd + 1 || null };
      carry = buffer.subarray(0, lineEnd);
      end = start;
    }
    return { messages: messages.reverse(), before: null };
  } finally {
    await handle.close();
  }
}

function displayMessage(line, offset) {
  let entry;
  try { entry = JSON.parse(line); } catch { return null; }
  const message = entry.message;
  if (!['user', 'assistant', 'toolResult'].includes(message?.role)) return null;
  const { role, timestamp, toolCallId, toolName, isError, durationMs } = message;
  const blocks = Array.isArray(message.content) ? message.content : [{ type: 'text', text: message.content }];
  const text = blocks.filter(block => block.type === 'text').map(block => block.text).join('\n');
  const calls = blocks.filter(block => block.type === 'toolCall').map(({ id, name, arguments: args }) => ({ id, name, args }));
  return {
    offset, role, timestamp, text, calls, toolCallId, toolName, isError, durationMs,
    error: message.errorMessage,
  };
}
