import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createAgentSession, DefaultResourceLoader, ModelRuntime, SessionManager, SettingsManager } from '@earendil-works/pi-coding-agent';
import { McpClient, McpConnectionClosedError, McpSessionExpiredError, StreamableHttpTransport, toLlmContent } from '@earendil-works/pi-mcp';
import { Agent, fetch } from 'undici';

export const modelId = 'gpt-6.1-terra';
export const thinkingLevel = 'medium';
const maxContinuationsWithoutAction = 3;
const continuePlaying = 'The server has not verified a dragon kill. Continue playing from the current world state. Call the next useful Minecraft tool. If a foreground action is pending or its result has not been retrieved, recover that action first.';

export async function openai(agentDir) {
  const runtime = await ModelRuntime.create({
    authPath: join(agentDir, 'auth.json'),
    modelsPath: fileURLToPath(new URL('../deploy/models.json', import.meta.url)), refreshOnCreate: false,
  });
  const credential = (await runtime.listCredentials()).find(item => item.providerId === 'openai');
  if (credential?.type !== 'oauth') throw new Error('Sign in to Pi with ChatGPT OAuth before starting a run.');
  const model = runtime.getModel('openai', modelId);
  if (model?.api !== 'openai-responses') throw new Error(`Pi does not support ${modelId} through OpenAI Responses.`);
  const getAuth = runtime.getAuth.bind(runtime);
  runtime.getAuth = async (target, options) => {
    const resolution = await getAuth(target, options);
    if (resolution?.source !== 'OAuth') throw new Error('Pi requires ChatGPT OAuth for every request.');
    return resolution;
  };
  const stream = runtime.streamSimple.bind(runtime);
  runtime.streamSimple = (target, context, options) => stream(target, context, {
    ...options, timeoutMs: Math.max(options?.timeoutMs ?? 0, 30 * 60_000),
  });
  return { runtime, model };
}

export async function play({ directory, socket, agentDir, signal, record, prompt: promptFile = new URL('../deploy/prompt.txt', import.meta.url), actionWaitMs, inference = openai }) {
  const { runtime, model } = await inference(agentDir);
  const dispatcher = new Agent({ connect: { socketPath: socket } });
  let client;
  async function connectMcp() {
    client = new McpClient({ name: 'Pi Minecraft', version: '1.0.0', requestTimeoutMs: 180_000 });
    await client.connect(new StreamableHttpTransport({
      url: 'http://127.0.0.1:25575/mcp',
      fetch: (url, options) => fetch(url, {
        ...options, dispatcher, signal: options?.signal ? AbortSignal.any([signal, options.signal]) : signal,
      }),
    }));
  }
  let session;
  let lastReply;
  let continuationsWithoutAction = 0;
  const completedActions = new Set();
  const stop = () => { void session.abort().catch(() => {}); };
  try {
    signal.throwIfAborted();
    await connectMcp();
    const catalogue = await client.listTools({ signal });
    if (catalogue.length !== 37 || catalogue.some(tool => tool.name.startsWith('debug_'))) {
      throw new Error('Mine AI did not expose the 37 standard tools.');
    }
    const tools = catalogue.map(tool => ({
      name: `mcp__minecraft__${tool.name}`,
      label: tool.name,
      description: tool.description,
      parameters: tool.inputSchema,
      async execute(_id, args, toolSignal) {
        // The async action keeps its owner and checkpoint after a wait expires.
        // Bound how long Pi goes without a chance to reconsider the action.
        const waitKey = tool.name === 'wait_for_action' ? 'timeout_ms' : 'wait_timeout_ms';
        if (actionWaitMs && args[waitKey] > actionWaitMs) args = { ...args, [waitKey]: actionWaitMs };
        let result;
        try {
          result = await client.callTool(tool.name, args, { signal: toolSignal });
        } catch (error) {
          if (!(error instanceof McpSessionExpiredError || error instanceof McpConnectionClosedError)) throw error;
          signal.throwIfAborted();
          await client.close();
          await connectMcp();
          record('mcp_reconnected', {});
          // A 404 rejects the call before dispatch. After a dropped connection its outcome is unknown.
          if (error instanceof McpConnectionClosedError) {
            throw new Error('Minecraft MCP reconnected. Check view_status and retrieve any pending action before retrying.');
          }
          result = await client.callTool(tool.name, args, { signal: toolSignal });
        }
        const response = result.structuredContent?.response;
        // Polling or retrieving an old result again does not make progress.
        const completed = response?.data?.output?.action ? response.data.actionId
          : response?.markdown?.match(/^Action ID: ([a-f0-9-]+)\n\n## `/)?.[1];
        if (!result.isError && completed && !completedActions.has(completed)) {
          completedActions.add(completed);
          continuationsWithoutAction = 0;
        }
        const { _meta, ...details } = result;
        const content = toLlmContent(result);
        if (result.content.length && result.structuredContent?.response?.format === 'json') {
          content.push({ type: 'text', text: JSON.stringify(result.structuredContent) });
        }
        return { content, details, isError: result.isError };
      },
    }));
    const settingsManager = SettingsManager.inMemory({ compaction: { enabled: false }, cacheWarming: 'off' });
    const resourceLoader = new DefaultResourceLoader({
      cwd: directory, agentDir, settingsManager, noExtensions: true, noSkills: true,
      noContextFiles: true, noPromptTemplates: true, noThemes: true, appendSystemPrompt: [], systemPrompt: client.instructions,
    });
    await resourceLoader.reload();
    ({ session } = await createAgentSession({
      cwd: directory, agentDir, model, modelRuntime: runtime, thinkingLevel, settingsManager, resourceLoader,
      sessionManager: SessionManager.create(directory, directory),
      customTools: tools, noTools: 'all', tools: tools.map(tool => tool.name),
    }));
    // A wait and its successor must reach Mine AI in the order Pi emitted them.
    session.agent.toolExecution = 'sequential';
    if (session.getActiveToolNames().sort().join() !== tools.map(tool => tool.name).sort().join()) {
      throw new Error('Pi did not activate the exact Mine AI tool catalogue.');
    }
    record('catalogue', { tools: catalogue, model: model.id, contextWindow: model.contextWindow });
    session.subscribe(event => {
      if (event.type === 'tool_execution_start' || event.type === 'tool_execution_end') {
        const response = event.result?.details?.structuredContent?.response;
        const markdown = response?.markdown ?? '';
        const actionId = response?.data?.actionId ?? markdown.match(/^Action ID: ([a-f0-9-]+)$/m)?.[1];
        const outcome = response?.data?.state ?? (markdown.startsWith('**Accepted:**') ? 'accepted'
          : markdown.startsWith('## Action in progress') ? 'pending'
          : actionId ? 'settled' : undefined);
        record(event.type, { id: event.toolCallId, tool: event.toolName, isError: event.isError, actionId, outcome });
      }
      if (event.type === 'message_end' && event.message.role === 'assistant') {
        lastReply = event.message;
        const { model, usage, stopReason, rawStopReason, errorMessage } = event.message;
        record('assistant', { model, usage, stopReason, rawStopReason, errorMessage });
      }
    });
    signal.addEventListener('abort', stop, { once: true });
    signal.throwIfAborted();
    let prompt = await readFile(promptFile, 'utf8');
    for (;;) {
      signal.throwIfAborted();
      lastReply = undefined;
      await session.prompt(prompt);
      signal.throwIfAborted();
      const last = lastReply;
      if (last?.errorMessage) throw new Error(last.errorMessage);
      if (last?.stopReason !== 'stop') {
        return `Pi stopped (${last?.rawStopReason ?? last?.stopReason ?? 'no model reply'}) before the server recorded a dragon kill.`;
      }
      if (continuationsWithoutAction === maxContinuationsWithoutAction) {
        return `The model kept ending early. ${maxContinuationsWithoutAction} follow-ups produced no completed Minecraft action.`;
      }
      record('continuation', { attempt: ++continuationsWithoutAction });
      prompt = continuePlaying;
    }
  } finally {
    signal.removeEventListener('abort', stop);
    session?.dispose();
    await client?.close().catch(() => {});
    await dispatcher.destroy();
  }
}
