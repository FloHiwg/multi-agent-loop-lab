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

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from proofbench.index_db import db_path
from proofbench.models import AuditConfig

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


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
