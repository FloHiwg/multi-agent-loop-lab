"""Extract a JSON value from an LLM text reply that may be wrapped in prose or fences."""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    fence_match = _FENCE_RE.search(text)
    candidate = fence_match.group(1) if fence_match else text
    candidate = candidate.strip()

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # Fall back: scan for every balanced JSON value in the text and return
    # the largest one. The old find("[")..rfind("]") heuristic tried arrays
    # before objects and sliced across the whole text -- when a reply was
    # "prose... {\"evidence\": [...], \"verdict\": {...}}", it carved the
    # evidence array out of the middle of a perfectly valid object, which
    # is what most recorded Verifier "shape failures" actually were.
    decoder = json.JSONDecoder()
    best: Any = None
    best_len = 0
    i = 0
    while i < len(candidate):
        if candidate[i] not in "{[":
            i += 1
            continue
        try:
            value, end = decoder.raw_decode(candidate, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if end - i > best_len:
            best, best_len = value, end - i
        i = end
    if best is not None:
        return best

    raise ValueError(f"could not extract JSON from LLM reply: {text[:200]!r}")
