"""Prose mentions: the second gatherer of the Evidence Dossier.

Tables are covered by the facts graph; numbers stated in narrative body
text (commentary letters, quarterly updates) were invisible to it -- the
measured cause of the universal prose_table_clash miss (PROTOCOL.md,
exp-20260709T185721Z). This module extracts numeric *mentions* from the
already-indexed page/sheet spans:

- **Deterministic candidates**: code finds number-bearing sentences in the
  page text, normalizes the value (EUR millions, percent, counts), and
  extracts the verbatim sentence as the quote. The quote is code-extracted
  from the indexed span -- never model output.
- **Lightweight labeling**: one bounded sub-model call per document
  (PROOFBENCH_SUB_MODEL, default gpt-5-nano) names each candidate's
  metric_phrase and period and drops non-metric numbers (dates, page
  numbers). Pointer-only trust, like entity embeddings: a label can only
  help *find* the sentence, the
  sentence itself is what gets cited, and every row carries extracted_by.
- **Same-doc dedupe**: a candidate whose value already exists as a table
  fact of the same document is the table restated, not new evidence, and
  is skipped. Cross-document repeats are kept on purpose -- they are
  exactly the occurrences the dossier needs.

Build with `proofbench mentions <audit-id>` after `proofbench index`
(the index is recreated from scratch, like `embed`).
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3

from proofbench.graph import _close, parse_number
from proofbench.index_db import db_path
from proofbench.jsonutil import extract_json
from proofbench.llm import run_agent

_NUMBER_RE = re.compile(
    r"(?:EUR|€)\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:million|thousand|bn|billion|k))?"
    r"|\d[\d,]*(?:\.\d+)?\s?%"
    r"|\d[\d,]*(?:\.\d+)?",
)

LABEL_SYSTEM_PROMPT = """\
You label numeric mentions found in one business document's narrative text.

You get a JSON array of candidates: {"i": <index>, "sentence": "...",
"numbers": ["..."]}. For each candidate, decide for EACH number string
whether it states a business metric's value, and if so what metric.

Respond with ONLY a JSON array (no prose, no fences), one object per kept
number:
{"i": <candidate index>, "number": "<the exact number string>",
 "metric_phrase": "<short name of the metric as the sentence phrases it,
   e.g. 'net promoter score', 'qualified pipeline'>",
 "period": "<normalized period like '2026-Q2' if the sentence or document
   title implies one, else null>",
 "unit": "<currency|percent|count|days|hours|score|other>"}

Drop numbers that are dates, years on their own, page numbers, section
numbers, or parts of names. Never invent numbers not in the list, never
alter the number string.
"""


def normalize_mention_value(number_text: str) -> float | None:
    """Deterministic value normalization: 'EUR 13.26 million' -> 13260000,
    '93.5%' -> 0.935, '9,450' -> 9450."""
    t = number_text.strip()
    scale = 1.0
    lowered = t.casefold()
    if "million" in lowered or lowered.endswith("bn") or "billion" in lowered:
        scale = 1e9 if ("billion" in lowered or lowered.endswith("bn")) else 1e6
    elif "thousand" in lowered or lowered.rstrip().endswith("k"):
        scale = 1e3
    percent = t.rstrip().endswith("%")
    cleaned = re.sub(r"(?i)(eur|€|million|billion|thousand|bn|k\b|%)", "", t).strip()
    base = parse_number(cleaned)
    if base is None:
        return None
    if percent:
        return base / 100
    return base * scale


def _sentences(page_text: str) -> list[str]:
    flat = " ".join(page_text.split())
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", flat) if s.strip()]


def _candidate_sentences(conn: sqlite3.Connection, kind: str) -> dict[str, list[dict]]:
    """doc_id -> [{location, sentence, numbers}] for prose-looking sentences.
    A sentence qualifies when it has enough words to be narrative (table
    dumps in page text are short label/number lines) and contains a number."""
    fact_values: dict[str, list[float]] = {}
    for doc_id, value in conn.execute(
        "SELECT facts.doc_id, value FROM facts "
        "JOIN documents ON documents.doc_id = facts.doc_id WHERE documents.kind = ?",
        (kind,),
    ):
        parsed = parse_number(value)
        if parsed is not None:
            fact_values.setdefault(doc_id, []).append(parsed)

    by_doc: dict[str, list[dict]] = {}
    rows = conn.execute(
        "SELECT spans_fts.doc_id, location, text FROM spans_fts "
        "JOIN documents ON documents.doc_id = spans_fts.doc_id "
        "WHERE documents.kind = ? AND location NOT LIKE '%/row:%' AND location NOT LIKE '%!row%'",
        (kind,),
    ).fetchall()
    for doc_id, location, text in rows:
        for sentence in _sentences(text):
            if len(sentence.split()) < 6:
                continue
            numbers = _NUMBER_RE.findall(sentence)
            kept = []
            for num in numbers:
                value = normalize_mention_value(num)
                if value is None:
                    continue
                # same-doc dedupe: the table already states this value (in
                # any of its native scales), so the sentence is a restatement
                doc_facts = fact_values.get(doc_id, [])
                if any(
                    _close(value, fv) or _close(value, fv * 1e3) or _close(value, fv * 1e6)
                    for fv in doc_facts
                ):
                    continue
                kept.append(num)
            if kept:
                by_doc.setdefault(doc_id, []).append(
                    {"location": location, "sentence": sentence, "numbers": kept}
                )
    return by_doc


async def _label_document(doc_id: str, candidates: list[dict], model: str) -> tuple[list[dict], float]:
    payload = [
        {"i": i, "sentence": c["sentence"], "numbers": c["numbers"]}
        for i, c in enumerate(candidates)
    ]
    # One flaky sub-model reply (empty text, malformed JSON) must not kill
    # the whole extraction -- retry once, then skip the document.
    mentions: list[dict] = []
    cost = 0.0
    labels = None
    for _attempt in range(2):
        reply = await run_agent(
            LABEL_SYSTEM_PROMPT,
            f"DOCUMENT: {doc_id}\nCANDIDATES:\n{json.dumps(payload)}",
            model=model,
        )
        cost += reply.cost_usd or 0.0
        try:
            labels = extract_json(reply.text)
            break
        except ValueError:
            continue
    if not isinstance(labels, list):
        print(f"mentions: skipping {doc_id} -- unusable labeling reply after retry")
        return mentions, cost
    for label in labels:
        try:
            cand = candidates[int(label["i"])]
            number = str(label["number"])
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        if number not in cand["numbers"]:
            continue  # the model may not invent or alter numbers
        value = normalize_mention_value(number)
        if value is None:
            continue
        mentions.append(
            {
                "doc_id": doc_id,
                "location": cand["location"],
                "value": value,
                "unit": str(label.get("unit") or "other"),
                "metric_phrase": " ".join(str(label.get("metric_phrase") or "").split()),
                "period": label.get("period"),
                "quote": cand["sentence"],
            }
        )
    return mentions, cost


async def extract_mentions_async(
    audit_id: str, *, kind: str = "vault", model: str | None = None, max_concurrency: int = 4
) -> tuple[int, float]:
    """Extract + label prose mentions into prose_mentions; embed metric
    phrases into mention_embeddings. Returns (mention_count, cost_usd)."""
    import os

    from proofbench.embeddings import _pack, embed_texts, resolve_embedding_model

    resolved_model = model or os.environ.get("PROOFBENCH_SUB_MODEL", "openai/gpt-5-nano")
    conn = sqlite3.connect(db_path(audit_id))
    try:
        by_doc = _candidate_sentences(conn, kind)
        semaphore = asyncio.Semaphore(max_concurrency)

        async def bounded(doc_id: str, cands: list[dict]) -> tuple[list[dict], float]:
            async with semaphore:
                return await _label_document(doc_id, cands, resolved_model)

        results = await asyncio.gather(*(bounded(d, c) for d, c in sorted(by_doc.items())))
        mentions = [m for doc_mentions, _ in results for m in doc_mentions]
        cost = sum(doc_cost for _, doc_cost in results)

        conn.execute(
            "CREATE TABLE IF NOT EXISTS prose_mentions ("
            "mention_id INTEGER PRIMARY KEY, doc_id TEXT NOT NULL, location TEXT NOT NULL, "
            "value REAL NOT NULL, unit TEXT, metric_phrase TEXT NOT NULL, period TEXT, "
            "quote TEXT NOT NULL, extracted_by TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS mention_embeddings ("
            "mention_id INTEGER PRIMARY KEY, model TEXT NOT NULL, vector BLOB NOT NULL)"
        )
        conn.execute("DELETE FROM prose_mentions")
        conn.execute("DELETE FROM mention_embeddings")
        ids = []
        for m in mentions:
            cur = conn.execute(
                "INSERT INTO prose_mentions (doc_id, location, value, unit, metric_phrase, "
                "period, quote, extracted_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (m["doc_id"], m["location"], m["value"], m["unit"], m["metric_phrase"],
                 m["period"], m["quote"], resolved_model),
            )
            ids.append(cur.lastrowid)
        if mentions:
            embedding_model = resolve_embedding_model()
            vectors = embed_texts([m["metric_phrase"] for m in mentions], embedding_model)
            conn.executemany(
                "INSERT INTO mention_embeddings (mention_id, model, vector) VALUES (?, ?, ?)",
                [(mid, embedding_model, _pack(v)) for mid, v in zip(ids, vectors)],
            )
        conn.commit()
        return len(mentions), cost
    finally:
        conn.close()


def mention_count(audit_id: str) -> int:
    conn = sqlite3.connect(db_path(audit_id))
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'prose_mentions'"
        ).fetchone()
        if row is None:
            return 0
        return conn.execute("SELECT COUNT(*) FROM prose_mentions").fetchone()[0]
    finally:
        conn.close()


def matching_mentions(
    conn: sqlite3.Connection, query_text: str, k: int = 12, min_similarity: float = 0.35
) -> list[dict]:
    """Prose mentions whose metric_phrase embedding is close to query_text
    (a claim label or canonical entity name). Falls back to substring match
    when embeddings are unavailable."""
    from proofbench.embeddings import _cosine, _unpack, embed_texts

    rows = conn.execute(
        "SELECT pm.mention_id, pm.doc_id, pm.location, pm.value, pm.unit, pm.metric_phrase, "
        "pm.period, pm.quote, pm.extracted_by, me.model, me.vector "
        "FROM prose_mentions pm LEFT JOIN mention_embeddings me ON me.mention_id = pm.mention_id"
    ).fetchall()
    if not rows:
        return []

    def as_dict(r, similarity=None):
        d = {"doc_id": r[1], "location": r[2], "value": r[3], "unit": r[4],
             "metric_phrase": r[5], "period": r[6], "quote": r[7], "extracted_by": r[8]}
        if similarity is not None:
            d["similarity"] = round(similarity, 3)
        return d

    embedded = [r for r in rows if r[10] is not None]
    if embedded:
        model = embedded[0][9]
        try:
            query_vector = embed_texts([query_text], model)[0]
        except Exception:
            query_vector = None
        if query_vector is not None:
            scored = sorted(
                ((r, _cosine(query_vector, _unpack(r[10]))) for r in embedded),
                key=lambda pair: pair[1], reverse=True,
            )
            return [as_dict(r, s) for r, s in scored[:k] if s >= min_similarity]

    lowered = query_text.casefold()
    return [as_dict(r) for r in rows
            if r[5].casefold() in lowered or lowered in r[5].casefold()][:k]
