import { chmod, mkdir, rename, writeFile } from 'node:fs/promises';
import path from 'node:path';

export const LOCAL_PROVIDER_ID = 'molab-vllm';

export function localModelSettings(env = process.env) {
  if (!env.MINECRAFT_LOCAL_MODEL_PROVIDER) return null;
  if (env.MINECRAFT_LOCAL_MODEL_PROVIDER !== LOCAL_PROVIDER_ID) {
    throw new Error(`unsupported local model provider ${env.MINECRAFT_LOCAL_MODEL_PROVIDER}`);
  }
  const baseUrl = String(env.MINECRAFT_LOCAL_MODEL_BASE_URL || '').replace(/\/$/, '');
  let parsed;
  try { parsed = new URL(baseUrl); }
  catch { throw new Error('local vLLM base URL is invalid'); }
  if (parsed.protocol !== 'http:' || !['127.0.0.1', 'localhost', '[::1]'].includes(parsed.hostname) || parsed.pathname !== '/v1') {
    throw new Error('local vLLM must use an HTTP loopback /v1 endpoint');
  }
  const id = String(env.MINECRAFT_LOCAL_MODEL_ID || '').trim();
  if (!id) throw new Error('local vLLM model ID is missing');
  return {
    provider: LOCAL_PROVIDER_ID,
    id,
    name: 'Nemotron 3.5 Super VL 120B A12B NVFP4',
    baseUrl,
    revision: String(env.MINECRAFT_LOCAL_MODEL_REVISION || ''),
    contextWindow: Number(env.MINECRAFT_LOCAL_MODEL_CONTEXT_WINDOW || 1_048_576),
    maxTokens: Number(env.MINECRAFT_LOCAL_MODEL_MAX_TOKENS || 16_384),
  };
}

export function localModelsJson(settings) {
  return {
    providers: {
      [settings.provider]: {
        name: 'Molab vLLM',
        baseUrl: settings.baseUrl,
        api: 'openai-completions',
        // Pi requires a configured credential to list a static compatible model; vLLM ignores this dummy key.
        apiKey: 'not-needed',
        models: [{
          id: settings.id,
          name: settings.name,
          reasoning: true,
          input: ['text', 'image'],
          contextWindow: settings.contextWindow,
          maxTokens: settings.maxTokens,
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        }],
      },
    },
  };
}

export async function writeLocalModelsJson(agentDir, settings) {
  const target = path.join(agentDir, 'molab-models.json');
  const temporary = `${target}.tmp`;
  await mkdir(agentDir, { recursive: true, mode: 0o700 });
  await writeFile(temporary, `${JSON.stringify(localModelsJson(settings), null, 2)}\n`, { mode: 0o600 });
  await chmod(temporary, 0o600);
  await rename(temporary, target);
  return target;
}

export async function probeLocalModel(settings, fetchImpl = fetch) {
  const url = `${settings.baseUrl}/models`;
  const response = await fetchImpl(url, { signal: AbortSignal.timeout(5_000) });
  if (!response.ok) throw new Error(`local vLLM model probe returned HTTP ${response.status}`);
  const data = await response.json();
  const served = Array.isArray(data.data) ? data.data : [];
  if (!served.some((model) => model?.id === settings.id)) {
    throw new Error(`local vLLM is healthy but does not serve ${settings.id}`);
  }
  return {
    provider: settings.provider,
    id: settings.id,
    name: settings.name,
    api: 'openai-completions',
    context_window: settings.contextWindow,
    max_output_tokens: settings.maxTokens,
    image_input: true,
    reasoning: true,
    revision: settings.revision,
    endpoint: settings.baseUrl,
    selection_policy: 'pinned local Molab vLLM model',
  };
}
