"""Facts catalog + entity alias enrichment: the two retrieval-context levers
the eval harness (eval.py) measures.

The measured cost problem (see ARCHITECTURE.md) was vocabulary mismatch,
not over-eager searching: a claim says "quarter-end cash", the vault fact's
entity is "Cash and cash equivalents", and the Verifier burns 2-3 blind
search_facts guesses bridging the gap. Two fixes, cheapest first:

- **Facts catalog** (`catalog_prompt_section`): a compact listing of every
  distinct entity/attribute name in the facts index, injected into the
  Verifier's system prompt so it knows the vault's vocabulary on turn one.
  Names only, not values -- grounding still requires an actual tool call,
  so evidence provenance is unchanged. Built with one SQL query, zero LLM
  cost.
- **Alias enrichment** (`enrich_aliases_async`): one LLM call per index
  build that reads the entity names and emits plausible alternative
  phrasings into the `fact_aliases` table, which search_facts can then
  match against (tools.py, `use_aliases=True`). Runs once per audit, so
  its cost amortizes over every claim in every later verification run.
  Low-risk by construction: an alias can only make search *find* a
  deterministically-extracted fact more easily -- it can never alter the
  fact's value or provenance.
"""

from __future__ import annotations

import sqlite3

from proofbench.jsonutil import extract_json
from proofbench.llm import AgentReply, run_agent
from proofbench.index_db import db_path

ALIAS_SYSTEM_PROMPT = """\
You generate search aliases for a financial-audit fact index.

You are given a JSON array of entity names -- row labels from tables in
financial/operational documents (e.g. "Cash and cash equivalents",
"Net revenue retention"). For each entity, list 2-4 alternative phrasings
an auditor or a narrative report might use for the same concept (e.g.
"Cash and cash equivalents" -> "quarter-end cash", "cash position",
"cash balance"). Aliases must refer to the SAME concept -- never a
broader, narrower, or merely related one.

Respond with ONLY a JSON object mapping each entity name to its array of
alias strings. No prose, no markdown fences.
"""


def distinct_entities(audit_id: str, kind: str = "vault") -> list[str]:
    conn = sqlite3.connect(db_path(audit_id))
    try:
        rows = conn.execute(
            "SELECT DISTINCT entity FROM facts "
            "JOIN documents ON documents.doc_id = facts.doc_id "
            "WHERE documents.kind = ? ORDER BY facts.id",
            (kind,),
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def facts_catalog(audit_id: str, kind: str = "vault") -> str:
    """Distinct entity/attribute names per document, one compact block --
    measured at a few hundred tokens for the Northstar fixture (vs ~1,254
    for the full facts-with-values digest)."""
    conn = sqlite3.connect(db_path(audit_id))
    try:
        rows = conn.execute(
            "SELECT DISTINCT facts.doc_id, entity, attribute FROM facts "
            "JOIN documents ON documents.doc_id = facts.doc_id "
            "WHERE documents.kind = ? ORDER BY facts.id",
            (kind,),
        ).fetchall()
    finally:
        conn.close()

    by_doc: dict[str, dict[str, list[str]]] = {}
    for doc_id, entity, attribute in rows:
        attrs = by_doc.setdefault(doc_id, {}).setdefault(entity, [])
        if attribute not in attrs:
            attrs.append(attribute)

    lines: list[str] = []
    for doc_id, entities in by_doc.items():
        lines.append(f"{doc_id}:")
        for entity, attrs in entities.items():
            lines.append(f'  "{entity}" [{" | ".join(attrs)}]')
    return "\n".join(lines)


def catalog_prompt_section(audit_id: str, kind: str = "vault") -> str:
    catalog = facts_catalog(audit_id, kind)
    if not catalog:
        return ""
    return (
        "\n\nFACTS CATALOG -- every entity (row label) and its attributes "
        "(column headers) that exist in the structured facts index, per "
        "document. The claim's wording may differ from these names: map the "
        "claim to the closest catalog entity and use that EXACT name with "
        "search_facts, instead of guessing names that are not listed here.\n"
        f"{catalog}\n"
    )


def alias_count(audit_id: str) -> int:
    conn = sqlite3.connect(db_path(audit_id))
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'fact_aliases'"
        ).fetchone()
        if row is None:
            return 0
        return conn.execute("SELECT COUNT(*) FROM fact_aliases").fetchone()[0]
    finally:
        conn.close()


async def enrich_aliases_async(
    audit_id: str, *, model: str | None = None, kind: str = "vault"
) -> tuple[int, AgentReply]:
    """One plain LLM call (no tools): entity names in, alias mapping out,
    written to fact_aliases (replacing any previous enrichment). Returns
    (number of aliases written, the AgentReply for cost accounting)."""
    entities = distinct_entities(audit_id, kind)
    if not entities:
        raise ValueError(f"no {kind} facts indexed for {audit_id} -- run `proofbench index` first")

    import json

    reply = await run_agent(ALIAS_SYSTEM_PROMPT, json.dumps(entities), model=model)
    mapping = extract_json(reply.text)
    if not isinstance(mapping, dict):
        raise ValueError(f"expected a JSON object of entity->aliases, got {type(mapping).__name__}")

    known = set(entities)
    rows = [
        (entity, str(alias).strip())
        for entity, aliases in mapping.items()
        if entity in known and isinstance(aliases, list)
        for alias in aliases
        if str(alias).strip()
    ]

    conn = sqlite3.connect(db_path(audit_id))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS fact_aliases "
            "(entity TEXT NOT NULL, alias TEXT NOT NULL, PRIMARY KEY (entity, alias))"
        )
        conn.execute("DELETE FROM fact_aliases")
        conn.executemany("INSERT OR IGNORE INTO fact_aliases (entity, alias) VALUES (?, ?)", rows)
        conn.commit()
    finally:
        conn.close()
    return len(rows), reply
