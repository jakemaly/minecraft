#!/usr/bin/env python3
"""Limit only the notebook, viewer, and Minecraft ports to the operator Mac."""

from __future__ import annotations

import argparse
import sys

from minecraft_agent.containers import configure_tailnet_firewall
from minecraft_agent.network import client_tailscale_address, tailscale_address


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="apply narrowly scoped Tailscale-interface rules using the Docker host firewall helper")
    args = parser.parse_args()
    host = tailscale_address()
    client = client_tailscale_address()
    if host == "127.0.0.1" or not client:
        print("Could not identify both Linux host and Mac Tailscale IPv4 addresses; set MINECRAFT_TAILSCALE_IP and MINECRAFT_CLIENT_TAILSCALE_IP.", file=sys.stderr)
        return 2
    if not args.apply:
        print(f"Would allow {client} to {host} on TCP 2718, 3000, and 25565 over tailscale0, and drop other tailnet sources for those ports.")
        print("Other host firewall policies and ports will not be changed.")
        return 0
    try:
        print(configure_tailnet_firewall(host, client))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Could not configure the service-port firewall: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
