// Replace the remote model boundary while running the real Pi worker and tools.
import { strict as assert } from 'node:assert';
import { existsSync, writeFileSync, unlinkSync } from 'node:fs';
import { registerHooks } from 'node:module';

registerHooks({
  resolve(specifier, context, next) {
    return specifier === '@earendil-works/pi-coding-agent'
      ? { url: import.meta.url, shortCircuit: true }
      : next(specifier, context);
  },
});

globalThis.fetch = async (url, options) => {
  if (url === 'https://html.duckduckgo.com/html/?q=mineflayer') {
    return new Response('<div class="result__body"><a class="result__a" href="https://example.test/docs">Bot &amp; world</a></div>');
  }
  assert.equal(url, 'https://api.openai.com/v1/models');
  assert.equal(options.headers.authorization, 'Bearer test-oauth');
  return Response.json({ models: ['gpt-6-sol', 'gpt-6-astra', 'text-only'].map(slug => ({ slug })) });
};

const model = id => ({
  id, name: id, provider: 'openai', api: 'openai-responses',
  input: id === 'text-only' ? ['text'] : ['text', 'image'],
  reasoning: true, contextWindow: 100_000, maxTokens: 10_000,
});

export const ModelRuntime = {
  async create({ authPath }) {
    return {
      listCredentials: () => existsSync(authPath) ? [{ providerId: 'openai', type: 'oauth' }] : [],
      getAuth: () => ({ auth: { apiKey: 'test-oauth' } }),
      getModels: () => ['gpt-6-sol', 'text-only', 'gpt-6-astra', 'unlisted'].map(model),
      getModel: (_provider, id) => model(id),
      async login(_provider, type, interaction) {
        assert.equal(type, 'oauth');
        interaction.notify({ type: 'auth_url', url: 'https://example.test/login' });
        assert.equal(await interaction.prompt({ placeholder: 'callback' }), 'http://127.0.0.1:1455/auth/callback?code=test');
        writeFileSync(authPath, '{}');
      },
      logout: () => unlinkSync(authPath),
    };
  },
};

export const SettingsManager = { create() {} };
export const SessionManager = { inMemory() {} };
export const defineTool = tool => tool;
export const estimateTokens = () => 1;
export class DefaultResourceLoader {
  async reload() {}
}

export async function createAgentSession({ customTools }) {
  const messages = [];
  let onEvent;
  return { session: {
    agent: { state: { messages } },
    subscribe(listener) { onEvent = listener; return () => {}; },
    dispose() {},
    async prompt(text, { images }) {
      assert.ok(images.length <= 1);
      if (images.length) {
        assert.equal(images[0].mimeType, 'image/png');
        assert.equal(Buffer.from(images[0].data, 'base64').subarray(1, 4).toString(), 'PNG');
      }
      messages.push({ role: 'user', content: [{ type: 'text', text }, ...images] });
      const assistant = { role: 'assistant', id: crypto.randomUUID(), content: [] };
      onEvent({ type: 'message_start', message: assistant });
      if (text.includes('simulate-subscription-limit')) throw new Error('subscription_sharing_usage_limit_exceeded');

      async function call(name, args) {
        const tool = customTools.find(candidate => candidate.name === name);
        assert.ok(tool, `missing native tool ${name}`);
        const id = crypto.randomUUID();
        const result = await tool.execute(id, args);
        assert.ok(!result.isError, JSON.stringify(result));
        assistant.content.push({ type: 'toolCall', name, id, arguments: args });
        return result.details;
      }

      if (customTools.some(tool => tool.name === 'probe_complete')) {
        await call('probe_complete', { description: 'Minecraft scene' });
      } else {
        await call('minecraft_observe', {});
        await call('minecraft_verify_goal', {});
        const search = await call('web_search', { query: 'mineflayer', max_results: 1 });
        assert.deepEqual(search.results, [{ title: 'Bot & world', url: 'https://example.test/docs', snippet: '' }]);
        await call('write', { path: 'scripts/live.js', content: 'return bot.username;' });
        if (text.includes('DEATH REFLECTION REQUIRED')) {
          assert.ok(text.includes('"type":"death"'));
          const memory = await call('read', { path: 'memory.md' });
          const reflection = 'Death reflection: preserve this world and continue the objective';
          if (!memory.content.includes(reflection)) {
            await call('write', { path: 'memory.md', content: [
              memory.content, reflection,
              'Cause: unknown. Evidence: last observed state only. Prevention: inspect the current state and change the failed approach before repeating it. Recovery: continue in this world.',
            ].filter(Boolean).join('\n') });
          }
          const plan = await call('read', { path: 'plan.md' });
          if (!plan.content.includes('Recovery after death')) {
            await call('write', { path: 'plan.md', content: [
              plan.content,
              '## Recovery after death\n- Cause remains unknown from direct evidence.\n- Re-observe and choose a changed, safe next action.',
            ].filter(Boolean).join('\n\n') });
          }
        }
      }
      messages.push(assistant);
    },
  } };
}
