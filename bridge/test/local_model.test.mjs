import test from 'node:test';
import assert from 'node:assert/strict';

import {
  LOCAL_PROVIDER_ID,
  localModelSettings,
  localModelsJson,
  probeLocalModel,
} from '../src/local_model.mjs';

const env = {
  MINECRAFT_LOCAL_MODEL_PROVIDER: LOCAL_PROVIDER_ID,
  MINECRAFT_LOCAL_MODEL_ID: 'nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4',
  MINECRAFT_LOCAL_MODEL_BASE_URL: 'http://127.0.0.1:8000/v1',
  MINECRAFT_LOCAL_MODEL_REVISION: 'fixed-revision',
};

test('local provider only accepts an HTTP loopback endpoint', () => {
  assert.equal(localModelSettings(env).id, env.MINECRAFT_LOCAL_MODEL_ID);
  assert.throws(() => localModelSettings({ ...env, MINECRAFT_LOCAL_MODEL_BASE_URL: 'https://remote.example/v1' }), /loopback/);
  assert.throws(() => localModelSettings({ ...env, MINECRAFT_LOCAL_MODEL_BASE_URL: 'http://127.0.0.1:8000/other' }), /loopback/);
});

test('Pi gets one configured image-capable model and no hosted-provider catalog', () => {
  const config = localModelsJson(localModelSettings(env));
  assert.deepEqual(Object.keys(config.providers), [LOCAL_PROVIDER_ID]);
  assert.equal(config.providers[LOCAL_PROVIDER_ID].api, 'openai-completions');
  assert.deepEqual(config.providers[LOCAL_PROVIDER_ID].models[0].input, ['text', 'image']);
});

test('local model health must report the exact pinned served model', async () => {
  const settings = localModelSettings(env);
  const goodFetch = async (url) => {
    assert.equal(url, `${settings.baseUrl}/models`);
    return { ok: true, json: async () => ({ data: [{ id: settings.id }] }) };
  };
  assert.equal((await probeLocalModel(settings, goodFetch)).id, settings.id);
  const wrongFetch = async () => ({ ok: true, json: async () => ({ data: [{ id: 'other-model' }] }) });
  await assert.rejects(probeLocalModel(settings, wrongFetch), /does not serve/);
});
