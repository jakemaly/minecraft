# Minecraft Agent

Mineflayer runs the bot, Pi runs the agent, and the local web app shows the live Prismarine Viewer plus the saved conversation and action trace.

## Start

Requires Python 3.11+, Node 22, Java 21, and Tailscale; Docker adds process isolation and is optional.

Install the locked bridge dependencies with `cd bridge && npm ci`, then run `scripts/run_app.sh`. Open `http://<Linux-Tailscale-IP>:2718` from the paired computer. The viewer uses port `3000`; Minecraft uses `25565`.

Connect ChatGPT in the app, choose a goal, and start a run. Each game turn starts a fresh model session to keep the context bounded; Pi rebuilds it from the objective, current observation and image, recent frame summaries, `plan.md`, and `memory.md`. The transcript is saved for you to inspect but is not replayed wholesale into the next turn. Workspace files and the event log persist across every turn in that run. Stopping keeps its files and world under `~/.local/share/minecraft-agent/`; a new run starts with an empty plan, memory, and scripts. Only tested skills explicitly published with `mc-skill publish skills/<name>` carry over.

The optional `notebooks/01_vllm_server.py` downloads and serves the pinned Nemotron model for standalone inference (`uv run notebooks/01_vllm_server.py`). It does not run a second Minecraft agent.

The trace records the model-visible context, user-visible messages, tool calls, and results. Private chain-of-thought is not saved or displayed.

## Molab Nemotron app

Open `notebooks/02_molab_nemotron_app.py` from this repository in Molab. Its controls prepare the pinned Node bridge and Chromium viewer capture, download the pinned Nemotron snapshot, start vLLM on `127.0.0.1`, initialize the existing Pi run harness, and manage/export a Minecraft run. The Java server, bot, Pi agent, viewer capture, and vLLM process all run inside the notebook host. The local vLLM provider is configured as the only model provider for this app; it never falls back to OpenAI.

The downloaded run archive contains the original event trace and a versioned offline export at `run/rollout/episode.json` and `run/rollout/transitions.jsonl`. Sparse reward is `1` only when the server verifies the selected objective; state deltas remain raw signals. Token IDs and log probabilities are marked unavailable because Pi's OpenAI-compatible adapter does not expose them.
