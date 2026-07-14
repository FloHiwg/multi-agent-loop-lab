"""Evidence Dossier: gather every occurrence of a claim's fact, then let
the Verifier judge.

The brain/hands split (PROTOCOL.md, decided after exp-20260709T185721Z):
gathering is not the strong model's job. Three gatherers feed one dossier:

1. **graph** -- table facts via deterministic entity resolution
   (entity_profile for the resolved entity AND its see_also neighbors).
2. **prose** -- prose_mentions matched by embedding similarity of the
   mention's metric_phrase to the claim label (mentions.py).
3. **researcher** -- one bounded cheap-model sweep (rlm.py) for anything
   the first two missed: vocabulary drift, phrasings no index caught.

The dossier carries provenance for every occurrence (doc_id, location,
verbatim quote where available) plus adjudication metadata the corpus
already knows but verification never used: the document's evidence tag and
its authority_rank from audit.yaml's evidence_priority. A cross-source
conflict summary (same period, materially different values) is computed
deterministically so disagreements cannot be overlooked -- including the
prose-vs-table clashes the per-entity table conflict detector can't see.

The Verifier receives the dossier in its first message and judges:
which occurrences are relevant, what the documents establish about
authority, and the verdict. It keeps read-only tools to confirm quotes.
"""

from __future__ import annotations

import json
import sqlite3

from proofbench.embeddings import nearest_entities
from proofbench.graph import _close, entity_profile_data, parse_number
from proofbench.index_db import db_path
from proofbench.ingest import load_audit_config
from proofbench.jsonutil import extract_json
from proofbench.mentions import matching_mentions
from proofbench.models import Claim


def _doc_metadata(audit_id: str) -> dict[str, dict]:
    config = load_audit_config(audit_id)
    priority = {tag: rank for rank, tag in enumerate(config.evidence_priority, start=1)}
    return {
        ref.doc_id: {
            "doc_tag": ref.tag,
            "authority_rank": priority.get(ref.tag) if ref.tag else None,
        }
        for ref in config.documents
    }


def _table_occurrences(conn: sqlite3.Connection, kind: str, claim: Claim, audit_id: str) -> list[dict]:
    """Resolve the claim's wording to entities and pull every table fact for
    them and their near-name neighbors."""
    seen_entities: list[str] = []
    occurrences: list[dict] = []

    candidates = nearest_entities(audit_id, kind, claim.label, k=3) or []
    names = [c["entity"] for c in candidates]
    frontier = names[:2] if names else [claim.label]
    while frontier:
        name = frontier.pop(0)
        if name in seen_entities:
            continue
        seen_entities.append(name)
        profile = entity_profile_data(conn, kind, name)
        if profile is None:
            continue
        if profile["entity"] in seen_entities and profile["entity"] != name:
            continue
        seen_entities.append(profile["entity"])
        for fact in profile["facts"]:
            occurrences.append(
                {
                    "source": "table",
                    "doc_id": fact["doc_id"],
                    "location": fact["location"],
                    "entity": profile["entity"],
                    "attribute": fact["attribute"],
                    "period": fact["period"],
                    "role": fact["role"],
                    "value": fact["value"],
                    "quote": fact["span_text"],
                }
            )
        for neighbor in profile.get("see_also", []):
            if neighbor["entity"] not in seen_entities:
                frontier.append(neighbor["entity"])
    return occurrences


def _dedupe(occurrences: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    unique = []
    for occ in occurrences:
        key = (occ["doc_id"], occ["location"], str(occ["value"]))
        if key in seen:
            continue
        seen.add(key)
        unique.append(occ)
    return unique


def _conflict_summary(occurrences: list[dict]) -> list[dict]:
    """Cross-source: same period, materially different values, any mix of
    table/prose/researcher occurrences. Non-numeric or period-less
    occurrences are left for the judge."""
    by_period: dict[str, list[tuple[float, dict]]] = {}
    for occ in occurrences:
        period = occ.get("period")
        value = parse_number(str(occ["value"]))
        if period is None or value is None:
            continue
        # roles other than actuals (budget, plan) aren't competing statements
        if occ.get("role") not in (None, "actual"):
            continue
        by_period.setdefault(period, []).append((value, occ))
    conflicts = []
    for period, values in sorted(by_period.items()):
        distinct: list[float] = []
        for v, _ in values:
            # prose states EUR while tables state EUR-millions: compare
            # across the plausible scales before calling it a disagreement
            if not any(_close(v, d) or _close(v, d * 1e6) or _close(v * 1e6, d) or _close(v, d * 1e3) or _close(v * 1e3, d) for d in distinct):
                distinct.append(v)
        if len(distinct) > 1:
            conflicts.append(
                {
                    "period": period,
                    "values": [
                        {"doc_id": occ["doc_id"], "source": occ["source"], "value": v}
                        for v, occ in values
                    ],
                }
            )
    return conflicts


async def build_dossier(
    claim: Claim,
    audit_id: str,
    *,
    kind: str = "vault",
    use_researcher: bool = True,
    sub_costs: list[float] | None = None,
) -> dict:
    """All known occurrences of the claim's fact, with provenance and
    adjudication metadata. Gathering only -- no verdicts here."""
    conn = sqlite3.connect(db_path(audit_id))
    try:
        occurrences = _table_occurrences(conn, kind, claim, audit_id)
        for mention in matching_mentions(conn, claim.label):
            occurrences.append(
                {
                    "source": "prose",
                    "doc_id": mention["doc_id"],
                    "location": mention["location"],
                    "metric_phrase": mention["metric_phrase"],
                    "period": mention["period"],
                    "role": None,
                    "value": mention["value"],
                    "quote": mention["quote"],
                    "extracted_by": mention["extracted_by"],
                    "similarity": mention.get("similarity"),
                }
            )
    finally:
        conn.close()

    if use_researcher:
        occurrences += await _researcher_gap_check(claim, audit_id, kind, occurrences, sub_costs)

    occurrences = _dedupe(occurrences)
    metadata = _doc_metadata(audit_id)
    for occ in occurrences:
        occ.update(metadata.get(occ["doc_id"], {"doc_tag": None, "authority_rank": None}))

    return {
        "claim_id": claim.claim_id,
        "occurrences": occurrences,
        "cross_source_conflicts": _conflict_summary(occurrences),
        "note": (
            "Occurrences were gathered by three mechanisms: deterministic table "
            "facts (source=table, quotes verbatim), LLM-labeled prose mentions "
            "(source=prose, quote is the code-extracted verbatim sentence; the "
            "metric_phrase label is model output), and a research sub-agent "
            "sweep (source=researcher; confirm its quotes before citing). "
            "authority_rank: 1 = highest configured evidence priority."
        ),
    }


async def _researcher_gap_check(
    claim: Claim, audit_id: str, kind: str, known: list[dict], sub_costs: list[float] | None
) -> list[dict]:
    """One bounded cheap-model sweep for occurrences the indexes missed."""
    from proofbench.rlm import ask_researcher_tool

    known_docs = sorted({occ["doc_id"] for occ in known})
    tool = ask_researcher_tool(audit_id, kind, sub_costs=sub_costs)
    question = (
        f"Find every place any vault document states a value for this metric: "
        f"{claim.label!r} (period: {claim.time_scope}). Search several phrasings and "
        f"synonyms. Occurrences in these documents are already known -- only report "
        f"spans from OTHER documents, or clearly different phrasings within them: "
        f"{json.dumps(known_docs)}. If there is nothing new, say so."
    )
    result = await tool.handler({"question": question})
    try:
        report = extract_json(result["content"][0]["text"])
    except (ValueError, KeyError, IndexError):
        return []
    if not isinstance(report, dict) or report.get("warning") or report.get("error"):
        return []
    occurrences = []
    for span in report.get("spans", []) or []:
        if not isinstance(span, dict) or not span.get("doc_id"):
            continue
        occurrences.append(
            {
                "source": "researcher",
                "doc_id": span.get("doc_id"),
                "location": span.get("location"),
                "metric_phrase": None,
                "period": None,
                "role": None,
                "value": span.get("span_text", ""),
                "quote": span.get("span_text"),
            }
        )
    return occurrences
