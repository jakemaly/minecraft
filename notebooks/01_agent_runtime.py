# /// script
# requires-python = ">=3.11"
# dependencies = ["marimo==0.23.11"]
# ///
"""Thin Marimo control panel for the persistent Pi/Mineflayer Linux service."""

import marimo

__generated_with = "0.23.11"
app = marimo.App(width="full")


@app.cell
def _():
    import html
    import json
    import marimo as mo
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    _source = str(repo_root / "src")
    if _source not in sys.path:
        sys.path.insert(0, _source)
    from minecraft_agent.daemon_client import ensure_daemon, request
    from minecraft_agent.session import get_session

    session = get_session()
    try:
        ensure_daemon(repo_root)
        startup_error = None
    except Exception as exc:
        startup_error = f"{type(exc).__name__}: {str(exc)[:500]}"

    return Path, html, json, mo, request, session, startup_error


@app.cell
def _(mo):
    callback = mo.ui.text(label="OAuth callback URL (only if localhost did not complete)", placeholder="http://127.0.0.1:1455/auth/callback?code=…&state=…", full_width=True)
    connect = mo.ui.run_button(label="Connect with ChatGPT")
    submit_callback = mo.ui.run_button(label="Submit callback URL")
    run_kind = mo.ui.dropdown(options=["diamond_pilot", "dragon_proof"], value="diamond_pilot", label="Fresh Normal Survival trial")
    start = mo.ui.run_button(label="Start fresh world and agent")
    pause = mo.ui.run_button(label="Pause, release controls, freeze and save")
    resume = mo.ui.run_button(label="Resume same world and workspace")
    stop = mo.ui.run_button(label="Stop and save run")
    observer = mo.ui.text(label="Mac Minecraft username", placeholder="Your Java profile name")
    set_observer = mo.ui.run_button(label="Set joined observer to spectator")
    export = mo.ui.run_button(label="Export run trace, world and video")
    refresh = mo.ui.refresh(options=[5, 15], default_interval=5, label="Refresh status")

    return callback, connect, export, observer, pause, refresh, resume, run_kind, set_observer, start, stop, submit_callback


@app.cell
def _(callback, connect, export, observer, pause, refresh, request, resume, run_kind, session, set_observer, start, startup_error, stop, submit_callback):
    results = []

    def once(name, button, method, params=None, timeout=30):
        if session.claim_button(name, button.value):
            try:
                value = request(method, params or {}, timeout=timeout)
                results.append({"action": name, "result": value})
                return value
            except Exception as error:
                results.append({"action": name, "error": f"{type(error).__name__}: {str(error)[:500]}"})
        return None

    once("connect_chatgpt", connect, "auth_start")
    once("oauth_callback", submit_callback, "auth_callback", {"url": callback.value}, 60)
    once("start_run", start, "start_run", {"kind": run_kind.value})
    once("pause_run", pause, "pause_run", timeout=30)
    once("resume_run", resume, "resume_run", timeout=30)
    once("stop_run", stop, "stop_run", timeout=90)
    once("set_observer", set_observer, "set_observer", {"username": observer.value})
    artifact = once("export_run", export, "export_run", timeout=120)
    refresh_counter = refresh.value

    try:
        status = request("status", timeout=20)
        status_error = None
    except Exception as exc:
        status = {}
        status_error = f"{type(exc).__name__}: {str(exc)[:500]}"

    return artifact, refresh_counter, results, startup_error, status, status_error


@app.cell
def _(mo, session, status):
    active_for_requests = status.get("active_run") or {}
    contexts = [event for event in (active_for_requests.get("recent_events") or []) if event.get("type") == "model_context"]
    request_choices = {
        f"#{event.get('sequence', '?')} · {(event.get('payload') or {}).get('model', 'model')} · {str((event.get('payload') or {}).get('request_id', ''))[-8:]}": (event.get("payload") or {}).get("request_id")
        for event in contexts
    }
    picker_default_label = next((label for label, request_id in request_choices.items() if request_id == session.reports.get("observability_request_id")), None)
    picker_default_label = picker_default_label or (next(reversed(request_choices)) if request_choices else None)
    request_picker = mo.ui.dropdown(options=list(request_choices), value=picker_default_label, label="Inspect a model request") if request_choices else None

    return request_choices, request_picker


@app.cell
def _(Path, artifact, callback, connect, export, html, json, mo, observer, pause, refresh, refresh_counter, request_choices, request_picker, results, resume, run_kind, session, set_observer, start, startup_error, status, status_error, stop, submit_callback):
    import base64

    auth = status.get("chatgpt_auth") or {}
    active = status.get("active_run") or {}
    events = active.get("recent_events") or []
    authorization_url = auth.get("authorization_url")
    auth_panel = mo.md("ChatGPT subscription: **connected**" if auth.get("connected") else "ChatGPT subscription: **not connected**")
    if authorization_url:
        auth_panel = mo.vstack([
            auth_panel,
            mo.Html(f'<p><a href="{html.escape(authorization_url, quote=True)}" target="_blank" rel="noopener">Continue with ChatGPT</a></p>'),
            mo.md("If the browser returns to a localhost address on your Mac and cannot connect, copy that full address into the callback field above. The callback URL is used only for sign-in and is not recorded in run traces."),
            callback,
            submit_callback,
        ])
    elif not auth.get("connected"):
        auth_details = []
        if auth.get("login_in_progress"):
            auth_details.append(mo.md(f"ChatGPT sign-in is starting. {html.escape(str(auth.get('progress') or 'Waiting for the provider.'))}"))
        if auth.get("error"):
            auth_details.append(mo.md(f"Sign-in error: `{html.escape(str(auth['error']))}`"))
        auth_details.extend([connect, mo.md("This uses Pi’s OpenAI Sign in with ChatGPT OAuth provider. API-key billing fallback is disabled.")])
        auth_panel = mo.vstack([auth_panel, *auth_details])
    else:
        catalog = auth.get("account_model_catalog") or {}
        model = catalog.get("chosen") or status.get("selected_model")
        if model:
            model_label = "Pinned default model" if catalog.get("default_model_pinned") else "Selected account model"
            pinned_note = mo.md("This model is saved as the default for future runs.") if catalog.get("default_model_pinned") else None
            candidates = catalog.get("candidates", [])
            candidate_rows = "".join(
                f"<li><code>{html.escape(str(candidate.get('id', 'unknown')))}</code> · "
                f"{html.escape(str(candidate.get('display_name') or candidate.get('name') or ''))} · "
                f"{int(candidate.get('context_window') or 0):,}-token window · "
                f"{'image' if candidate.get('image_input') else 'no image'} · "
                f"{'reasoning' if candidate.get('reasoning') else 'no reasoning'}</li>"
                for candidate in candidates
            )
            candidate_panel = mo.Html(
                f'<details style="margin:8px 0"><summary>Available account models ({len(candidates)})</summary>'
                f'<ul style="margin:8px 0;padding-left:24px;line-height:1.8">{candidate_rows}</ul></details>'
            )
            auth_panel = mo.vstack([
                auth_panel,
                mo.md(f"{model_label}: **{model.get('display_name') or model.get('id')}** (`{model.get('id')}`) · context `{model.get('context_window')}` · image input enabled"),
                *([pinned_note] if catalog.get("default_model_pinned") else []),
                mo.md(f"Pinned default: `{catalog.get('default_model_id')}` · {'yes' if catalog.get('default_model_pinned') else 'no'}") if catalog.get("default_model_id") else mo.md("No model default is pinned."),
                mo.md(f"Pinned model unavailable: `{html.escape(str(catalog['pin_error']))}`") if catalog.get("pin_error") else candidate_panel,
            ])
        elif catalog.get("pin_error"):
            auth_panel = mo.vstack([auth_panel, mo.md(f"Pinned model unavailable: `{html.escape(str(catalog['pin_error']))}`")])
        if auth.get("error"):
            auth_panel = mo.vstack([auth_panel, mo.md(f"Model catalog error: `{html.escape(str(auth['error']))}`"), connect])

    controls = mo.vstack([
        mo.md("## Run controls\n\nStarting a trial creates a fresh random-seed Normal Survival world, starts one Mineflayer control bot and the observer viewer, makes one image-plus-native-tool model probe, then starts the selected run. The world defaults all human players to spectator; the control bot is explicitly switched to Survival. The diamond pilot runs for up to four active hours; the dragon proof is capped at 72 active hours."),
        mo.hstack([run_kind, start]),
        mo.hstack([pause, resume, stop]),
        mo.hstack([observer, set_observer]),
        export,
    ])

    live_states = {"probing_model", "running", "paused"}
    viewer = None
    if active.get("viewer_url") and active.get("state") in live_states:
        viewer = mo.Html(f'<iframe title="Live first-person Prismarine Viewer" src="{html.escape(active["viewer_url"], quote=True)}" style="width:100%;height:560px;border:1px solid #ccc;border-radius:8px;background:#101820" loading="lazy" allow="fullscreen"></iframe>')

    selected_label = request_picker.value if request_picker is not None else None
    selected_request_id = request_choices.get(selected_label)
    if selected_request_id:
        session.reports["observability_request_id"] = selected_request_id
    context_event = next((event for event in reversed(events) if event.get("type") == "model_context" and (event.get("payload") or {}).get("request_id") == selected_request_id), None)
    context = (context_event or {}).get("payload") or {}
    frame_event = next((event for event in reversed(events) if event.get("type") == "viewer_frame" and (not context_event or event.get("sequence", 0) < context_event.get("sequence", 0))), None)
    if frame_event is None and context_event and active.get("run_directory"):
        # The status response tails only 100 events; recover the matching frame from the durable trace.
        event_path = Path(active["run_directory"]) / "events" / "events.jsonl"
        target_sequence = context_event.get("sequence", 0)
        try:
            with event_path.open("rb") as trace:
                _trace_position = trace.seek(0, 2)
                _trace_carry = b""
                while _trace_position > 0 and frame_event is None:
                    _trace_start = max(0, _trace_position - 64 * 1024)
                    trace.seek(_trace_start)
                    chunk = trace.read(_trace_position - _trace_start) + _trace_carry
                    lines = chunk.split(b"\n")
                    _trace_carry = lines[0] if _trace_start else b""
                    for line in reversed(lines[1:] if _trace_start else lines):
                        try:
                            candidate = json.loads(line)
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            continue
                        if candidate.get("type") == "viewer_frame" and candidate.get("sequence", 0) < target_sequence:
                            frame_event = candidate
                            break
                    _trace_position = _trace_start
        except OSError:
            pass
    frame_preview = mo.md("No model-input frame has been captured for this run yet.")
    if frame_event and active.get("run_directory"):
        frame_data = frame_event.get("payload") or {}
        frame_path = Path(active["run_directory"]) / str(frame_data.get("path", ""))
        if frame_path.is_file():
            frame_bytes = frame_path.read_bytes()
            frame_url = "data:image/png;base64," + base64.b64encode(frame_bytes).decode("ascii")
            frame_preview = mo.Html(f'<figure style="margin:0;max-width:640px"><img alt="Latest first-person image sent to the model" src="{frame_url}" style="display:block;width:100%;height:auto;image-rendering:auto;border:1px solid #334155;border-radius:8px;background:#0f172a"><figcaption style="font:12px system-ui;color:#64748b;margin-top:6px">Frame {frame_data.get("path", "")} · {frame_data.get("width", "?")}×{frame_data.get("height", "?")} · SHA-256 {html.escape(str(frame_data.get("sha256", "unavailable"))[:16])}…</figcaption></figure>')

    request_id = context.get("request_id")
    model_message_event = next((event for event in reversed(events) if event.get("type") == "model_message" and (event.get("payload") or {}).get("request_id") == request_id), None)
    model_message = (model_message_event or {}).get("payload") or {}
    reasoning_updates = [
        (event.get("payload") or {}).get("delta", "")
        for event in events
        if event.get("type") == "reasoning_update" and (event.get("payload") or {}).get("request_id") == request_id
    ]
    reasoning = model_message.get("exposed_reasoning") or "".join(reasoning_updates)
    reasoning_panel = mo.md("No provider-exposed reasoning is available for this request yet. Hidden chain-of-thought is not inferred or fabricated.")
    if reasoning:
        reasoning_panel = mo.Html(f'<div style="white-space:pre-wrap;overflow-wrap:anywhere;max-height:440px;overflow:auto;border-left:4px solid #8b5cf6;padding:12px 16px;background:#faf5ff;color:#312e50;border-radius:4px">{html.escape(reasoning)}</div>')
    elif model_message:
        visibility = html.escape(str(model_message.get("reasoning_visibility", "provider did not expose reasoning")))
        reasoning_panel = mo.md(f"Provider status: **{visibility}**. The model’s private reasoning is not available to the harness.")

    answer = model_message.get("text") or "No final answer has been recorded for this request yet."
    answer_panel = mo.Html(f'<pre style="white-space:pre-wrap;overflow-wrap:anywhere;max-height:260px;overflow:auto;padding:12px;background:#f8fafc;border:1px solid #cbd5e1;border-radius:8px">{html.escape(answer)}</pre>')

    segments = context.get("segments") or []
    model_info = active.get("model") or {}
    context_window = context.get("context_window") or model_message.get("context_window") or model_info.get("context_window")
    usage = model_message.get("usage") or {}
    exact_input = usage.get("input_tokens")
    estimated_input = context.get("estimated_input_tokens")
    shown_input = exact_input if isinstance(exact_input, (int, float)) else estimated_input
    total_estimate = sum(max(0, int(segment.get("estimated_tokens") or 0)) for segment in segments)
    if isinstance(shown_input, (int, float)) and total_estimate > shown_input and total_estimate:
        scale = shown_input / total_estimate
    else:
        scale = 1.0
    colors = {
        "system_prompt": "#4f46e5", "tool_definitions": "#8b5cf6", "current_observation": "#0284c7",
        "verified_goal": "#0f766e", "frame_summaries": "#16a34a", "plan": "#65a30d",
        "memory": "#ca8a04", "turn_instructions": "#ea580c", "current_image": "#dc2626",
        "tool_round_history": "#db2777", "unattributed": "#64748b",
    }
    context_window_value = int(context_window) if isinstance(context_window, (int, float)) and context_window > 0 else None
    used_for_bar = max(0, int(shown_input or 0))
    allocated = sum(max(0, int(segment.get("estimated_tokens") or 0)) * scale for segment in segments)
    remainder = max(0, used_for_bar - allocated) if exact_input is not None else 0
    if remainder:
        segments_for_bar = [*segments, {"key": "unattributed", "label": "Provider/image overhead", "estimated_tokens": remainder}]
    else:
        segments_for_bar = segments
    denominator = context_window_value or max(used_for_bar, total_estimate, 1)
    bar_parts = []
    legend_parts = []
    for segment in segments_for_bar:
        key = str(segment.get("key", "unattributed"))
        label = str(segment.get("label", key))
        estimate = max(0, float(segment.get("estimated_tokens") or 0))
        weight = estimate * scale if key != "unattributed" else estimate
        width = min(100.0, weight / denominator * 100)
        color = colors.get(key, "#64748b")
        if width > 0:
            bar_parts.append(f'<span title="{html.escape(label)} · {round(weight):,} tokens allocated" style="display:block;height:100%;width:{width:.5f}%;background:{color};min-width:1px"></span>')
        preview = html.escape(str(segment.get("preview") or "(no text preview)"))
        legend_parts.append(f'<details style="margin:6px 0"><summary style="cursor:pointer"><span style="display:inline-block;width:12px;height:12px;margin-right:8px;border-radius:3px;background:{color}"></span><b>{html.escape(label)}</b> · {int(segment.get("estimated_tokens") or 0):,} estimated tokens</summary><pre style="white-space:pre-wrap;overflow-wrap:anywhere;max-height:220px;overflow:auto;padding:8px;background:#f8fafc;border:1px solid #cbd5e1;border-radius:5px">{preview}</pre></details>')
    context_bar = mo.md("No model context snapshot has been recorded yet.")
    if segments and context_window_value:
        utilization = min(100.0, used_for_bar / context_window_value * 100)
        count_label = f"{used_for_bar:,} / {context_window_value:,} tokens ({utilization:.3f}%)"
        authority = "provider-reported input" if exact_input is not None else "Pi estimate; awaiting provider usage"
        context_bar = mo.Html(f'<div><div style="display:flex;justify-content:space-between;gap:12px;font:14px system-ui;margin-bottom:7px"><b>{count_label}</b><span>{authority}</span></div><div role="img" aria-label="Color-coded context window usage" style="height:22px;width:100%;display:flex;overflow:hidden;border-radius:6px;background:#e2e8f0;border:1px solid #cbd5e1">{"".join(bar_parts)}</div><div style="font:12px system-ui;color:#64748b;margin-top:7px">Window: {context_window_value:,} tokens · component sizes use Pi’s characters÷4 estimator; total is provider-reported after each response.</div><details style="margin-top:12px"><summary>Context sections and previews</summary><div style="font:13px system-ui;margin-top:8px">{"".join(legend_parts)}</div></details></div>')

    metrics_panel = mo.json({
        "request_id": request_id,
        "model": model_message.get("model") or context.get("model") or model_info.get("id"),
        "provider_input_tokens": exact_input,
        "provider_output_tokens": usage.get("output_tokens"),
        "reasoning_tokens": usage.get("reasoning_tokens"),
        "cache_read_tokens": usage.get("cache_read_tokens"),
        "cache_write_tokens": usage.get("cache_write_tokens"),
        "latency_ms": (model_message.get("timing") or {}).get("latency_ms"),
        "ttft_ms": (model_message.get("timing") or {}).get("ttft_ms"),
        "output_tokens_per_second": (model_message.get("timing") or {}).get("output_tokens_per_second"),
        "image_policy": model_message.get("image_policy") or context.get("image_policy"),
    })

    tool_events = [event for event in events if event.get("type") in {"tool_call_started", "tool_call_finished"}][-8:]
    tool_panel = mo.json(tool_events or {"state": "No tool calls recorded for this run yet."})

    download = None
    export_path = (artifact or {}).get("path") if isinstance(artifact, dict) else None
    if export_path and Path(export_path).is_file():
        download = mo.download(data=Path(export_path).read_bytes(), filename=Path(export_path).name, mimetype="application/zip", label="Download evidence ZIP")

    state_panel = mo.json({
        "service": status.get("service", "starting"),
        "host_tailnet_address": status.get("tailnet"),
        "active_run": active,
        "previous_run": status.get("previous_run"),
        "last_control_action": results[-1] if results else None,
        "refresh_sequence": refresh_counter,
    })
    error = status_error or startup_error
    if error:
        state_panel = mo.callout(mo.md(f"Run service error: `{html.escape(error)}`"), kind="danger")

    mo.vstack([
        mo.md("# Minecraft long-run harness\n\nA model-agnostic Pi control plane over a persistent Linux supervisor. The model gets a fresh first-person Prismarine Viewer frame as the final item of each new turn, native tools, a run-isolated workspace, and a persistent Mineflayer program runner."),
        auth_panel,
        controls,
        refresh,
        mo.md("## Live bot view · Prismarine Viewer"),
        viewer if viewer is not None else mo.md("Start a run to show the first-person viewer."),
        mo.md("### Latest exact image sent to the model"),
        frame_preview,
        mo.md("## Agent reasoning and response"),
        mo.md("Provider-exposed reasoning summary only; models may withhold private chain-of-thought."),
        reasoning_panel,
        mo.md("### Final answer"),
        answer_panel,
        mo.md("## Context window at the selected request"),
        request_picker if request_picker is not None else mo.md("No model request has been recorded yet."),
        context_bar,
        mo.md("### Request telemetry"),
        metrics_panel,
        mo.md("### Recent tool activity"),
        tool_panel,
        mo.md("## Run state and trace"),
        state_panel,
        download if download is not None else mo.md("Run exports appear here after selecting Export."),
    ])
    return


if __name__ == "__main__":
    app.run()
