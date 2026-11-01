"""Build compact, versioned offline RL episodes from the existing event trace."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


ROLLOUT_SCHEMA_VERSION = 1


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _inventory_counts(observation: Any) -> dict[str, float]:
    if not isinstance(observation, Mapping):
        return {}
    summary = observation.get("inventory_summary") or observation.get("inventory") or {}
    counts: dict[str, float] = {}
    if isinstance(summary, Mapping):
        for name, value in summary.items():
            if isinstance(value, Mapping):
                value = value.get("count")
            number = _number(value)
            if number is not None:
                counts[str(name)] = number
    elif isinstance(summary, list):
        for item in summary:
            if not isinstance(item, Mapping):
                continue
            name = item.get("name") or item.get("item")
            number = _number(item.get("count"))
            if name is not None and number is not None:
                counts[str(name)] = number
    return counts


def state_delta(before: Any, after: Any) -> dict[str, Any]:
    """Derive numeric changes while preserving full observations on the transition."""
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return {"available": False}
    before_player = before.get("player") if isinstance(before.get("player"), Mapping) else {}
    after_player = after.get("player") if isinstance(after.get("player"), Mapping) else {}
    player_delta: dict[str, float] = {}
    for key in sorted(set(before_player) | set(after_player)):
        left, right = _number(before_player.get(key)), _number(after_player.get(key))
        if left is not None and right is not None:
            player_delta[key] = right - left
    before_position = before_player.get("position") if isinstance(before_player.get("position"), Mapping) else {}
    after_position = after_player.get("position") if isinstance(after_player.get("position"), Mapping) else {}
    position_delta = {
        axis: _number(after_position.get(axis)) - _number(before_position.get(axis))
        for axis in ("x", "y", "z")
        if _number(before_position.get(axis)) is not None and _number(after_position.get(axis)) is not None
    }
    before_items, after_items = _inventory_counts(before), _inventory_counts(after)
    inventory_delta = {
        name: after_items.get(name, 0.0) - before_items.get(name, 0.0)
        for name in sorted(set(before_items) | set(after_items))
        if after_items.get(name, 0.0) != before_items.get(name, 0.0)
    }
    before_events = before.get("recent_events") if isinstance(before.get("recent_events"), list) else []
    after_events = after.get("recent_events") if isinstance(after.get("recent_events"), list) else []
    seen = {repr(item) for item in before_events}
    added_events = [item for item in after_events if repr(item) not in seen]
    return {
        "available": True,
        "player_numeric": player_delta,
        "position": position_delta,
        "inventory_counts": inventory_delta,
        "alive_changed": before_player.get("alive") != after_player.get("alive") if "alive" in before_player or "alive" in after_player else None,
        "recent_events_added": added_events,
    }


def _event_payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def _tool_actions(events: list[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    finished = {
        str(_event_payload(event).get("tool_call_id")): _event_payload(event)
        for event in events
        if event.get("type") == "tool_call_finished" and _event_payload(event).get("tool_call_id")
    }
    generated = {
        str(_event_payload(event).get("tool_call_id")): _event_payload(event)
        for event in events
        if event.get("type") == "rl_tool_action" and _event_payload(event).get("tool_call_id")
    }
    actions: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    calls = {
        str(_event_payload(event).get("tool_call_id")): _event_payload(event)
        for event in events
        if event.get("type") == "tool_call_started" and _event_payload(event).get("tool_call_id")
    }
    calls.update({call_id: {**calls.get(call_id, {}), **call} for call_id, call in generated.items()})
    for call_id, call in calls.items():
        call_id = str(call.get("tool_call_id", ""))
        result = finished.get(call_id)
        action = {
            "tool_call_id": call.get("tool_call_id"),
            "tool": call.get("tool"),
            "arguments": call.get("arguments", {}),
        }
        if result:
            results.append({
                "tool_call_id": call.get("tool_call_id"),
                "tool": result.get("tool", call.get("tool")),
                "is_error": result.get("is_error"),
                "duration_ms": result.get("duration_ms"),
                "result": result.get("result"),
            })
        actions.append(action)
    return actions, results


def build_rollout_export(manifest: Mapping[str, Any], events: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows = list(events)
    native_transitions = [index for index, row in enumerate(rows) if row.get("type") == "agent_transition" and isinstance(row.get("payload"), Mapping)]
    turn_starts = [index for index, row in enumerate(rows) if row.get("type") == "agent_turn_started" and isinstance(_event_payload(row).get("observation"), Mapping)]
    finished = next((row for row in reversed(rows) if row.get("type") == "agent_run_finished"), None)
    if finished is None:
        finished = next((row for row in reversed(rows) if row.get("type") in {"run_stopped", "run_failed"}), None)
    finish_payload = _event_payload(finished) if finished else {}
    final_status = str(finish_payload.get("status") or ("failed" if finished and finished.get("type") == "run_failed" else "incomplete"))
    run_id = str(manifest.get("run_id", "unknown"))
    model = manifest.get("model") if isinstance(manifest.get("model"), Mapping) else {}
    game = manifest.get("game") if isinstance(manifest.get("game"), Mapping) else {}
    inference = manifest.get("inference_config", {})

    # Preserve completed transitions, and reconstruct interrupted turns from the
    # existing turn and tool events so stopping mid-turn does not erase the data.
    records: list[tuple[int, Mapping[str, Any], list[Mapping[str, Any]], Mapping[str, Any] | None]] = []
    used_native: set[int] = set()
    for start_number, start_index in enumerate(turn_starts):
        next_start = turn_starts[start_number + 1] if start_number + 1 < len(turn_starts) else len(rows)
        native_index = next((index for index in native_transitions if start_index < index < next_start), None)
        chunk = rows[start_index:next_start]
        if native_index is not None:
            used_native.add(native_index)
            records.append((native_index, _event_payload(rows[native_index]), chunk, _event_payload(rows[start_index])))
        else:
            start_payload = _event_payload(rows[start_index])
            actions, results = _tool_actions(chunk)
            records.append((start_index, {
                "step_id": start_payload.get("step_id"),
                "observation": start_payload.get("observation"),
                "screenshot": start_payload.get("screenshot"),
                "verified_goal_before": start_payload.get("verified_goal"),
                "actions": actions,
                "tool_calls": actions,
                "tool_results": results,
                "token_metrics_status": "unavailable_through_pi_openai_compatible_provider",
            }, chunk, start_payload))
    for index in native_transitions:
        if index not in used_native:
            records.append((index, _event_payload(rows[index]), [], None))
    records.sort(key=lambda item: item[0])

    episode_steps: list[dict[str, Any]] = []
    used_step_ids: set[Any] = set()
    for index, (event_index, payload, chunk, start_payload) in enumerate(records):
        last = index == len(records) - 1
        event = rows[event_index]
        before = payload.get("observation")
        after = payload.get("next_observation")
        if after is None and index + 1 < len(records):
            after = records[index + 1][1].get("observation")
        if after is None and last:
            after = finish_payload.get("final_observation")
        verified = payload.get("verified_goal_after") or {}
        next_verified = records[index + 1][1].get("verified_goal_before") if index + 1 < len(records) else None
        if not verified and isinstance(next_verified, Mapping):
            verified = next_verified
        terminated = verified.get("status") == "passed"
        truncated = bool(last and not terminated and final_status in {"stopped", "paused", "failed", "incomplete", "timeout"})
        raw_step_id = payload.get("step_id", index + 1)
        step_id = raw_step_id
        try:
            duplicate_step = step_id in used_step_ids
        except TypeError:
            duplicate_step = True
        if duplicate_step:
            step_id = index + 1
        used_step_ids.add(step_id)
        actions = payload.get("actions", [])
        tool_calls = payload.get("tool_calls", [])
        tool_results = payload.get("tool_results", [])
        if chunk and start_payload is not None and payload.get("tool_results") is None:
            fallback_actions, fallback_results = _tool_actions(chunk)
            if not actions:
                actions = fallback_actions
            if not tool_calls:
                tool_calls = fallback_actions
            tool_results = fallback_results
        model_messages = [_event_payload(row) for row in chunk if row.get("type") == "model_message"]
        episode_steps.append({
            "schema_version": ROLLOUT_SCHEMA_VERSION,
            "episode_id": run_id,
            "step_id": step_id,
            "seed": game.get("world_seed"),
            "model": model.get("id"),
            "model_revision": manifest.get("model_revision") or model.get("revision"),
            "inference_config": inference,
            "timestamp": event.get("wall_time"),
            "elapsed_seconds": event.get("elapsed_seconds"),
            "observation": before,
            "screenshot": payload.get("screenshot"),
            "actions": actions,
            "tool_calls": tool_calls,
            "tool_results": tool_results,
            "next_observation": after,
            "raw_state_delta": state_delta(before, after),
            "verified_goal_before": payload.get("verified_goal_before"),
            "verified_goal_after": verified,
            "reward": 1.0 if terminated else 0.0,
            "terminated": terminated,
            "truncated": truncated,
            "usage_and_latency": [
                message for message in model_messages
            ],
            "token_ids": payload.get("token_ids"),
            "token_logprobs": payload.get("token_logprobs"),
            "token_metrics_status": payload.get("token_metrics_status", "unavailable_through_pi_openai_compatible_provider"),
        })
    episode = {
        "schema_version": ROLLOUT_SCHEMA_VERSION,
        "episode_id": run_id,
        "world_id": manifest.get("world_id"),
        "seed": game.get("world_seed"),
        "objective": manifest.get("objective"),
        "goal": manifest.get("goal"),
        "model": model.get("id"),
        "model_revision": manifest.get("model_revision") or model.get("revision"),
        "inference_config": inference,
        "status": final_status,
        "step_count": len(episode_steps),
        "total_reward": sum(step["reward"] for step in episode_steps),
        "terminated": any(step["terminated"] for step in episode_steps),
        "truncated": bool(episode_steps and episode_steps[-1]["truncated"]) or (not episode_steps and final_status in {"stopped", "paused", "failed", "incomplete", "timeout"}),
        "created_at": manifest.get("created_at"),
        "token_metrics_status": "unavailable through Pi OpenAI-compatible provider" if episode_steps else "no_policy_steps",
    }
    return episode, episode_steps
