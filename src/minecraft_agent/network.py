"""Resolve and validate the tailnet addresses used by the private game services."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
from typing import Any


def _is_tailscale_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if address.version == 4:
        return address in ipaddress.ip_network("100.64.0.0/10")
    return address in ipaddress.ip_network("fd7a:115c:a1e0::/48")


def tailscale_address() -> str:
    configured = os.getenv("MINECRAFT_TAILSCALE_IP", "").strip()
    if configured:
        if not _is_tailscale_address(configured):
            raise ValueError("MINECRAFT_TAILSCALE_IP must be an address in the Tailscale CGNAT or ULA range")
        return configured
    try:
        output = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=4, check=True).stdout.strip()
        address = ipaddress.ip_address(output)
        if address.version == 4 and _is_tailscale_address(output):
            return output
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return "127.0.0.1"


def client_tailscale_address() -> str | None:
    configured = os.getenv("MINECRAFT_CLIENT_TAILSCALE_IP", "").strip()
    if configured:
        if not _is_tailscale_address(configured):
            raise ValueError("MINECRAFT_CLIENT_TAILSCALE_IP must be a Tailscale address")
        return configured
    client_name = os.getenv("MINECRAFT_CLIENT_NAME", "Jake’s MacBook Air").casefold()
    try:
        output = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5, check=True).stdout
        peers: dict[str, Any] = json.loads(output).get("Peer", {})
        for peer in peers.values():
            hostname = str(peer.get("HostName", "")).casefold()
            if hostname == client_name or ("jake" in client_name and "jake" in hostname and "mac" in hostname):
                addresses = peer.get("TailscaleIPs", [])
                ipv4 = next((value for value in addresses if ipaddress.ip_address(value).version == 4 and _is_tailscale_address(value)), None)
                if ipv4:
                    return ipv4
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        pass
    return None


def viewer_url(host: str, port: int) -> str | None:
    if host in {"127.0.0.1", "::1"}:
        return None
    return f"http://{host}:{port}/"
