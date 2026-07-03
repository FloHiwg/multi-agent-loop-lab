"""Vault Retriever + Verifier, combined for v1.

CONCEPT.md splits retrieval and verification into separate agents, but with
a vault of a handful of documents, full-text search adds no recall over
just handing the Verifier the whole corpus (see CONCEPT.md open question
#3). Split this into a real retrieval step once the vault is too large to
fit in one prompt.

The Verifier never decides alone in the product sense -- everything short
of `supported` lands in review_queue/ for a human.
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
from proofbench.models import (
    AgentRole,
    Claim,
    EvidenceCandidate,
    RunManifest,
    Verdict,
    VerdictStatus,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

SYSTEM_PROMPT = """\
You are the Verifier agent in Proofbench, an audit workbench.

You are given one numeric claim from a master document, and the full text
of the vault (all basis/support documents). Find every span in the vault
that is relevant to this claim -- supporting, contradicting, or otherwise
bearing on it -- and decide a verdict.

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

Respond with ONLY a JSON object: {"evidence": [...], "verdict": {...}}.
No prose, no markdown fences.
"""


def _vault_corpus_text(config) -> str:
    from proofbench.models import DocumentKind

    parts = [render_document(doc.doc_id) for doc in config.documents if doc.kind == DocumentKind.VAULT]
    return "\n\n".join(parts)


async def verify_claim_async(
    claim: Claim, vault_text: str, run_id: str, *, model: str | None = None
) -> tuple[list[EvidenceCandidate], Verdict]:
    user_prompt = f"CLAIM:\n{claim.model_dump_json(indent=2)}\n\nVAULT:\n{vault_text}"
    reply = await run_agent(SYSTEM_PROMPT, user_prompt, model=model)
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
    config = load_audit_config(audit_id)
    vault_text = _vault_corpus_text(config)
    claims = _load_claims(audit_id)
    model = resolve_model()

    verdicts: list[Verdict] = []
    for claim in claims:
        run_id = f"{audit_id}/verify-{claim.claim_id.split('/')[-1]}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
        started_at = datetime.now(timezone.utc)

        evidence, verdict = await verify_claim_async(claim, vault_text, run_id, model=model)
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
