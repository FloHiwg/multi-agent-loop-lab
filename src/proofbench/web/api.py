"""Read-only FastAPI backend for the Proofbench workbench.

Every endpoint just reads the existing on-disk state (audits/, runs/,
index/search/<id>.db) -- there is no separate database or cache to keep in
sync. That also means "live" progress is just the frontend re-polling
these endpoints while `proofbench extract`/`verify` writes new files in
another terminal; see web/static/index.html.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import fitz  # PyMuPDF
import yaml
from fastapi import FastAPI, HTTPException, Response
from fastapi.staticfiles import StaticFiles

from proofbench.index_db import db_path
from proofbench.models import AuditConfig, DocumentRef

REPO_ROOT = Path(__file__).resolve().parents[3]
AUDITS_DIR = REPO_ROOT / "audits"
RUNS_DIR = REPO_ROOT / "runs"
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Proofbench Workbench")


def _load_audit_config(audit_id: str) -> AuditConfig:
    path = AUDITS_DIR / audit_id / "audit.yaml"
    if not path.exists():
        raise HTTPException(404, f"no such audit: {audit_id}")
    return AuditConfig.model_validate(yaml.safe_load(path.read_text()))


def _read_json_dir(dir_path: Path) -> dict[str, dict]:
    if not dir_path.exists():
        return {}
    return {p.stem: json.loads(p.read_text()) for p in dir_path.glob("*.json")}


@app.get("/api/audits")
def list_audits() -> list[dict]:
    if not AUDITS_DIR.exists():
        return []
    out = []
    for path in sorted(AUDITS_DIR.iterdir()):
        if not (path / "audit.yaml").exists():
            continue
        config = _load_audit_config(path.name)
        claims = _read_json_dir(path / "claims")
        out.append({"audit_id": path.name, "master_doc_id": config.master_doc_id, "claim_count": len(claims)})
    return out


@app.get("/api/audits/{audit_id}")
def audit_detail(audit_id: str) -> dict:
    config = _load_audit_config(audit_id)
    claims, statuses = _claims_with_status(audit_id)
    coverage: dict[str, int] = {}
    for c in claims:
        coverage[c["effective_status"]] = coverage.get(c["effective_status"], 0) + 1
    return {
        "audit_id": audit_id,
        "master_doc_id": config.master_doc_id,
        "evidence_priority": config.evidence_priority,
        "documents": [d.model_dump() for d in config.documents],
        "coverage": coverage,
        "claim_count": len(claims),
    }


def _claims_with_status(audit_id: str) -> tuple[list[dict], dict[str, str]]:
    claims_dir = AUDITS_DIR / audit_id / "claims"
    results = _read_json_dir(AUDITS_DIR / audit_id / "results")
    reviews = _read_json_dir(AUDITS_DIR / audit_id / "review_queue")
    failed_claim_ids = _failed_claim_ids(audit_id)

    claims = []
    statuses: dict[str, str] = {}
    for path in sorted(claims_dir.glob("*.json")) if claims_dir.exists() else []:
        claim = json.loads(path.read_text())
        claim_id = claim["claim_id"]
        suffix = path.stem
        if suffix in results:
            effective_status = results[suffix]["verdict"]["status"]
        elif claim_id in failed_claim_ids:
            effective_status = "failed"
        else:
            effective_status = "pending"
        claim["effective_status"] = effective_status
        claim["has_review_card"] = suffix in reviews
        claims.append(claim)
        statuses[claim_id] = effective_status
    return claims, statuses


def _failed_claim_ids(audit_id: str) -> set[str]:
    ids = set()
    if not RUNS_DIR.exists():
        return ids
    for path in RUNS_DIR.glob(f"{audit_id}__failed-*.json"):
        manifest = json.loads(path.read_text())
        ids.update(manifest.get("input_refs", []))
    return ids


@app.get("/api/audits/{audit_id}/claims")
def list_claims(audit_id: str) -> list[dict]:
    claims, _ = _claims_with_status(audit_id)
    return claims


@app.get("/api/audits/{audit_id}/claims/{claim_suffix}")
def claim_detail(audit_id: str, claim_suffix: str) -> dict:
    claims_dir = AUDITS_DIR / audit_id / "claims"
    claim_path = claims_dir / f"{claim_suffix}.json"
    if not claim_path.exists():
        raise HTTPException(404, f"no such claim: {claim_suffix}")
    claim = json.loads(claim_path.read_text())

    results = _read_json_dir(AUDITS_DIR / audit_id / "results")
    reviews = _read_json_dir(AUDITS_DIR / audit_id / "review_queue")
    result = results.get(claim_suffix) or reviews.get(claim_suffix)

    run = None
    if result:
        run_id = result["verdict"].get("produced_by_run_id")
        if run_id:
            run = _find_run_manifest(run_id)

    failed_claim_ids = _failed_claim_ids(audit_id)
    effective_status = (
        result["verdict"]["status"] if result else ("failed" if claim["claim_id"] in failed_claim_ids else "pending")
    )

    return {
        "claim": claim,
        "effective_status": effective_status,
        "evidence": result["evidence"] if result else [],
        "verdict": result["verdict"] if result else None,
        "human_decision": result.get("human_decision") if claim_suffix in reviews else None,
        "run": run,
    }


@app.get("/api/audits/{audit_id}/vault")
def vault_detail(audit_id: str) -> dict:
    config = _load_audit_config(audit_id)
    path = db_path(audit_id)
    if not path.exists():
        return {"documents": [d.model_dump() for d in config.documents], "indexed": False}

    conn = sqlite3.connect(path)
    try:
        docs = []
        for doc in config.documents:
            span_count = conn.execute("SELECT COUNT(*) FROM spans_fts WHERE doc_id = ?", (doc.doc_id,)).fetchone()[0]
            fact_count = conn.execute("SELECT COUNT(*) FROM facts WHERE doc_id = ?", (doc.doc_id,)).fetchone()[0]
            docs.append({**doc.model_dump(), "span_count": span_count, "fact_count": fact_count})
        return {"documents": docs, "indexed": True}
    finally:
        conn.close()


def _find_document(config: AuditConfig, doc_id: str) -> DocumentRef:
    for doc in config.documents:
        if doc.doc_id == doc_id:
            return doc
    raise HTTPException(404, f"no such document: {doc_id}")


@app.get("/api/audits/{audit_id}/docs/{doc_id}/pages")
def doc_pages(audit_id: str, doc_id: str) -> dict:
    config = _load_audit_config(audit_id)
    doc = _find_document(config, doc_id)
    if doc.format.value != "pdf":
        return {"format": doc.format.value, "pages": []}

    conn = sqlite3.connect(db_path(audit_id))
    try:
        rows = conn.execute(
            "SELECT page, width, height FROM pages WHERE doc_id = ? ORDER BY page", (doc_id,)
        ).fetchall()
    finally:
        conn.close()
    return {"format": "pdf", "pages": [{"page": p, "width": w, "height": h} for p, w, h in rows]}


@app.get("/api/audits/{audit_id}/docs/{doc_id}/page/{page_num}.png")
def doc_page_png(audit_id: str, doc_id: str, page_num: int) -> Response:
    config = _load_audit_config(audit_id)
    doc = _find_document(config, doc_id)
    if doc.format.value != "pdf":
        raise HTTPException(400, "page rendering is only supported for PDF documents")

    with fitz.open(REPO_ROOT / doc.path) as pdf:
        if page_num < 1 or page_num > pdf.page_count:
            raise HTTPException(404, f"page {page_num} out of range (doc has {pdf.page_count} pages)")
        pixmap = pdf[page_num - 1].get_pixmap(dpi=150)
        png_bytes = pixmap.tobytes("png")
    return Response(content=png_bytes, media_type="image/png")


@app.get("/api/audits/{audit_id}/docs/{doc_id}/highlights")
def doc_highlights(audit_id: str, doc_id: str, page: int) -> list[dict]:
    """Claims (if doc_id is the master doc) or evidence spans (if it's a
    vault doc) located on this page, each with a bounding box for the
    frontend to draw a clickable highlight over the rendered page image.
    """
    config = _load_audit_config(audit_id)
    doc = _find_document(config, doc_id)
    if doc.format.value != "pdf":
        return []

    if doc_id == config.master_doc_id:
        return _claim_highlights(audit_id, doc, page)
    return _evidence_highlights(audit_id, doc, page)


def _claim_highlights(audit_id: str, doc: DocumentRef, page: int) -> list[dict]:
    claims, statuses = _claims_with_status(audit_id)
    on_page = [c for c in claims if c.get("source_doc_id") == doc.doc_id and c.get("source_page") == page]
    if not on_page:
        return []

    highlights = []
    with fitz.open(REPO_ROOT / doc.path) as pdf:
        pdf_page = pdf[page - 1]
        for claim in on_page:
            rects = pdf_page.search_for(claim["raw_text"])
            for rect in rects:
                highlights.append(
                    {
                        "kind": "claim",
                        "claim_id": claim["claim_id"],
                        "label": claim["label"],
                        "status": claim["effective_status"],
                        "raw_text": claim["raw_text"],
                        "canonical_value": claim["canonical_value"],
                        "unit": claim["unit"],
                        "bbox": list(rect),
                    }
                )
    return highlights


def _evidence_highlights(audit_id: str, doc: DocumentRef, page: int) -> list[dict]:
    results = _read_json_dir(AUDITS_DIR / audit_id / "results")
    reviews = _read_json_dir(AUDITS_DIR / audit_id / "review_queue")
    all_results = {**results, **reviews}

    entries = []
    for suffix, result in all_results.items():
        verdict = result["verdict"]
        for evidence in result["evidence"]:
            if evidence["doc_id"] == doc.doc_id and evidence.get("page") == page:
                entries.append((suffix, verdict, evidence))
    if not entries:
        return []

    conn = sqlite3.connect(db_path(audit_id))
    highlights = []
    pdf = fitz.open(REPO_ROOT / doc.path)
    try:
        pdf_page = pdf[page - 1]
        for suffix, verdict, evidence in entries:
            bbox = _lookup_indexed_bbox(conn, doc.doc_id, evidence["span_text"])
            if bbox is None:
                rects = pdf_page.search_for(evidence["span_text"])
                bboxes = [list(r) for r in rects]
            else:
                bboxes = [bbox]
            for b in bboxes:
                highlights.append(
                    {
                        "kind": "evidence",
                        "claim_id": verdict["claim_id"],
                        "evidence_id": evidence["evidence_id"],
                        "status": verdict["status"],
                        "span_text": evidence["span_text"],
                        "canonical_value": evidence.get("canonical_value"),
                        "unit": evidence.get("unit"),
                        "bbox": b,
                    }
                )
    finally:
        pdf.close()
        conn.close()
    return highlights


def _lookup_indexed_bbox(conn: sqlite3.Connection, doc_id: str, text: str) -> list[float] | None:
    """Exact-match the evidence's span_text against our own indexed span
    text to recover the bbox recorded at index time -- reliable for
    table-row evidence, since the model's span_text is literally copied
    from what search_vault/read_span returned (see verification.py's
    system prompt). Falls back to a live PDF text search by the caller
    when this returns None (narrative/paragraph evidence, whose span_text
    is a real substring of the page but not a whole indexed span).
    """
    row = conn.execute(
        """
        SELECT locations.bbox_json FROM spans_fts
        JOIN locations ON locations.doc_id = spans_fts.doc_id AND locations.location = spans_fts.location
        WHERE spans_fts.doc_id = ? AND spans_fts.text = ? AND locations.bbox_json IS NOT NULL
        LIMIT 1
        """,
        (doc_id, text),
    ).fetchone()
    if row is None:
        return None
    return json.loads(row[0])


def _find_run_manifest(run_id: str) -> dict | None:
    suffix = run_id.replace("/", "__")
    path = RUNS_DIR / f"{suffix}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


@app.get("/api/runs")
def list_runs(audit_id: str | None = None) -> list[dict]:
    if not RUNS_DIR.exists():
        return []
    runs = []
    for path in RUNS_DIR.glob("*.json"):
        manifest = json.loads(path.read_text())
        if audit_id and manifest.get("audit_id") != audit_id:
            continue
        runs.append(
            {
                "run_id": manifest["run_id"],
                "audit_id": manifest["audit_id"],
                "agent_role": manifest["agent_role"],
                "model": manifest["model"],
                "status": manifest["status"],
                "cost_usd": manifest.get("cost_usd"),
                "started_at": manifest["started_at"],
                "finished_at": manifest.get("finished_at"),
                "input_refs": manifest.get("input_refs", []),
                "error": manifest.get("error"),
                "tool_call_count": len(manifest.get("tool_trace", [])),
            }
        )
    runs.sort(key=lambda r: r["started_at"], reverse=True)
    return runs


@app.get("/api/runs/{run_id:path}")
def run_detail(run_id: str) -> dict:
    manifest = _find_run_manifest(run_id)
    if manifest is None:
        raise HTTPException(404, f"no such run: {run_id}")
    return manifest


@app.get("/api/history/{manager_run_id:path}")
def run_history(manager_run_id: str) -> dict:
    """Stitch a Manager run together with every job it scheduled into one
    chronological narrative: orchestrator starts -> workers spawned (each
    with its full tool-call trace and final answer) -> orchestrator
    finishes. This is what the workbench's History view renders -- one
    connected trace instead of separate manager/job panels a person has
    to click between.
    """
    manager = _find_run_manifest(manager_run_id)
    if manager is None:
        raise HTTPException(404, f"no such run: {manager_run_id}")
    if manager.get("agent_role") != "manager":
        raise HTTPException(400, f"{manager_run_id} is not a Manager run (agent_role={manager.get('agent_role')})")

    jobs = []
    for outcome in manager.get("job_outcomes") or []:
        job_manifest = _find_run_manifest(outcome["run_id"])
        jobs.append(
            {
                "job_id": outcome["job_id"],
                "run_id": outcome["run_id"],
                "outcome_status": outcome["status"],
                "cost_usd": outcome["cost_usd"],
                "manifest": job_manifest,  # None for skipped_budget jobs that never ran
            }
        )
    jobs.sort(key=lambda j: (j["manifest"] or {}).get("started_at") or "")

    return {"manager": manager, "jobs": jobs}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
