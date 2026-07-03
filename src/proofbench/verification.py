"""Vault Retriever + Verifier, combined into one tool-equipped agent.

CONCEPT.md splits retrieval and verification into separate agents; here
they're one agent that searches the vault itself via tools scoped to
vault-kind documents (see tools.py) -- full-text search_vault plus the
structured search_facts entity/attribute graph, coding-agent style,
rather than the whole corpus stuffed into the prompt. Split retrieval into
its own agent only if verification quality suggests the search step itself
needs different judgment than the compare-and-decide step.

The Verifier never decides alone in the product sense -- everything short
of `supported` lands in review_queue/ for a human.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from proofbench.index_db import db_path
from proofbench.jsonutil import extract_json
from proofbench.llm import resolve_model, run_agent
from proofbench.models import (
    AgentRole,
    Claim,
    EvidenceCandidate,
    RunManifest,
    Verdict,
    VerdictStatus,
)
from proofbench.tools import allowed_tool_names, build_server

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_NAME = "proofbench_vault"

SYSTEM_PROMPT = """\
You are the Verifier agent in Proofbench, an audit workbench.

You are given one numeric claim from a master document. You have read-only
tools to search the vault (all basis/support documents):
- list_documents: see what vault documents exist and their locations
- search_facts: structured search over extracted entity/attribute/value facts
  (e.g. entity="Revenue", attribute="Q4 2025 actual") -- prefer this for
  precise numeric lookups
- search_vault: full-text search over document contents
- read_span: read the full text of a specific location

Use these tools to find every span relevant to this claim -- supporting,
contradicting, or otherwise bearing on it -- before deciding a verdict.
Search under a few different phrasings/entities if your first search finds
nothing; only conclude missing_evidence after making a genuine effort.

Comparison rules:
- exact match: values must be equal after unit/currency normalization
- tolerance match: within the claim's stated tolerance, if any
- formula check: if the claim is a derived figure (e.g. a ratio or margin),
  check it against the underlying inputs in the vault
- contradiction: an authoritative source states a different value for the
  same entity/period/role

Verdict status must be exactly one of:
- "supported": at least one vault span confirms the claim's value
- "contradicted": an authoritative vault span gives a different value
- "ambiguous": multiple vault spans disagree and neither is clearly authoritative
- "outdated": the vault's best evidence is for a different, superseded period
- "missing_evidence": no vault span addresses this claim at all

For each relevant span found, output an evidence object with:
- doc_id, page (int or null), span_text (verbatim), canonical_value (number or null),
  unit, role (one of "actual", "budget", "forecast", "prior_period", "restated", "other"),
  effective_date (ISO date or null), extractor_confidence (0-1)

Then output a verdict object with:
- status (one of the five values above)
- delta (claim value minus best evidence value, or null)
- confidence (0-1)
- rationale (one or two sentences)
- suggested_action (one sentence on what a human reviewer should do next, or null if status is "supported")

Once you've searched enough to be confident, respond with ONLY a JSON object
as your final answer: {"evidence": [...], "verdict": {...}}.
No prose, no markdown fences.
"""


async def verify_claim_async(
    claim: Claim, audit_id: str, run_id: str, *, model: str | None = None
) -> tuple[list[EvidenceCandidate], Verdict]:
    server = build_server(audit_id, "vault", SERVER_NAME)
    user_prompt = f"CLAIM:\n{claim.model_dump_json(indent=2)}"
    reply = await run_agent(
        SYSTEM_PROMPT,
        user_prompt,
        model=model,
        mcp_servers={SERVER_NAME: server},
        allowed_tools=allowed_tool_names(SERVER_NAME),
    )
    raw = extract_json(reply)

    evidence: list[EvidenceCandidate] = []
    for i, raw_ev in enumerate(raw.get("evidence", []), start=1):
        evidence.append(
            EvidenceCandidate(
                evidence_id=f"{claim.claim_id}/evidence-{i:03d}",
                claim_id=claim.claim_id,
                **raw_ev,
            )
        )

    raw_verdict = raw["verdict"]
    verdict = Verdict(
        claim_id=claim.claim_id,
        status=VerdictStatus(raw_verdict["status"]),
        matched_evidence_ids=[e.evidence_id for e in evidence],
        delta=raw_verdict.get("delta"),
        confidence=raw_verdict["confidence"],
        rationale=raw_verdict["rationale"],
        suggested_action=raw_verdict.get("suggested_action"),
        produced_by_run_id=run_id,
    )
    return evidence, verdict


async def verify_audit_async(audit_id: str) -> list[Verdict]:
    if not db_path(audit_id).exists():
        raise FileNotFoundError(f"{db_path(audit_id)} not found -- run `proofbench index {audit_id}` first")

    claims = _load_claims(audit_id)
    model = resolve_model()

    verdicts: list[Verdict] = []
    for claim in claims:
        run_id = f"{audit_id}/verify-{claim.claim_id.split('/')[-1]}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
        started_at = datetime.now(timezone.utc)

        evidence, verdict = await verify_claim_async(claim, audit_id, run_id, model=model)
        verdicts.append(verdict)

        manifest = RunManifest(
            run_id=run_id,
            audit_id=audit_id,
            agent_role=AgentRole.VERIFIER,
            model=model,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            input_refs=[claim.claim_id],
            output_refs=[verdict.claim_id] + [e.evidence_id for e in evidence],
            prompt=SYSTEM_PROMPT,
            status="succeeded",
        )
        _write_run_manifest(manifest)
        _write_result(audit_id, claim, evidence, verdict)
        if verdict.status != VerdictStatus.SUPPORTED:
            _write_review_card(audit_id, claim, evidence, verdict)

    return verdicts


def verify_audit(audit_id: str) -> list[Verdict]:
    return asyncio.run(verify_audit_async(audit_id))


def _load_claims(audit_id: str) -> list[Claim]:
    claims_dir = REPO_ROOT / "audits" / audit_id / "claims"
    if not claims_dir.exists():
        raise FileNotFoundError(f"{claims_dir} not found -- run `proofbench extract {audit_id}` first")
    claims = []
    for path in sorted(claims_dir.glob("*.json")):
        claims.append(Claim.model_validate_json(path.read_text()))
    return claims


def _write_result(audit_id: str, claim: Claim, evidence: list[EvidenceCandidate], verdict: Verdict) -> None:
    results_dir = REPO_ROOT / "audits" / audit_id / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    suffix = claim.claim_id.split("/")[-1]
    payload = {
        "claim": json.loads(claim.model_dump_json()),
        "evidence": [json.loads(e.model_dump_json()) for e in evidence],
        "verdict": json.loads(verdict.model_dump_json()),
    }
    (results_dir / f"{suffix}.json").write_text(json.dumps(payload, indent=2) + "\n")


def _write_review_card(audit_id: str, claim: Claim, evidence: list[EvidenceCandidate], verdict: Verdict) -> None:
    review_dir = REPO_ROOT / "audits" / audit_id / "review_queue"
    review_dir.mkdir(parents=True, exist_ok=True)
    suffix = claim.claim_id.split("/")[-1]
    payload = {
        "claim": json.loads(claim.model_dump_json()),
        "evidence": [json.loads(e.model_dump_json()) for e in evidence],
        "verdict": json.loads(verdict.model_dump_json()),
        "human_decision": None,
    }
    (review_dir / f"{suffix}.json").write_text(json.dumps(payload, indent=2) + "\n")


def _write_run_manifest(manifest: RunManifest) -> None:
    runs_dir = REPO_ROOT / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    suffix = manifest.run_id.replace("/", "__")
    (runs_dir / f"{suffix}.json").write_text(manifest.model_dump_json(indent=2) + "\n")
