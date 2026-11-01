"""Existing-request negotiation and bounded native acknowledgement reads."""

from __future__ import annotations

import json
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

MAX_RESPONSE_BYTES = 256 * 1024


def evidence_url(url: str) -> str:
    parsed = urlsplit(url)
    query = [
        (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "debugbundle_evidence"
    ]
    query.append(("debugbundle_evidence", "1"))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def bounded_response_json(response: httpx.Response, deadline: float) -> object | None:
    try:
        length = response.headers.get("content-length")
        if length is not None and length.isdigit() and int(length) > MAX_RESPONSE_BYTES:
            return None
        # Native ACK requests ask for identity. Do not allocate an unbounded
        # decompressed body if an intermediary ignores that request.
        if response.headers.get("content-encoding", "identity").lower() not in ("identity", ""):
            return None
        if response.is_stream_consumed:
            data = response.content
            if len(data) > MAX_RESPONSE_BYTES or time.monotonic() > deadline:
                return None
        else:
            buffer = bytearray()
            for chunk in response.iter_raw():
                if len(chunk) > MAX_RESPONSE_BYTES - len(buffer) or time.monotonic() > deadline:
                    return None
                buffer.extend(chunk)
            data = buffer
        return json.loads(data) if time.monotonic() <= deadline else None
    except (ValueError, TypeError, RecursionError, OverflowError):
        return None
