# Acacia: Self-Improving Minecraft Agent
Acacia empowers large language models (LLMs) to embody a stateful player in digital environment, and to take actions within it.

## Intro
<img width="1332" height="852" alt="image" src="https://github.com/user-attachments/assets/a1fa1594-3d81-4fe1-8e75-560a4f1c3c1e" />

## What is Acacia?
Acacia is a self-improving model harness and agent system  - it uses a fork of [Pi](https://pi.dev), a minimal agent harness giving models the ability to read, write, and run files. With this capability, it can drive AXI tools and create scripts using the [Mineflayer API](https://github.com/prismarinejs/mineflayer), a Minecraft bot library that allows the agent to control the player in space. 

Inspired by Nous Research's Hermes Agent and Voyager, Acacia learns by continuously programming itself new Skills and Mineflayer scripts, enriching its capabilities as time goes on. Upon death, the model creates a reflection on why it died and saves new Skills to its Library. Additionally, model deaths and their environments are turned into negative rewards for reinforcement learning (RL), allowing us to train more risk-adverse agents. On simulated environments, RL-trained models choose risk-aware alternatives 43% more often compared to untrained counterparts.

<img width="677" height="679" alt="image" src="https://github.com/user-attachments/assets/f41a589b-2e1f-4b56-9871-9bd1d22835e8" />

## So what?
Acacia represents a fundamental shift in AI systems, with implications for physical intelligence, robotics, and recursive self-improvement. Traditional long-horizon agent tasks like those found in Software Engineering have a static environment - if you don't change anything, nothing happens. In Minecraft, the world is constantly changing around you, and no two environments will ever be the same. Acacia shows that dynamic environments demand continuous learning and adaptation that persist past training cutoffs, especially for Transformer-based models (LLMs, VLAs, etc). 

## Closing
Acacia is still a work-in-progress! Currently working on a new system to beat Hardcore (aka without dying once) with a fine-tuned System One model for enhanced reactivity, and getting random-seed beacon recreations of previous trials.

## Run the app

Run the app on a Linux host with Python 3.11+, [uv](https://docs.astral.sh/uv/), Node.js 22+, npm, Java 21+, and Docker. Open the dashboard in a browser on any device that can reach the host. From the repository root, install the app and bridge dependencies, then start the notebook:

```sh
uv sync --extra notebook
npm ci --prefix bridge
./scripts/run_notebook.sh
```

Open `http://<server-address>:2718` from your browser.
