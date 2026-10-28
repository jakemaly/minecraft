"""Strict, bounded newline-delimited JSON envelopes shared by Python and Node."""

from __future__ import annotations

import json
import uuid
from typing import Any, BinaryIO

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 1_000_000


class ProtocolError(ValueError):
    pass


def encode_request(run_id: str, method: str, params: dict[str, Any], request_id: str | None = None) -> bytes:
    if not method or not isinstance(params, dict):
        raise ProtocolError("method and object params are required")
    envelope = {"version": PROTOCOL_VERSION, "run_id": run_id, "request_id": request_id or str(uuid.uuid4()), "method": method, "params": params}
    payload = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ProtocolError("request exceeds maximum message size")
    return payload + b"\n"


def decode_line(line: bytes, *, max_bytes: int = MAX_MESSAGE_BYTES) -> dict[str, Any]:
    if len(line) > max_bytes:
        raise ProtocolError("message exceeds maximum size")
    try:
        message = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid UTF-8 JSON message") from exc
    if not isinstance(message, dict) or message.get("version") != PROTOCOL_VERSION:
        raise ProtocolError("unsupported protocol envelope")
    return message


def read_line(stream: BinaryIO, *, max_bytes: int = MAX_MESSAGE_BYTES) -> dict[str, Any]:
    line = stream.readline(max_bytes + 1)
    if not line:
        raise EOFError("peer closed protocol stream")
    if not line.endswith(b"\n"):
        raise ProtocolError("message is not newline terminated or exceeds limit")
    return decode_line(line[:-1], max_bytes=max_bytes)
