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

Claims are verified through manager.run_jobs (bounded concurrency, soft
cost budget, per-claim failure isolation) rather than a plain sequential
loop -- see manager.py.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from proofbench.index_db import db_path
from proofbench.jsonutil import extract_json
from proofbench.llm import AgentReply, resolve_model, run_agent
from proofbench.manager import Job, ManagerReport, run_jobs
from proofbench.models import (
    AgentRole,
    Claim,
    EvidenceCandidate,
    RunManifest,
    Verdict,
    VerdictStatus,
)
from proofbench.runlog import write_run_manifest
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
as your final answer. The top-level value MUST be a JSON *object* with
exactly two keys, "evidence" and "verdict" -- for example:
{"evidence": [...], "verdict": {...}}
Do NOT respond with just the evidence array on its own. No prose, no
markdown fences.
"""


class VerifyClaimError(ValueError):
    """Raised when the agent's final reply doesn't parse into a valid
    verdict. Carries the AgentReply's tool_trace/final_text so the Manager
    can still record what the agent actually did before failing -- without
    this, a failed job's RunManifest would show only an error message and
    lose the entire trace leading up to it.
    """

    def __init__(self, message: str, reply: AgentReply) -> None:
        super().__init__(message)
        self.tool_trace = reply.tool_trace
        self.final_text = reply.text


async def verify_claim_async(
    claim: Claim, audit_id: str, run_id: str, *, model: str | None = None
) -> tuple[list[EvidenceCandidate], Verdict, AgentReply]:
    server = build_server(audit_id, "vault", SERVER_NAME)
    user_prompt = f"CLAIM:\n{claim.model_dump_json(indent=2)}"
    reply = await run_agent(
        SYSTEM_PROMPT,
        user_prompt,
        model=model,
        mcp_servers={SERVER_NAME: server},
        allowed_tools=allowed_tool_names(SERVER_NAME),
    )
    raw = extract_json(reply.text)
    if not isinstance(raw, dict) or "evidence" not in raw or "verdict" not in raw:
        raise VerifyClaimError(
            f"expected a JSON object with 'evidence' and 'verdict' keys, "
            f"got {type(raw).__name__}: {str(raw)[:200]!r}",
            reply,
        )

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
    return evidence, verdict, reply


async def _process_claim(run_id: str, claim: Claim, audit_id: str, model: str) -> float | None:
    """One claim's full job: verify, write result/review card, write its own
    succeeded RunManifest (under the run_id the Manager assigned it),
    return cost_usd. Raises on failure -- the Manager catches that and
    records it, this function doesn't need to.
    """
    started_at = datetime.now(timezone.utc)

    evidence, verdict, reply = await verify_claim_async(claim, audit_id, run_id, model=model)

    write_run_manifest(
        RunManifest(
            run_id=run_id,
            audit_id=audit_id,
            agent_role=AgentRole.VERIFIER,
            model=model,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            input_refs=[claim.claim_id],
            output_refs=[verdict.claim_id] + [e.evidence_id for e in evidence],
            prompt=SYSTEM_PROMPT,
            cost_usd=reply.cost_usd,
            tool_trace=reply.tool_trace,
            final_text=reply.text,
            status="succeeded",
        )
    )
    _write_result(audit_id, claim, evidence, verdict)
    if verdict.status != VerdictStatus.SUPPORTED:
        _write_review_card(audit_id, claim, evidence, verdict)
    return reply.cost_usd


async def verify_audit_async(
    audit_id: str, *, max_concurrency: int = 4, max_budget_usd: float | None = None
) -> ManagerReport:
    if not db_path(audit_id).exists():
        raise FileNotFoundError(f"{db_path(audit_id)} not found -- run `proofbench index {audit_id}` first")

    claims = _load_claims(audit_id)
    model = resolve_model()

    jobs = [
        Job(
            job_id=claim.claim_id,
            audit_id=audit_id,
            agent_role=AgentRole.VERIFIER,
            model=model,
            run_fn=lambda run_id, c=claim: _process_claim(run_id, c, audit_id, model),
        )
        for claim in claims
    ]
    return await run_jobs(jobs, max_concurrency=max_concurrency, max_budget_usd=max_budget_usd)


def verify_audit(audit_id: str, *, max_concurrency: int = 4, max_budget_usd: float | None = None) -> ManagerReport:
    return asyncio.run(verify_audit_async(audit_id, max_concurrency=max_concurrency, max_budget_usd=max_budget_usd))


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
