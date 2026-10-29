#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAIL_IP="${MINECRAFT_TAILSCALE_IP:-$(tailscale ip -4)}"
PORT="${MINECRAFT_DASHBOARD_PORT:-2718}"

if [[ -z "$TAIL_IP" || "$TAIL_IP" == "127.0.0.1" ]]; then
  echo "Tailscale IPv4 address is unavailable; refusing to expose the notebook on a public interface." >&2
  exit 2
fi

export MINECRAFT_TAILSCALE_IP="$TAIL_IP"
cd "$ROOT"
uv run --extra notebook python scripts/configure_tailnet_firewall.py --apply
exec uv run --extra notebook marimo run notebooks/01_agent_runtime.py --host "$TAIL_IP" --port "$PORT"
