# Parallel Minecraft rollouts through one vLLM server

**Reviewed:** 2026-10-31
**Scope:** Feasibility of driving independent Minecraft worlds from one vLLM model server and recording useful online-RL trajectories. The repository pins `vllm==0.32.0`, so vLLM references below use its versioned docs.

## Finding

This is feasible. Run multiple isolated environment workers (one episode/world per worker) and have them submit independent, asynchronous policy requests to one vLLM endpoint. vLLM's continuous batching schedules requests together; the clients do not need to construct GPU batches themselves. The server loads one copy of the policy weights for a single replica and shares those weights across requests. Model throughput is still finite: active sequences, prompt/output token budgets, KV cache, and GPU capacity limit how many requests can make progress at once. [vLLM serving features](https://docs.vllm.ai/en/v0.32.0/), [vLLM scheduler limits](https://docs.vllm.ai/en/v0.32.0/cli/serve/), [vLLM data-parallel deployment](https://docs.vllm.ai/en/v0.32.0/serving/data_parallel_deployment/)

The scalable unit is **an environment worker**, not a dedicated model copy. Keep the worlds, bots, world seeds, episode state, and reward computation outside vLLM. A coordinator fans policy requests into the shared server and joins each result to the correct environment using a caller-generated `episode_id` / `step_id` (and, where useful, vLLM's request ID). vLLM's request ID is for correlating inference work; it does not give the model an independent persistent memory per bot. Send the current observation and any policy history/memory needed with each request. This is an architecture recommendation, based on vLLM's request-oriented serving API.

## Current machine constraint

At review time, `nvidia-smi` showed one GeForce RTX 3090 with 24,576 MiB total and 956 MiB free. The pinned Nemotron NVFP4 repository totals 75.1 GB on disk; NVIDIA's model card lists DGX Spark, RTX PRO 6000, H200, 2×H100, and GB200 as deployment targets. This workstation is outside those targets and cannot host this checkpoint as configured, so concurrency measurements for the selected model need a supported GPU host. [NVIDIA model card](https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4)

## Status in Molab Nemotron v1

The Molab notebook ([`02_molab_nemotron_app.py`](../../notebooks/02_molab_nemotron_app.py)) now serves the pinned Nemotron snapshot locally and selects it through Pi's OpenAI-compatible provider path. It keeps one active world and configures vLLM for one sequence; repeated runs produce independently seeded episodes. Concurrent environments remain a follow-up, rather than a feature of v1. ([`daemon.py`](../../src/minecraft_agent/daemon.py), [`pi_agent.mjs`](../../bridge/src/pi_agent.mjs), [`model_runtime.py`](../../src/minecraft_agent/model_runtime.py))

Exports now include a versioned offline rollout with step observations, generated tool actions, tool results, next observations, sparse server-verified success reward, termination/truncation, seed, model revision, inference settings, timing, screenshots, and raw state deltas. The Pi OpenAI-compatible path does not expose generated token IDs or sampled logprobs, so those fields are explicitly marked unavailable; this is useful trajectory data, but policy-gradient training that needs token-level probabilities requires a later inference-path change. ([`rollouts.py`](../../src/minecraft_agent/rollouts.py), [`pi_agent.mjs`](../../bridge/src/pi_agent.mjs))

To collect concurrent episodes, the next architecture step is a run coordinator with one isolated environment worker per world, explicit seed selection and per-worker ports, and independently correlated episode/step IDs. Workers can issue asynchronous requests to one vLLM endpoint; vLLM then schedules them under sequence, token, KV-cache, and hardware limits. Add backpressure/retry behavior and measure whole environment throughput before increasing `--max-num-seqs`; do not equate a shared endpoint with unlimited world capacity. Token-level RL data may need a dedicated vLLM token-in/token-out path alongside the unchanged gameplay harness.

Each containerized world currently reserves a 4 GiB Minecraft heap plus 1.5 GiB container headroom, and its bot container has a 2 GiB limit, before accounting for the coordinator, viewer rendering, and model server. That makes world count a CPU/RAM budget as well as a GPU inference budget. ([`daemon.py`](../../src/minecraft_agent/daemon.py), [`containers.py`](../../src/minecraft_agent/containers.py))

## What vLLM provides

- **Concurrent online inference:** vLLM serves independent OpenAI-compatible requests and schedules them under token and sequence limits. `--max-num-seqs` bounds sequences in one iteration; `--max-num-active-seqs` can lower the running admission limit; `--max-num-batched-tokens` bounds tokens per iteration. A queued-request cap can return HTTP 503 once saturated, so the coordinator should apply backpressure and retry rather than launch unbounded calls. [Serving limits and queue behavior](https://docs.vllm.ai/en/v0.32.0/cli/serve/)
- **More inference capacity:** For models/configurations that fit the hardware, data parallelism starts multiple weight replicas behind one API endpoint; it costs additional GPUs/memory. The docs specify `--max-num-seqs` per DP rank and queued-request limits per API server. A single replica is the natural first experiment; increase concurrency and then add data-parallel replicas if GPU capacity permits. [Data-parallel deployment](https://docs.vllm.ai/en/v0.32.0/serving/data_parallel_deployment/)
- **Training-oriented generation data:** The token-in/token-out API accepts prompt token IDs and returns generated token IDs and supports logprobs; it is explicitly described as useful when the caller owns tokenization and chat templating, such as an RL rollout worker. It requires `--enable-scale-out` or `--tokens-only`. For ordinary app integration, Chat Completions can also return token logprobs. [Token-in/token-out API](https://docs.vllm.ai/en/v0.32.0/serving/online_serving/token_in_token_out/), [OpenAI-compatible API](https://docs.vllm.ai/en/v0.32.0/serving/online_serving/openai_compatible_server/)
- **Valid action shapes:** Structured output constraints can force generated actions to a JSON schema, regex, grammar, or choice. This is useful for making the bot protocol parseable, but it constrains the model's output distribution; record the schema/version and generation settings with each rollout. [Structured outputs](https://docs.vllm.ai/en/v0.32.0/features/structured_outputs/)
- **Policy updates later:** If the project grows from fixed-policy data collection into online training, vLLM 0.32 provides a weight-transfer path to update inference weights and pause/resume generation around a sync. That machinery is unnecessary for initial rollout collection with fixed weights. [Weight transfer](https://docs.vllm.ai/en/v0.32.0/training/weight_transfer/)

## RL-friendly episode record

Use an episode/step schema that can be serialized independently of the notebook. RLDS is a useful reference: episodes contain steps with `is_first`, `is_last`, observation, action, reward, terminal state, discount, and extensible metadata. [RLDS format](https://github.com/google-research/rlds/blob/main/README.md)

For this project, each policy decision should record at least:

```text
episode_id, step_id, world_seed, environment/config revision
observation (and screenshot or durable artifact reference, if used)
action text/JSON plus the exact generated token IDs
per-token sampled logprobs; sampler settings; schema/prompt version
reward and optional reward components; next observation
terminated vs truncated; discount; action duration / in-game ticks
model checkpoint/revision, request ID, timestamps, error/retry status
```

Make one decision correspond to one bounded Minecraft action chunk (for example, a short set of controls for a fixed number of ticks), then observe the result and assign the environment reward to that chunk. This gives a clear transition `(observation_t, action_t, reward_t, observation_t+1)` even though the policy action itself may be several generated tokens. Keep `terminated` separate from `truncated` so a time limit or worker failure is not mislabeled as task success. These are data-design recommendations; RLDS supplies the episode/step semantics, while vLLM supplies generated tokens and optional logprobs.

If using top-k/top-p sampling and training from token logprobs, preserve the actual sampling support. vLLM 0.32's sampling-mask feature can return it, but it requires processed logprobs and a top-k bound, and it disables the fused FlashInfer sampler for all requests, which may reduce rollout throughput. This is an opt-in tradeoff to benchmark, not a default to enable preemptively. [Sampling-mask guide](https://docs.vllm.ai/en/v0.32.0/training/sampling_mask/)

The world RNG seed and the vLLM sampling seed are different controls: record/set the environment seed in the environment runner, and set a separate vLLM sampling seed when reproducible language sampling is needed. vLLM documents `seed` as a generation random seed. [Sampling parameters](https://docs.vllm.ai/en/v0.32.0/api/vllm/sampling_params/)

## Proposed first experiment

1. Start with one vLLM server and a small pool of isolated worlds/bots. Launch requests concurrently from the environment coordinator; do not make a separate model server per bot.
2. Fix one prompt format, one bounded action schema, and one action duration. Use short outputs and record complete episode metadata above. Keep the first run fixed-policy; defer online weight sync.
3. Sweep active environments (1, 2, 4, 8, …) while measuring successful decisions/sec, p50/p95 action latency, queue depth/503s, GPU memory/KV-cache pressure, CPU/RAM per world, and episode completion rate. Benchmark with the actual observation sizes and outputs, not only synthetic prompts.
4. Compare environment-only throughput (worlds stepping without model calls) with inference-limited throughput. This identifies whether to spend effort on Minecraft server/process isolation, reducing observation/prompt size, action batching, or adding a vLLM replica.

vLLM provides `vllm bench serve` for online request-rate/concurrency measurements, including `--request-rate` and `--max-concurrency`; the real environment coordinator should then run an end-to-end sweep because synthetic serving benchmarks cannot reveal Minecraft world CPU and process overhead. [vLLM serving benchmark CLI](https://docs.vllm.ai/en/v0.32.0/cli/bench/serve/)

## Unknowns requiring a local benchmark

Documentation establishes that the inference topology is supported, but it cannot give a useful number of simultaneous Minecraft worlds for this machine. That number depends on the actual GPU/model quantization, prompt lengths (especially if observations include images), output-token/action length, scheduler/KV-cache settings, world-server CPU/RAM, and acceptable decision latency. Also validate that the chosen model follows the action schema reliably and that the collected token-level probabilities line up with the eventual RL trainer's policy objective. No performance capacity claim is made here.
