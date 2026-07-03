"""In-process MCP tools that let an agent search the vault/master index
itself, coding-agent style, instead of receiving the whole corpus stuffed
into its prompt. Tools are scoped to one document `kind` ("master" or
"vault") per caller, so the Claim Extractor can't see vault documents and
the Verifier can't see the master document -- the bound comes from which
tools it's handed, not from trusting the model to stay in its lane.
"""

from __future__ import annotations

import json
import sqlite3

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server, tool

from proofbench.index_db import db_path

_RESULT_CAP = 20


def _connect(audit_id: str) -> sqlite3.Connection:
    return sqlite3.connect(db_path(audit_id))


def _tool_result(payload: object) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}]}


def _list_documents_tool(audit_id: str, kind: str) -> SdkMcpTool:
    @tool(
        "list_documents",
        "List documents available to search, optionally filtered by evidence tag "
        "(e.g. 'finance_pack', 'operations_review', 'customer_appendix'). Each document's "
        "`locations` lists exactly what you can pass to read_span for that doc_id -- "
        "e.g. 'page:1' for a PDF page, 'sheet:Segment Detail' for a whole XLSX sheet, or "
        "'page:1/table:2/row:Revenue' for one extracted table row.",
        {
            "type": "object",
            "properties": {"tag": {"type": ["string", "null"], "description": "Evidence tag to filter by"}},
            "required": [],
        },
    )
    async def list_documents(args: dict) -> dict:
        conn = _connect(audit_id)
        try:
            tag = args.get("tag")
            query = "SELECT doc_id, tag, format FROM documents WHERE kind = ?"
            params: list = [kind]
            if tag:
                query += " AND tag = ?"
                params.append(tag)
            rows = conn.execute(query, params).fetchall()
            result = []
            for doc_id, doc_tag, fmt in rows:
                locations = [
                    r[0]
                    for r in conn.execute(
                        "SELECT location FROM spans_fts WHERE doc_id = ? ORDER BY rowid", (doc_id,)
                    ).fetchall()
                ]
                result.append({"doc_id": doc_id, "tag": doc_tag, "format": fmt, "locations": locations})
            return _tool_result(result)
        finally:
            conn.close()

    return list_documents


def _search_vault_tool(audit_id: str, kind: str) -> SdkMcpTool:
    @tool(
        "search_vault",
        "Full-text search over document contents (pages, table rows, spreadsheet sheets). "
        "Returns matching locations with a text snippet -- use read_span to get the full text "
        "of a specific match.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms (FTS5 syntax supported)"},
                "tag": {"type": ["string", "null"], "description": "Restrict to documents with this evidence tag"},
            },
            "required": ["query"],
        },
    )
    async def search_vault(args: dict) -> dict:
        conn = _connect(audit_id)
        try:
            sql = (
                "SELECT spans_fts.doc_id, location, snippet(spans_fts, 2, '[', ']', '...', 12) "
                "FROM spans_fts JOIN documents ON documents.doc_id = spans_fts.doc_id "
                "WHERE documents.kind = ? AND spans_fts MATCH ?"
            )
            params: list = [kind, args["query"]]
            if args.get("tag"):
                sql += " AND documents.tag = ?"
                params.append(args["tag"])
            sql += " LIMIT ?"
            params.append(_RESULT_CAP)
            rows = conn.execute(sql, params).fetchall()
            return _tool_result([{"doc_id": r[0], "location": r[1], "snippet": r[2]} for r in rows])
        except sqlite3.OperationalError as e:
            return {"content": [{"type": "text", "text": f"search error: {e}"}], "is_error": True}
        finally:
            conn.close()

    return search_vault


def _search_facts_tool(audit_id: str, kind: str) -> SdkMcpTool:
    @tool(
        "search_facts",
        "Search the structured entity/attribute/value facts extracted from tables and "
        "spreadsheets (e.g. entity='Revenue', attribute='Q4 2025 actual'). Use this for "
        "precise numeric lookups; use search_vault for free-text search.",
        {
            "type": "object",
            "properties": {
                "entity": {"type": ["string", "null"], "description": "Row label to match (substring, case-insensitive)"},
                "attribute": {"type": ["string", "null"], "description": "Column header to match (substring, case-insensitive)"},
            },
            "required": [],
        },
    )
    async def search_facts(args: dict) -> dict:
        conn = _connect(audit_id)
        try:
            sql = (
                "SELECT facts.doc_id, location, entity, attribute, value "
                "FROM facts JOIN documents ON documents.doc_id = facts.doc_id "
                "WHERE documents.kind = ?"
            )
            params: list = [kind]
            if args.get("entity"):
                sql += " AND entity LIKE ?"
                params.append(f"%{args['entity']}%")
            if args.get("attribute"):
                sql += " AND attribute LIKE ?"
                params.append(f"%{args['attribute']}%")
            sql += " LIMIT ?"
            params.append(_RESULT_CAP)
            rows = conn.execute(sql, params).fetchall()
            return _tool_result(
                [{"doc_id": r[0], "location": r[1], "entity": r[2], "attribute": r[3], "value": r[4]} for r in rows]
            )
        finally:
            conn.close()

    return search_facts


def _read_span_tool(audit_id: str, kind: str) -> SdkMcpTool:
    @tool(
        "read_span",
        "Read the full text of a specific location returned by search_vault or search_facts.",
        {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string"},
                "location": {"type": "string"},
            },
            "required": ["doc_id", "location"],
        },
    )
    async def read_span(args: dict) -> dict:
        conn = _connect(audit_id)
        try:
            row = conn.execute(
                "SELECT spans_fts.text FROM spans_fts JOIN documents ON documents.doc_id = spans_fts.doc_id "
                "WHERE documents.kind = ? AND spans_fts.doc_id = ? AND location = ?",
                (kind, args["doc_id"], args["location"]),
            ).fetchone()
            if row is None:
                return {"content": [{"type": "text", "text": "no span found at that doc_id/location"}], "is_error": True}
            return _tool_result({"doc_id": args["doc_id"], "location": args["location"], "text": row[0]})
        finally:
            conn.close()

    return read_span


def build_server(audit_id: str, kind: str, server_name: str) -> McpSdkServerConfig:
    """Build an in-process MCP server exposing search/read tools scoped to one
    document kind ("master" or "vault") of one audit's index.
    """
    tools = [
        _list_documents_tool(audit_id, kind),
        _search_vault_tool(audit_id, kind),
        _search_facts_tool(audit_id, kind),
        _read_span_tool(audit_id, kind),
    ]
    return create_sdk_mcp_server(server_name, tools=tools)


def allowed_tool_names(server_name: str) -> list[str]:
    return [
        f"mcp__{server_name}__list_documents",
        f"mcp__{server_name}__search_vault",
        f"mcp__{server_name}__search_facts",
        f"mcp__{server_name}__read_span",
    ]
