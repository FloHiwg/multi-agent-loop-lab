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

    # Fall back to the outermost [...] or {...} span in the raw text.
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        start = candidate.find(open_ch)
        end = candidate.rfind(close_ch)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                continue

    raise ValueError(f"could not extract JSON from LLM reply: {text[:200]!r}")
