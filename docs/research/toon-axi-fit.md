# TOON and AXI fit for the Minecraft agent harness

Research checked 2026-11-10 against the projects' official documentation and source repositories.

## Findings

### TOON

TOON v4.4 is a lossless textual encoding of the JSON data model. Its token savings come mainly from representing repeated, uniform object arrays as one field header plus rows. That shape is a good match for inventory, nearby entities, blocks, and other repeated observation records. Keep the canonical event log and internal data as JSON; encode only at the model-facing boundary so existing event readers and exports stay stable. [TOON spec](https://toonformat.dev/reference/spec) · [Format overview](https://toonformat.dev/guide/format-overview)

TOON is not a semantic summarizer. It will not remove stale tool transcripts, collapse duplicate lessons, prioritize the current objective, or explain a death in plain language. Deeply nested and irregular data may save little or even grow relative to compact JSON; the official efficiency analysis says results depend on structure, with arrays-of-arrays an exception. Keep narrative summaries as narrative text, and use TOON for uniform structured data. [Efficiency model](https://toonformat.dev/reference/efficiency-formalization)

The published retrieval benchmark reports 72.2% accuracy versus 71.4% for formatted JSON using 42.6% fewer tokens, across 244 retrieval questions and four tested models. Token counts use `o200k_base`; the savings are workload- and tokenizer-dependent. It is not a Minecraft task benchmark, and it does not test whether TOON prevents unsafe play or improves long-horizon planning. [TOON benchmarks](https://toonformat.dev/guide/benchmarks)

For model-generated TOON, the project recommends showing a short example and strict decoding/validation. This harness primarily needs to *encode* observations and receive native tool calls, so there is no reason to make model-generated TOON the action protocol. [Using TOON with LLMs](https://toonformat.dev/guide/llm-prompts)

### AXI

AXI is guidance for agent-facing CLI design, not a required package or wire protocol. The source lists ten principles: token-efficient output, minimal schemas, truncation with explicit full views, precomputed aggregates, definitive empty states, structured errors, ambient context, content-first defaults, contextual next-step hints, and concise help. The principles can be applied to the existing tools and UI without replacing their transport with a CLI. [AXI source and principles](https://github.com/kunchenguid/axi) · [AXI overview](https://axi.md/)

The direct fits here are: default to a few decision-relevant fields; cap long tool results but state exact counts and how to fetch details; include health/death/goal/inventory summaries the harness already knows; distinguish empty results from failures; and make tool errors actionable. For humans, a compact status card and timeline summary should lead, with raw events, scripts, screenshots, and full output available on expansion. That is an application of AXI's concise defaults and contextual disclosure, not a requirement to display TOON syntax to people. [AXI skill](https://github.com/kunchenguid/axi/blob/main/.agents/skills/axi/SKILL.md)

AXI's published results are promising but domain-limited: 490 browser runs and 425 GitHub runs, using Claude Sonnet 4.6 and LLM judging. Treat them as support for interface-design principles, not a performance guarantee for this game harness. [AXI benchmark and caveats](https://github.com/kunchenguid/axi) · [AXI caveats](https://axi.md/)

## Practical fit

1. Keep JSON for durable traces and tool transport; test TOON at the prompt boundary for uniform observation collections only.
2. Reduce/select fields before encoding. Format compression must not substitute for relevance filtering.
3. Make one small, readable run summary the shared source for the model and human view: objective status, position/dimension, health/food, armor/tools, deaths since last checkpoint, current recovery point, and the latest verified action/result.
4. Move long scripts, full shell outputs, and old events behind bounded previews plus explicit detail retrieval. Preserve exact error status, truncation counts, and stable event IDs.
5. Evaluate on this run's actual retrieval/action cases (including remembering death lessons) before making TOON the default. Compare encoded and source forms, token counts with the selected model tokenizer, answer correctness, and action outcomes.

## Limits

Neither TOON nor AXI addresses the game agent's strategic policy by itself. The active cause to investigate is whether repeated deaths are being translated into persistent, prioritized safety constraints and a useful recovery/checkpoint strategy. A compact format can make those lessons easier to consume, but the harness must preserve and surface the lessons in the right place.
