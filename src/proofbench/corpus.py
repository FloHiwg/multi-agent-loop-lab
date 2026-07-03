"""Render index/parsed/<doc_id>.json into plain text agents can read, with
enough location markers (page / sheet+cell) to trace a claim or an evidence
span back to where it came from.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INDEX_PARSED_DIR = REPO_ROOT / "index" / "parsed"


def load_parsed(doc_id: str) -> dict:
    path = INDEX_PARSED_DIR / f"{doc_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found -- run `proofbench init <audit-id>` first")
    return json.loads(path.read_text())


def render_as_text(doc_id: str, parsed: dict) -> str:
    fmt = parsed.get("format")
    if fmt == "pdf":
        lines = [f"=== DOCUMENT {doc_id} (pdf) ==="]
        for page in parsed["pages"]:
            lines.append(f"--- page {page['page']} ---")
            lines.append(page["text"])
        return "\n".join(lines)
    if fmt == "xlsx":
        lines = [f"=== DOCUMENT {doc_id} (xlsx) ==="]
        for sheet in parsed["sheets"]:
            lines.append(f"--- sheet {sheet['sheet']!r} ---")
            for cell in sheet["cells"]:
                lines.append(f"{cell['ref']}: {cell['value']}")
        return "\n".join(lines)
    raise ValueError(f"unsupported parsed format: {fmt}")


def render_document(doc_id: str) -> str:
    return render_as_text(doc_id, load_parsed(doc_id))
