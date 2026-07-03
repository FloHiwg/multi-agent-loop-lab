"""Claim Extractor agent: master document text -> typed, schema-valid Claim objects.

Bounded per CONCEPT.md §6: this agent reads one document and emits claims or
nothing. It does not decide verdicts and does not see the vault.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from proofbench.corpus import render_document
from proofbench.ingest import load_audit_config
from proofbench.jsonutil import extract_json
from proofbench.llm import resolve_model, run_agent
from proofbench.models import AgentRole, Claim, RunManifest

REPO_ROOT = Path(__file__).resolve().parents[2]

SYSTEM_PROMPT = """\
You are the Claim Extractor agent in Proofbench, an audit workbench.

Read the master document text you are given and split it into atomic,
independently verifiable numeric claims. A claim is one number with a clear
meaning -- do not combine two figures into one claim, and do not editorialize.

For each claim, output an object with exactly these fields:
- label: short human-readable name, e.g. "FY2025 revenue"
- raw_text: the verbatim sentence or phrase the number came from
- canonical_value: the number, normalized (e.g. "EUR 4.28 million" -> 4280000, "63.0%" -> 0.63)
- unit: one of "currency", "percent", "percent_change", "count", "ratio"
- currency: ISO 4217 code if unit is "currency", else null
- entity: which company/subsidiary/party this is about
- time_scope: the period or as-of date, e.g. "FY2025" or "2025-12-31"
- expected_evidence_type: your best guess at which kind of source document would support this
  (e.g. "finance_pack", "operations_review", "customer_appendix"), or null if unclear
- source_page: the page number in the master document this came from, or null

Respond with ONLY a JSON array of these objects. No prose, no markdown fences.
"""


async def extract_claims_async(audit_id: str) -> list[Claim]:
    config = load_audit_config(audit_id)
    master_text = render_document(config.master_doc_id)

    run_id = f"{audit_id}/extract-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    started_at = datetime.now(timezone.utc)
    model = resolve_model()

    reply = await run_agent(SYSTEM_PROMPT, master_text, model=model)
    raw_claims = extract_json(reply)

    claims: list[Claim] = []
    for i, raw in enumerate(raw_claims, start=1):
        claim = Claim(
            claim_id=f"{audit_id}/claim-{i:04d}",
            source_doc_id=config.master_doc_id,
            **raw,
        )
        claims.append(claim)

    manifest = RunManifest(
        run_id=run_id,
        audit_id=audit_id,
        agent_role=AgentRole.CLAIM_EXTRACTOR,
        model=model,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
        input_refs=[config.master_doc_id],
        output_refs=[c.claim_id for c in claims],
        prompt=SYSTEM_PROMPT,
        status="succeeded",
    )
    _write_run_manifest(manifest)
    _write_claims(audit_id, claims)
    return claims


def extract_claims(audit_id: str) -> list[Claim]:
    return asyncio.run(extract_claims_async(audit_id))


def _write_claims(audit_id: str, claims: list[Claim]) -> None:
    claims_dir = REPO_ROOT / "audits" / audit_id / "claims"
    claims_dir.mkdir(parents=True, exist_ok=True)
    for claim in claims:
        suffix = claim.claim_id.split("/")[-1]
        (claims_dir / f"{suffix}.json").write_text(claim.model_dump_json(indent=2) + "\n")


def _write_run_manifest(manifest: RunManifest) -> None:
    runs_dir = REPO_ROOT / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    suffix = manifest.run_id.replace("/", "__")
    (runs_dir / f"{suffix}.json").write_text(manifest.model_dump_json(indent=2) + "\n")
