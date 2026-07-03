"""Claim Extractor agent: master document -> typed, schema-valid Claim objects.

Bounded per CONCEPT.md §6: this agent gets read-only search/read tools
scoped to the master document only (see tools.py) -- it explores the
document itself, coding-agent style, rather than receiving the whole text
dumped into its prompt. It does not decide verdicts and cannot see the
vault; that bound comes from which tools it's handed, not from prompting.

Routed through manager.run_jobs (a single job) rather than called
directly, so a bad reply gets a proper failed RunManifest on disk instead
of just a raised exception with no audit trail -- see manager.py.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from proofbench.index_db import db_path
from proofbench.ingest import load_audit_config
from proofbench.jsonutil import extract_json
from proofbench.llm import resolve_model, run_agent
from proofbench.manager import Job, run_jobs
from proofbench.models import AgentRole, Claim, RunManifest
from proofbench.runlog import write_run_manifest
from proofbench.tools import allowed_tool_names, build_server

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_NAME = "proofbench_master"
JOB_ID = "master"

SYSTEM_PROMPT = """\
You are the Claim Extractor agent in Proofbench, an audit workbench.

You have read-only tools to explore the master document:
- list_documents: find the master document and see what locations it has
- read_span: read the full text of a page or table row
- search_vault / search_facts: search within the master document if it's long

Start by calling list_documents to find the master doc_id and its locations,
then read through it (read_span each location) to find every atomic,
independently verifiable numeric claim. A claim is one number with a clear
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

Once you've read the whole document, respond with ONLY a JSON array of these
objects as your final answer. No prose, no markdown fences.
"""


async def _process_extraction(audit_id: str, model: str, claims_out: list[Claim]) -> float | None:
    """The extraction job: run the agent, write claims + its own succeeded
    RunManifest, append results into claims_out, return cost_usd. Raises on
    failure -- the Manager catches that and records it.
    """
    config = load_audit_config(audit_id)
    server = build_server(audit_id, "master", SERVER_NAME)

    run_id = f"{audit_id}/extract-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    started_at = datetime.now(timezone.utc)

    reply = await run_agent(
        SYSTEM_PROMPT,
        "Extract all atomic numeric claims from the master document.",
        model=model,
        mcp_servers={SERVER_NAME: server},
        allowed_tools=allowed_tool_names(SERVER_NAME),
    )
    raw_claims = extract_json(reply.text)

    claims: list[Claim] = []
    for i, raw in enumerate(raw_claims, start=1):
        claim = Claim(
            claim_id=f"{audit_id}/claim-{i:04d}",
            source_doc_id=config.master_doc_id,
            **raw,
        )
        claims.append(claim)

    write_run_manifest(
        RunManifest(
            run_id=run_id,
            audit_id=audit_id,
            agent_role=AgentRole.CLAIM_EXTRACTOR,
            model=model,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            input_refs=[config.master_doc_id],
            output_refs=[c.claim_id for c in claims],
            prompt=SYSTEM_PROMPT,
            cost_usd=reply.cost_usd,
            status="succeeded",
        )
    )
    _write_claims(audit_id, claims)
    claims_out.extend(claims)
    return reply.cost_usd


async def extract_claims_async(
    audit_id: str, *, max_budget_usd: float | None = None
) -> list[Claim]:
    if not db_path(audit_id).exists():
        raise FileNotFoundError(f"{db_path(audit_id)} not found -- run `proofbench index {audit_id}` first")

    model = resolve_model()
    claims: list[Claim] = []
    job = Job(
        job_id=JOB_ID,
        audit_id=audit_id,
        agent_role=AgentRole.CLAIM_EXTRACTOR,
        model=model,
        run_fn=lambda: _process_extraction(audit_id, model, claims),
    )
    report = await run_jobs([job], max_concurrency=1, max_budget_usd=max_budget_usd)

    if JOB_ID in report.failed:
        raise RuntimeError(f"claim extraction failed: {report.failed[JOB_ID]}")
    if JOB_ID in report.skipped_budget:
        raise RuntimeError("claim extraction skipped: max_budget_usd exhausted before it could run")
    return claims


def extract_claims(audit_id: str, *, max_budget_usd: float | None = None) -> list[Claim]:
    return asyncio.run(extract_claims_async(audit_id, max_budget_usd=max_budget_usd))


def _write_claims(audit_id: str, claims: list[Claim]) -> None:
    claims_dir = REPO_ROOT / "audits" / audit_id / "claims"
    claims_dir.mkdir(parents=True, exist_ok=True)
    for claim in claims:
        suffix = claim.claim_id.split("/")[-1]
        (claims_dir / f"{suffix}.json").write_text(claim.model_dump_json(indent=2) + "\n")
