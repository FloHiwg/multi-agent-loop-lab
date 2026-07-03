"""Build a searchable SQLite index from index/parsed/<doc_id>.json.

Two complementary structures, both rebuildable from index/parsed/ (never
from vault/ directly -- ingest is the only thing that reads raw documents):

- `spans_fts`: full-text search over page/sheet/table-row-sized chunks, so
  an agent can grep the corpus instead of reading whole documents.
- `facts`: a lightweight entity/attribute/value graph (row label -> column
  header -> cell value), built from PDF tables (via PyMuPDF's find_tables(),
  which gives reliable header/data structure) and from XLSX sheets (using a
  nearest-header-above heuristic, since cells there are just a flat list of
  refs). This is what lets an agent ask "all actuals for Northstar Q4"
  instead of only keyword matches.

There's also a `locations` table -- doc_id + location -> page + bounding
box, from PyMuPDF's per-row table bboxes -- that the workbench UI's
Document view uses to draw real highlight boxes over a rendered page
image (web/api.py's document-view endpoints), rather than guessing where
a claim or evidence span sits on the page.

Tool functions in tools.py query this database; nothing else should.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from proofbench.graph import build_graph
from proofbench.ingest import INDEX_PARSED_DIR, load_audit_config

REPO_ROOT = Path(__file__).resolve().parents[2]
INDEX_SEARCH_DIR = REPO_ROOT / "index" / "search"

SCHEMA = """
CREATE TABLE documents (
    doc_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    tag TEXT,
    format TEXT NOT NULL
);

CREATE VIRTUAL TABLE spans_fts USING fts5(doc_id, location, text);

CREATE TABLE facts (
    id INTEGER PRIMARY KEY,
    doc_id TEXT NOT NULL,
    location TEXT NOT NULL,
    entity TEXT NOT NULL,
    attribute TEXT NOT NULL,
    value TEXT NOT NULL,
    entity_id INTEGER,
    period TEXT,
    role TEXT
);

CREATE TABLE entities (
    entity_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    norm TEXT NOT NULL UNIQUE
);

CREATE TABLE edges (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    target_entity_id INTEGER NOT NULL,
    input_entity_ids_json TEXT NOT NULL,
    detail_json TEXT NOT NULL
);

CREATE TABLE fact_aliases (
    entity TEXT NOT NULL,
    alias TEXT NOT NULL,
    PRIMARY KEY (entity, alias)
);

CREATE TABLE locations (
    doc_id TEXT NOT NULL,
    location TEXT NOT NULL,
    page INTEGER,
    bbox_json TEXT,
    PRIMARY KEY (doc_id, location)
);

CREATE TABLE pages (
    doc_id TEXT NOT NULL,
    page INTEGER NOT NULL,
    width REAL NOT NULL,
    height REAL NOT NULL,
    PRIMARY KEY (doc_id, page)
);
"""


def db_path(audit_id: str) -> Path:
    return INDEX_SEARCH_DIR / f"{audit_id}.db"


def build_index(audit_id: str) -> Path:
    config = load_audit_config(audit_id)
    INDEX_SEARCH_DIR.mkdir(parents=True, exist_ok=True)
    path = db_path(audit_id)
    path.unlink(missing_ok=True)

    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        for doc in config.documents:
            parsed = json.loads((INDEX_PARSED_DIR / f"{doc.doc_id}.json").read_text())
            conn.execute(
                "INSERT INTO documents (doc_id, kind, tag, format) VALUES (?, ?, ?, ?)",
                (doc.doc_id, doc.kind.value, doc.tag, doc.format.value),
            )
            if doc.format.value == "pdf":
                _index_pdf(conn, doc.doc_id, parsed)
            elif doc.format.value == "xlsx":
                _index_xlsx(conn, doc.doc_id, parsed)
        # graph layer over the freshly-inserted facts: canonical entities,
        # period/role normalization, mined arithmetic edges (see graph.py)
        build_graph(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def _index_pdf(conn: sqlite3.Connection, doc_id: str, parsed: dict) -> None:
    for page in parsed["pages"]:
        conn.execute(
            "INSERT INTO spans_fts (doc_id, location, text) VALUES (?, ?, ?)",
            (doc_id, f"page:{page['page']}", page["text"]),
        )
        conn.execute(
            "INSERT INTO locations (doc_id, location, page, bbox_json) VALUES (?, ?, ?, NULL)",
            (doc_id, f"page:{page['page']}", page["page"]),
        )
        conn.execute(
            "INSERT INTO pages (doc_id, page, width, height) VALUES (?, ?, ?, ?)",
            (doc_id, page["page"], page["width"], page["height"]),
        )

    for table in parsed.get("tables", []):
        rows = table["rows"]
        row_bboxes = table.get("row_bboxes", [])
        if len(rows) < 2:
            continue
        header, data_rows = rows[0], rows[1:]
        # A 2-column table (label, value) has no real header row -- rows[0]
        # is just the first data pair, e.g. ["DOCUMENT ID", "..."]. Only
        # treat rows[0] as a header, and emit facts, when there's more than
        # one attribute column to actually name. Row-level text is still
        # indexed either way, since it's valid full-text search material.
        has_header = len(header) >= 3
        location_prefix = f"page:{table['page']}/table:{table['table_index']}"
        for row_index, row in enumerate(data_rows, start=1):
            entity = str(row[0] or "").strip()
            if not entity:
                continue
            row_text = " | ".join(str(c) for c in row if c is not None)
            row_location = f"{location_prefix}/row:{entity}"
            conn.execute(
                "INSERT INTO spans_fts (doc_id, location, text) VALUES (?, ?, ?)",
                (doc_id, row_location, row_text),
            )
            bbox = row_bboxes[row_index] if row_index < len(row_bboxes) else None
            conn.execute(
                "INSERT OR REPLACE INTO locations (doc_id, location, page, bbox_json) VALUES (?, ?, ?, ?)",
                (doc_id, row_location, table["page"], json.dumps(bbox) if bbox else None),
            )
            if not has_header:
                continue
            for col_index, value in enumerate(row[1:], start=1):
                if value is None or col_index >= len(header) or not header[col_index]:
                    continue
                conn.execute(
                    "INSERT INTO facts (doc_id, location, entity, attribute, value) VALUES (?, ?, ?, ?, ?)",
                    (doc_id, row_location, entity, str(header[col_index]).strip(), str(value)),
                )


def _index_xlsx(conn: sqlite3.Connection, doc_id: str, parsed: dict) -> None:
    for sheet in parsed["sheets"]:
        sheet_name = sheet["sheet"]
        conn.execute(
            "INSERT INTO spans_fts (doc_id, location, text) VALUES (?, ?, ?)",
            (doc_id, f"sheet:{sheet_name}", "\n".join(f"{c['ref']}: {c['value']}" for c in sheet["cells"])),
        )

        rows: dict[int, dict[str, object]] = {}
        for cell in sheet["cells"]:
            col_letters = "".join(ch for ch in cell["ref"] if ch.isalpha())
            row_num = int("".join(ch for ch in cell["ref"] if ch.isdigit()))
            rows.setdefault(row_num, {})[col_letters] = cell["value"]

        last_header: dict[str, object] | None = None
        for row_num in sorted(rows):
            row = rows[row_num]
            values = list(row.values())
            is_header_candidate = len(values) >= 2 and all(isinstance(v, str) for v in values)
            has_numeric = any(isinstance(v, (int, float)) for v in values)

            if is_header_candidate and not has_numeric:
                last_header = row
                continue

            if has_numeric and last_header:
                cols_sorted = sorted(row.keys(), key=_col_to_index)
                entity_col = cols_sorted[0]
                entity = row.get(entity_col)
                if not isinstance(entity, str) or not entity.strip():
                    continue
                row_location = f"sheet:{sheet_name}!row{row_num}"
                for col, value in row.items():
                    if col == entity_col or value is None:
                        continue
                    attribute = last_header.get(col, col)
                    conn.execute(
                        "INSERT INTO facts (doc_id, location, entity, attribute, value) VALUES (?, ?, ?, ?, ?)",
                        (doc_id, row_location, entity.strip(), str(attribute), str(value)),
                    )


def _col_to_index(col_letters: str) -> int:
    index = 0
    for ch in col_letters:
        index = index * 26 + (ord(ch.upper()) - ord("A") + 1)
    return index
