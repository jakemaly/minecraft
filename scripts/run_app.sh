#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAIL_IP="${MINECRAFT_TAILSCALE_IP:-}"
if [[ -z "$TAIL_IP" ]] && command -v tailscale >/dev/null 2>&1; then
  TAIL_IP="$(tailscale ip -4 2>/dev/null || true)"
fi

if [[ -z "$TAIL_IP" || "$TAIL_IP" == "127.0.0.1" ]]; then
  echo "Tailscale IPv4 address is unavailable; refusing to expose the Minecraft app publicly." >&2
  exit 2
fi

export MINECRAFT_TAILSCALE_IP="$TAIL_IP"
export MINECRAFT_DASHBOARD_HOST="$TAIL_IP"
export MINECRAFT_AGENT_REPO_ROOT="$ROOT"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
cd "$ROOT"
python3 scripts/configure_tailnet_firewall.py --apply
exec python3 -m minecraft_agent.daemon
