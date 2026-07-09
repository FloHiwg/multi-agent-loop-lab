"""Graph layer over the facts index: canonical entities, normalized
periods/roles, and mined arithmetic edges.

The flat `facts` table is a proto-graph -- isolated entity/attribute/value
triples with no relationships. This module turns it into a real one, all
deterministically (no LLM calls) at `proofbench index` time:

- **Entity resolution**: one canonical entity node per normalized name
  (whitespace-collapsed, casefolded), merged across documents. "Enterprise
  customers" in the ops review and the same label in another doc become
  one node with two provenance links -- the principled version of what the
  alias hack approximated.
- **Period/role normalization**: attribute strings like "Q4 2025 actual"
  or "31 Dec 2025" parse into (period, role) columns on facts, so "same
  entity, same period, different value" -- the cross-document
  contradiction case -- becomes a query instead of a judgment call.
- **Arithmetic edge mining**: within each table, rows whose values are
  exactly the sum/difference of earlier rows get a typed edge
  (`derived_from` for pairs, `aggregates` for sums), checked across every
  column where all operands are present. This is what makes formula
  checks (gross margin, segment totals) graph traversals instead of
  agent arithmetic.

Consumed two ways: the Verifier's graph tools (tools.py: list_entities,
entity_profile) pull from these tables on demand -- pull-based context,
the coding-agent/RLM stance, rather than pushing digests into prompts --
and the planned recursive-verification phase traverses edges directly.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_QUARTER_YEAR_RE = re.compile(r"\bQ([1-4])\s+(\d{4})\b", re.IGNORECASE)
_QUARTER_RE = re.compile(r"\bQ([1-4])\b", re.IGNORECASE)
_DATE_RE = re.compile(
    r"\b(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{4})\b",
    re.IGNORECASE,
)

_ROLE_KEYWORDS = [
    ("actual", "actual"),
    ("budget", "budget"),
    ("forecast", "forecast"),
    ("definition", "definition"),
    ("change", "change"),
]


def normalize_name(name: str) -> str:
    return " ".join(name.split()).casefold()


def parse_attribute(attribute: str) -> tuple[str | None, str | None]:
    """attribute string -> (period, role), either may be None.
    "Q4 2025 actual" -> ("2025-Q4", "actual"); "31 Dec 2025" -> ("2025-12-31", None);
    "Q4 budget" -> ("Q4", "budget"); "Definition" -> (None, "definition")."""
    period: str | None = None
    if m := _QUARTER_YEAR_RE.search(attribute):
        period = f"{m.group(2)}-Q{m.group(1)}"
    elif m := _DATE_RE.search(attribute):
        day, month, year = int(m.group(1)), _MONTHS[m.group(2).lower()[:3]], int(m.group(3))
        period = f"{year:04d}-{month:02d}-{day:02d}"
    elif m := _QUARTER_RE.search(attribute):
        period = f"Q{m.group(1)}"

    role: str | None = None
    lowered = attribute.casefold()
    for keyword, value in _ROLE_KEYWORDS:
        if keyword in lowered:
            role = value
            break
    return period, role


def parse_number(text: str) -> float | None:
    t = text.strip().replace(",", "").replace("EUR", "").replace("€", "").strip()
    negative = t.startswith("(") and t.endswith(")")
    t = t.strip("()").strip()
    if t.endswith("%"):
        t = t[:-1].strip()
    try:
        value = float(t)
    except ValueError:
        return None
    return -value if negative else value


def _table_prefix(location: str) -> str:
    # "page:1/table:0/row:Revenue" -> "page:1/table:0"
    # "sheet:Customer Summary!row12" -> "sheet:Customer Summary"
    if "/row:" in location:
        return location.rsplit("/row:", 1)[0]
    if "!row" in location:
        return location.rsplit("!row", 1)[0]
    return location


@dataclass
class _Row:
    entity_id: int
    entity_name: str
    location: str
    values: dict[str, float]  # attribute -> parsed numeric value


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-4, abs_tol=0.01)


def _relation_holds(operands: list[dict[str, float]], target: dict[str, float], op: str) -> list[str]:
    """Return the attributes on which `target = op(operands)` holds.
    Requires at least two holding columns when the target has two or more
    numeric columns -- one column is too easy to satisfy by coincidence --
    but does NOT require every shared column to hold: a segment table's
    count and ARR columns aggregate while its ACV (ratio) column doesn't,
    and that's still a real aggregation edge."""
    common = [a for a in target if all(a in vals for vals in operands)]
    required = 2 if len(target) >= 2 else 1

    holding = []
    for attr in common:
        values = [vals[attr] for vals in operands]
        if op == "sum":
            result = sum(values)
        elif op == "diff":
            result = values[0] - values[1]
        else:
            raise ValueError(op)
        if _close(result, target[attr]):
            holding.append(attr)
    return holding if len(holding) >= required else []


def _mine_table_edges(rows: list[_Row]) -> list[dict]:
    edges = []
    for r in range(2, len(rows)):
        target = rows[r]
        if not target.values:
            continue
        found_for_target = []
        # pair relations: target = a + b or a - b, for any two earlier rows
        for i in range(r):
            for j in range(r):
                if i >= j or not rows[i].values or not rows[j].values:
                    continue
                for op, kind in (("sum", "aggregates"), ("diff", "derived_from")):
                    attrs = _relation_holds([rows[i].values, rows[j].values], target.values, op)
                    if attrs:
                        found_for_target.append(
                            {"kind": kind, "op": op, "inputs": [rows[i], rows[j]], "attributes": attrs}
                        )
        # run sums: target = sum of the k immediately preceding rows, k >= 3
        for k in range(3, r + 1):
            group = rows[r - k : r]
            if any(not g.values for g in group):
                continue
            attrs = _relation_holds([g.values for g in group], target.values, "sum")
            if attrs:
                found_for_target.append(
                    {"kind": "aggregates", "op": "sum", "inputs": group, "attributes": attrs}
                )
        for hit in found_for_target:
            edges.append(
                {
                    "kind": hit["kind"],
                    "op": hit["op"],
                    "target": target,
                    "inputs": hit["inputs"],
                    "attributes": hit["attributes"],
                }
            )
    return edges


def build_graph(conn: sqlite3.Connection) -> None:
    """Populate entities, edges, and facts.entity_id/period/role from the
    already-inserted facts rows. Called by index_db.build_index after the
    per-document indexing pass; idempotent over a fresh database."""
    facts = conn.execute("SELECT id, doc_id, location, entity, attribute, value FROM facts ORDER BY id").fetchall()

    # entity resolution: one node per normalized name, across documents
    entity_ids: dict[str, int] = {}
    for _, _, _, entity, _, _ in facts:
        norm = normalize_name(entity)
        if norm not in entity_ids:
            cursor = conn.execute("INSERT INTO entities (name, norm) VALUES (?, ?)", (entity, norm))
            entity_ids[norm] = cursor.lastrowid

    for fact_id, _, _, entity, attribute, _ in facts:
        period, role = parse_attribute(attribute)
        conn.execute(
            "UPDATE facts SET entity_id = ?, period = ?, role = ? WHERE id = ?",
            (entity_ids[normalize_name(entity)], period, role, fact_id),
        )

    # arithmetic edge mining, one table at a time, rows in document order
    tables: dict[tuple[str, str], dict[str, _Row]] = {}
    for _, doc_id, location, entity, attribute, value in facts:
        key = (doc_id, _table_prefix(location))
        row_map = tables.setdefault(key, {})
        row = row_map.setdefault(
            normalize_name(entity),
            _Row(entity_id=entity_ids[normalize_name(entity)], entity_name=entity, location=location, values={}),
        )
        parsed = parse_number(value)
        if parsed is not None:
            row.values[attribute] = parsed

    for (doc_id, prefix), row_map in tables.items():
        edges = _mine_table_edges(list(row_map.values()))
        for edge in edges:
            conn.execute(
                "INSERT INTO edges (kind, doc_id, target_entity_id, input_entity_ids_json, detail_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    edge["kind"],
                    doc_id,
                    edge["target"].entity_id,
                    json.dumps([r.entity_id for r in edge["inputs"]]),
                    json.dumps(
                        {
                            "op": edge["op"],
                            "table": prefix,
                            "target": edge["target"].entity_name,
                            "inputs": [r.entity_name for r in edge["inputs"]],
                            "attributes": edge["attributes"],
                        }
                    ),
                ),
            )


# --- read-side queries (plain functions so they're testable without MCP;
# --- the tools in tools.py are thin wrappers over these) ---


def list_entities_data(conn: sqlite3.Connection, kind: str) -> list[dict]:
    rows = conn.execute(
        "SELECT entities.name, GROUP_CONCAT(DISTINCT facts.doc_id) "
        "FROM entities JOIN facts ON facts.entity_id = entities.entity_id "
        "JOIN documents ON documents.doc_id = facts.doc_id "
        "WHERE documents.kind = ? GROUP BY entities.entity_id ORDER BY MIN(facts.id)",
        (kind,),
    ).fetchall()
    return [{"entity": name, "doc_ids": doc_ids.split(",")} for name, doc_ids in rows]


def _resolve_entity(conn: sqlite3.Connection, kind: str, name: str) -> tuple[int, str] | None:
    """Resolve a name to an entity that actually has facts of this kind --
    entities are global across documents, so an unscoped match can land on
    e.g. the master doc's "Cash" when the caller can only see vault facts."""
    norm = normalize_name(name)
    for clause, param in (("entities.norm = ?", norm), ("entities.norm LIKE ?", f"%{norm}%")):
        row = conn.execute(
            "SELECT entities.entity_id, entities.name FROM entities "
            "JOIN facts ON facts.entity_id = entities.entity_id "
            "JOIN documents ON documents.doc_id = facts.doc_id "
            f"WHERE documents.kind = ? AND {clause} "
            "GROUP BY entities.entity_id ORDER BY MIN(facts.id) LIMIT 1",
            (kind, param),
        ).fetchone()
        if row is not None:
            return row
    return None


def entity_profile_data(conn: sqlite3.Connection, kind: str, name: str) -> dict | None:
    """Everything the graph knows about one entity, kind-scoped: every fact
    (all docs, periods, roles) plus every mined edge it participates in.
    Resolves `name` by exact normalized match first, then substring."""
    resolved = _resolve_entity(conn, kind, name)
    if resolved is None:
        return None
    entity_id, canonical = resolved

    # span_text rides along so the agent can cite evidence verbatim straight
    # from the profile instead of following up with read_span calls per fact
    # (the measured tail of every verification trace).
    facts = conn.execute(
        "SELECT facts.doc_id, location, attribute, period, role, value, "
        "(SELECT text FROM spans_fts WHERE spans_fts.doc_id = facts.doc_id "
        " AND spans_fts.location = facts.location) AS span_text FROM facts "
        "JOIN documents ON documents.doc_id = facts.doc_id "
        "WHERE documents.kind = ? AND entity_id = ? ORDER BY facts.id",
        (kind, entity_id),
    ).fetchall()
    if not facts:
        return None

    edges = conn.execute(
        "SELECT edges.kind, edges.doc_id, target_entity_id, input_entity_ids_json, detail_json FROM edges "
        "JOIN documents ON documents.doc_id = edges.doc_id WHERE documents.kind = ?",
        (kind,),
    ).fetchall()

    edge_list = []
    for edge_kind, doc_id, target_id, input_ids_json, detail_json in edges:
        if target_id != entity_id and entity_id not in json.loads(input_ids_json):
            continue
        detail = json.loads(detail_json)
        edge_list.append(
            {
                "kind": edge_kind,
                "doc_id": doc_id,
                "op": detail["op"],
                "target": detail["target"],
                "inputs": detail["inputs"],
                "holds_for": detail["attributes"],
            }
        )

    return {
        "entity": canonical,
        "facts": [
            {
                "doc_id": d,
                "location": loc,
                "attribute": a,
                "period": p,
                "role": r,
                "value": v,
                "span_text": text,
            }
            for d, loc, a, p, r, v, text in facts
        ],
        "edges": edge_list,
    }
