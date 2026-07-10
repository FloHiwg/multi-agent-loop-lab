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

Conflicting sources rule: a claim is not "supported" merely because one
span matches it. If any other vault document states a different value for
the same entity and period, the verdict depends on what the DOCUMENTS
establish about which source wins: an explicit supersession/restatement
makes the older figure "outdated"; the values don't compete only if one
document explicitly states it covers a strictly narrower or different
slice (a single region, segment, or subset) than the claim. A different
measurement methodology or qualification basis for the SAME metric is
still a competing value, not a scope difference -- and a document that
says it is "not ranked" or "not comparable" against another source is
explicitly telling you no authority ordering exists. In all such cases
the verdict is "ambiguous" -- do NOT invent a reconciling explanation
the documents don't contain. Surfacing
unresolved conflicts for human review is the point of this system; a
plausible-sounding reconciliation that hides a conflict is the worst
failure mode.

Verdict status must be exactly one of:
- "supported": at least one vault span confirms the claim's value
- "contradicted": the vault's CURRENT authoritative value differs from the claim
- "ambiguous": multiple vault spans disagree and neither is clearly authoritative
- "outdated": the claim matches a figure that is stale rather than wrong --
  either (a) an older document version or restatement that a newer
  authoritative source explicitly replaces, or (b) an EARLIER PERIOD's value
  asserted as if it were current (e.g. the claim states last quarter's
  actual for a current-quarter metric). A claim that agrees with the stale
  figure is "outdated", NOT "contradicted": contradicted means the claim
  disagrees with the current value, outdated means it agrees with a stale
  one.
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


GRAPH_TOOLS_PROMPT = """\

You additionally have graph tools over the facts index -- prefer them first:
- entity_profile: everything known about one entity in a single call -- every
  value across all documents and periods WITH its verbatim span_text, plus
  mined arithmetic relationships (which rows sum to it, on which columns).
  The name resolves fuzzily ('cash' finds 'Cash and cash equivalents'), so
  try it with the claim's own wording before guessing search terms.
  Its "conflicts" list is computed deterministically: same entity, same
  period and role, different values in different documents. A conflict
  touching the claim's period is not optional context -- apply the
  conflicting sources rule to it: resolve it only with what the documents
  explicitly state, otherwise the verdict is "ambiguous". Its "see_also"
  list names similarly-named entities that may carry the same metric under
  different wording -- profile them too before concluding, they can hide a
  competing value.
  IMPORTANT: "conflicts" covers STRUCTURED TABLE FACTS ONLY. Narrative
  body text (commentary letters, quarterly updates, press-style briefs)
  can state a different value for the same metric and will NOT appear
  there. An empty conflicts list is not corpus-wide clearance: before
  returning "supported", run one search_vault sweep on the metric's name
  and check any prose mentions of it against the claim.
- list_entities: every canonical entity name and its documents -- use it when
  entity_profile finds nothing, to see what vocabulary actually exists.
Fall back to search_vault/search_facts/read_span for narrative text the facts
index doesn't cover.

entity_profile is backed by the same deterministic index as search_facts --
its facts do not need re-confirmation through other tools, and each fact's
span_text is the verbatim span: cite it directly in your evidence objects
instead of calling read_span for it. The efficient pattern is:
entity_profile, then decide. Only search further if the profile leaves the
verdict genuinely open -- e.g. to check other documents for a competing
value before declaring a contradiction, or to rule out narrative text when
the profile finds nothing.
"""


DOSSIER_PROMPT = """\

You are additionally given an EVIDENCE DOSSIER in the user message: every
occurrence of this claim's fact that three gatherers (table facts, prose
mentions, a research sweep) could find. You are now a JUDGE of that
dossier, not a searcher -- the gathering has already been done for you.

Your job:
- Decide which occurrences are actually relevant: same entity, same period,
  same scope as the claim. Not every occurrence in the dossier bears on it.
- A value stated as the organization is "entering", "starting", or "at
  the close of" a subsequent period normally describes the snapshot at the
  boundary just completed. Treat it as evidence for that completed period,
  not a forward-period measurement, unless the source explicitly identifies
  it as a forecast, target, or later remeasurement.
- Decide what the documents establish about authority. authority_rank is
  the audit's configured evidence priority (1 = highest), not an explicit
  reconciliation in either document. It can guide your scrutiny, but it
  cannot by itself turn two different same-fact, same-period values into a
  supported verdict. Require an explicit supersession, restatement, scope
  distinction, or reconciliation in the evidence; otherwise the verdict is
  "ambiguous". An explicit supersession or restatement note in a quote
  outranks a bare priority number.
- Table occurrences carry verbatim span_text -- cite them directly, no
  read_span needed. Prose occurrences carry a verbatim quoted sentence, but
  their metric_phrase label is model output -- sanity-check the sentence
  itself reads as the metric it claims to be before relying on it.
  Researcher occurrences are the least trusted tier: confirm any span you
  plan to cite with read_span before using it.
- cross_source_conflicts lists same-period disagreements across ALL
  sources, prose included. Every conflict touching the claim's period must
  be resolved from what the documents explicitly state, or the verdict is
  "ambiguous" -- do not let an empty table-only conflicts list (if you also
  call entity_profile) stand in for dossier-wide clearance.

Expected pattern: read the dossier, judge it, make 0-3 tool calls only to
confirm something doubtful, then answer.
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
        self.cost_usd = reply.cost_usd


async def verify_claim_async(
    claim: Claim,
    audit_id: str,
    run_id: str,
    *,
    model: str | None = None,
    system_prompt: str | None = None,
    use_aliases: bool = False,
    graph_tools: bool = False,
    rlm: bool = False,
    dossier: bool = False,
) -> tuple[list[EvidenceCandidate], Verdict, AgentReply]:
    # system_prompt/use_aliases/graph_tools/rlm/dossier exist for the eval
    # harness (eval.py), which runs prompt/retrieval variants against
    # gold.yaml -- production callers leave them at their defaults.
    server = build_server(
        audit_id, "vault", SERVER_NAME, use_aliases=use_aliases, include_graph_tools=graph_tools
    )
    mcp_servers = {SERVER_NAME: server}
    allowed_tools = allowed_tool_names(SERVER_NAME, graph_tools=graph_tools)
    sub_costs: list[float] = []
    if rlm:
        from claude_agent_sdk import create_sdk_mcp_server

        from proofbench.rlm import RLM_PROMPT, ask_researcher_tool

        mcp_servers["proofbench_rlm"] = create_sdk_mcp_server(
            "proofbench_rlm",
            tools=[ask_researcher_tool(audit_id, "vault", sub_costs=sub_costs)],
        )
        allowed_tools.append("mcp__proofbench_rlm__ask_researcher")
    if system_prompt is None:
        system_prompt = SYSTEM_PROMPT + (GRAPH_TOOLS_PROMPT if graph_tools else "")
        if rlm:
            system_prompt += RLM_PROMPT
        if dossier:
            system_prompt += DOSSIER_PROMPT
    # The output contract is restated here, not just in the system prompt:
    # after a long tool-use session the system prompt is many turns away,
    # and hard claims (8-12 tool calls) were the ones losing the shape.
    dossier_section = ""
    if dossier:
        from proofbench.dossier import build_dossier

        dossier_data = await build_dossier(claim, audit_id, sub_costs=sub_costs)
        dossier_section = (
            "EVIDENCE DOSSIER -- every known occurrence of this claim's fact, gathered by "
            "table facts, prose mentions, and a research sweep:\n"
            f"{json.dumps(dossier_data)}\n\n"
        )
    user_prompt = (
        f"CLAIM:\n{claim.model_dump_json(indent=2)}\n\n"
        f"{dossier_section}"
        'Reply with ONLY one JSON object of the form {"evidence": [...], "verdict": {...}} '
        "-- never a bare array."
    )
    reply = await run_agent(
        system_prompt,
        user_prompt,
        model=model,
        mcp_servers=mcp_servers,
        allowed_tools=allowed_tools,
    )
    # Researcher sub-sessions are real spend: fold them into the claim's
    # cost before any return or raise, so budget enforcement and reports
    # see the true total.
    if sub_costs:
        reply.cost_usd = (reply.cost_usd or 0.0) + sum(sub_costs)
    raw = extract_json(reply.text)
    if not isinstance(raw, dict) or "evidence" not in raw or "verdict" not in raw:
        raise VerifyClaimError(
            f"expected a JSON object with 'evidence' and 'verdict' keys, "
            f"got {type(raw).__name__}: {str(raw)[:200]!r}",
            reply,
        )

    # Field-level slips (a verdict missing "rationale", a malformed evidence
    # object) must fail as VerifyClaimError so the trace survives into the
    # record -- a bare KeyError here used to discard everything the agent did.
    try:
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
    except VerifyClaimError:
        raise
    except Exception as e:
        raise VerifyClaimError(f"reply parsed but failed validation: {e!r}", reply) from e
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
