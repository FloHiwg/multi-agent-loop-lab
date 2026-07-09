"""Entity-name embeddings: semantic fallback for entity_profile's name
resolution.

The graph eval exposed the gap (ARCHITECTURE.md): _resolve_entity matches
exact-normalized then substring, so every adversarial Meridian rename --
"turnover" for "Net revenue", "closing workforce" for "FTE, period end" --
missed, and the Verifier fell back to the blind search dance the graph
tools were built to replace. Lexical scoring can't bridge true synonyms
(or other languages), so misses are ranked semantically instead:

- **Build once per index** (`proofbench embed`): every canonical entity
  name is embedded through OpenRouter's embeddings endpoint and stored in
  the `entity_embeddings` table. Same trust posture as fact_aliases --
  a vector can only help *find* a deterministically-extracted fact, never
  alter its value or provenance.
- **Query only on miss**: when entity_profile fails to resolve a name, the
  incoming name is embedded (with the SAME model the table was built with,
  read back from the table, so an env change can't silently mix spaces)
  and the top-k nearest entities are returned as *suggestions*. Never
  auto-resolved: silently binding to the nearest neighbor would rebuild
  the wrong-entity failure mode this product exists to catch -- the agent
  confirms by calling entity_profile again with the exact name.

Embeddings for the same input and model are deterministic, but the table
is still a rebuildable derived artifact like everything else in
index/search/ -- swapping models just means rerunning `proofbench embed`.
"""

from __future__ import annotations

import json
import os
import sqlite3
import struct
import urllib.request

from proofbench.index_db import db_path

DEFAULT_EMBEDDING_MODEL = "openai/text-embedding-3-small"
EMBEDDINGS_URL = "https://openrouter.ai/api/v1/embeddings"

# Query-side embeddings repeat across claims (variants re-verify the same
# vocabulary), so memoize per (model, text) for the process lifetime.
_query_cache: dict[tuple[str, str], list[float]] = {}


def resolve_embedding_model() -> str:
    return os.environ.get("PROOFBENCH_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)


def embed_texts(texts: list[str], model: str) -> list[list[float]]:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not set (entity embeddings go through OpenRouter)")
    request = urllib.request.Request(
        EMBEDDINGS_URL,
        data=json.dumps({"model": model, "input": texts}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)
    items = sorted(payload["data"], key=lambda item: item["index"])
    if len(items) != len(texts):
        raise RuntimeError(f"embeddings response has {len(items)} vectors for {len(texts)} inputs")
    return [item["embedding"] for item in items]


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def embedding_count(audit_id: str) -> int:
    conn = sqlite3.connect(db_path(audit_id))
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'entity_embeddings'"
        ).fetchone()
        if row is None:
            return 0
        return conn.execute("SELECT COUNT(*) FROM entity_embeddings").fetchone()[0]
    finally:
        conn.close()


def embed_entities(audit_id: str, *, model: str | None = None) -> int:
    """Embed every canonical entity name into entity_embeddings (replacing
    any previous build). One embeddings call for the whole table."""
    resolved_model = model or resolve_embedding_model()
    conn = sqlite3.connect(db_path(audit_id))
    try:
        entities = conn.execute("SELECT entity_id, name FROM entities ORDER BY entity_id").fetchall()
        if not entities:
            return 0
        vectors = embed_texts([name for _, name in entities], resolved_model)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS entity_embeddings ("
            "entity_id INTEGER PRIMARY KEY, model TEXT NOT NULL, vector BLOB NOT NULL)"
        )
        conn.execute("DELETE FROM entity_embeddings")
        conn.executemany(
            "INSERT INTO entity_embeddings (entity_id, model, vector) VALUES (?, ?, ?)",
            [
                (entity_id, resolved_model, _pack(vector))
                for (entity_id, _), vector in zip(entities, vectors)
            ],
        )
        conn.commit()
        return len(entities)
    finally:
        conn.close()


def nearest_entities(audit_id: str, kind: str, name: str, k: int = 8) -> list[dict] | None:
    """Top-k entities (that have facts of this kind) by embedding similarity
    to `name`. Returns None when embeddings aren't built or the query can't
    be embedded -- callers degrade to the plain "no match" answer rather
    than failing the claim over a retrieval-quality nicety."""
    conn = sqlite3.connect(db_path(audit_id))
    try:
        if embedding_count(audit_id) == 0:
            return None
        rows = conn.execute(
            "SELECT entities.name, entity_embeddings.model, entity_embeddings.vector "
            "FROM entity_embeddings "
            "JOIN entities ON entities.entity_id = entity_embeddings.entity_id "
            "JOIN facts ON facts.entity_id = entities.entity_id "
            "JOIN documents ON documents.doc_id = facts.doc_id "
            "WHERE documents.kind = ? GROUP BY entities.entity_id",
            (kind,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None

    model = rows[0][1]
    cache_key = (model, name)
    if cache_key not in _query_cache:
        try:
            _query_cache[cache_key] = embed_texts([name], model)[0]
        except Exception:
            return None
    query_vector = _query_cache[cache_key]

    scored = sorted(
        ((entity, _cosine(query_vector, _unpack(blob))) for entity, _, blob in rows),
        key=lambda pair: pair[1],
        reverse=True,
    )
    return [{"entity": entity, "similarity": round(score, 3)} for entity, score in scored[:k]]
