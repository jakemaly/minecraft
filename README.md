# Mine AI Minecraft

Pi plays a fresh Minecraft 1.21.4 Survival world on seed `97996358`. [Mine AI MCP](https://github.com/aibengineering/mine-ai-mcp/tree/de4fef3260424d973bf81d0eaa15ff1a2f73ecff) owns the bot, actions, and survival reflexes. The dashboard starts and stops one run, shows tool activity, and embeds the bot’s first-person view. The Minecraft client can join as spectator.

Pi uses GPT-6.1 Terra at medium reasoning through ChatGPT sign-in. The prompt comes from the [published run manifest](https://huggingface.co/datasets/aibengineering/beat-the-game-minecraft/blob/de4bfb91972d263e592276c3a33a8b49d3e75d81/run.json) (CC BY 4.0, aibengineering). The original win used Claude Opus 5 in Claude Code. Each attempt begins from an empty world. Success requires the server to record the bot’s `minecraft:end/kill_dragon` advancement.

## Run locally

Use Linux, Node 24.15+, and Docker Engine 28+ with isolated bridge support. Accept the [Minecraft EULA](https://aka.ms/MinecraftEULA) before launching.

```sh
npm ci
docker pull eclipse-temurin:21-jre@sha256:a80c51f2d09a3e7e00d521f1c817bbceb6b3be94109b4a784d46078099882dda
docker build -f deploy/Dockerfile -t minecraft-mine-ai:de4fef3 .
npm start
```

Open http://127.0.0.1:2718. The default Pi credential directory is `~/.local/share/minecraft-agent/pi`. To sign in or refresh an expired login, run `PI_CODING_AGENT_DIR="$HOME/.local/share/minecraft-agent/pi" npx --no-install pi` and use `/login` to sign in with ChatGPT. Exit Pi before starting a run. The first run downloads and verifies Mojang’s server JAR. To use an existing verified copy, set `MINECRAFT_SERVER_JAR` to its absolute path.

For the user service, install the app at `~/.local/opt/minecraft-mine-ai` and install `deploy/minecraft-agent.service` in `~/.config/systemd/user`. Set `MINECRAFT_HOST` and `MINECRAFT_CLIENT_TAILSCALE_IP` in `~/.config/minecraft-mine-ai.env` to the server and operator addresses, respectively. The default ports are 2718 for the dashboard, 3000 for the viewer, and 25565 for Minecraft. Keep the corresponding tailnet grants. MCP crosses the sandbox on a private Unix socket. Docker runs the game and bot with an isolated bridge, read-only roots, dropped capabilities, resource limits, and per-run writable mounts.

Each attempt writes its manifest, Pi JSONL session, activity, server log and world, and Mine AI SQLite data under `~/.local/share/mine-ai`. A new run always gets a new directory. An early final model answer or terminal provider error ends the attempt.

## Verify

Run `npm run check` and `npm test`. After building the image, run `MINECRAFT_SMOKE=1 node --test test/smoke.test.mjs` for the Docker and Pi integration test.
