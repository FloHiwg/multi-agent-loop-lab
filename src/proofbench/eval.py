"""Eval harness: run Verifier variants against gold.yaml and compare
cost vs accuracy, instead of eyeballing single runs.

Motivation (see ARCHITECTURE.md): runs are noisy -- the intermittent
JSON-shape failure alone makes any single run a bad basis for judging a
prompt or retrieval change. So changes to the Verifier's context land here
first, as named variants scored against the audit's gold.yaml fixture.

Isolation rules:
- Output goes to runs/experiments/<experiment_id>/<variant>/ only. The
  harness never writes to audits/<id>/results/, review_queue/, or claims/,
  and never writes RunManifests into runs/ (the flat *.json glob the
  workbench reads) -- experiments must not masquerade as real audit runs.
- Claims are read from audits/<id>/claims/ (the already-extracted set), so
  every variant verifies the identical claim list.

Scoring: a claim is "correct" when the Verifier's verdict status equals
gold.yaml's expected_status. Failures (unparseable replies) count as
incorrect -- a variant that fails more is worse, not excluded.

Current variants compare the production baseline with pull-based graph,
researcher, and prepared-dossier retrieval strategies.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from proofbench.index_db import db_path
from proofbench.llm import resolve_model
from proofbench.models import Claim
from proofbench.preflight import check_credentials_async
from proofbench.verification import (
    DOSSIER_PROMPT,
    GRAPH_TOOLS_PROMPT,
    SYSTEM_PROMPT,
    VerifyClaimError,
    _load_claims,
    verify_claim_async,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENTS_DIR = REPO_ROOT / "runs" / "experiments"


@dataclass(frozen=True)
class Variant:
    name: str
    graph_tools: bool = False
    rlm: bool = False
    dossier: bool = False


VARIANTS: dict[str, Variant] = {
    "baseline": Variant("baseline"),
    # pull-based: the agent asks the graph for vocabulary/values when it
    # needs them (list_entities, entity_profile) -- the direction chosen
    # over prompt injection
    "graph": Variant("graph", graph_tools=True),
    # graph tools plus a cheap researcher sub-agent for exhaustive sweeps
    # (absence proofs, competing-value scans) -- see rlm.py
    "rlm": Variant("rlm", graph_tools=True, rlm=True),
    # brain/hands split: a prepared evidence dossier (table + prose +
    # researcher occurrences) is assembled and handed to the Verifier as
    # judge, instead of the Verifier gathering it itself -- see dossier.py
    "dossier": Variant("dossier", graph_tools=True, dossier=True),
}


def load_gold(audit_id: str) -> dict[str, str]:
    """claim_id -> expected_status from the audit's gold.yaml fixture."""
    gold_path = REPO_ROOT / "audits" / audit_id / "gold.yaml"
    if not gold_path.exists():
        raise FileNotFoundError(f"{gold_path} not found -- the eval harness needs a gold fixture to score against")
    gold = yaml.safe_load(gold_path.read_text())
    return {c["claim_id"]: c["expected_status"] for c in gold["claims"]}


class _Budget:
    """Soft cap shared across the whole experiment: once total spend reaches
    the cap, no new claim is started; in-flight claims finish."""

    def __init__(self, max_usd: float | None) -> None:
        self.max_usd = max_usd
        self.total_usd = 0.0
        self._lock = asyncio.Lock()

    async def add(self, cost_usd: float | None) -> None:
        async with self._lock:
            if cost_usd is not None:
                self.total_usd += cost_usd

    async def exhausted(self) -> bool:
        async with self._lock:
            return self.max_usd is not None and self.total_usd >= self.max_usd


async def _eval_claim(
    variant: Variant,
    claim: Claim,
    audit_id: str,
    model: str,
    system_prompt: str,
    budget: _Budget,
    semaphore: asyncio.Semaphore,
    gold: dict[str, str],
    variant_dir: Path,
) -> dict:
    # Checkpointing: a record file already on disk means a prior run of
    # this same --experiment-id already paid for and completed this claim
    # -- reuse it instead of re-paying, so a rerun after a mid-variant
    # crash resumes rather than starting over.
    suffix = claim.claim_id.split("/")[-1]
    record_path = variant_dir / f"{suffix}.json"
    dossier_path = variant_dir / f"{suffix}.dossier.json"
    if record_path.exists():
        return json.loads(record_path.read_text())

    expected = gold.get(claim.claim_id)
    record: dict = {
        "claim_id": claim.claim_id,
        "variant": variant.name,
        "expected_status": expected,
        "status": None,
        "correct": False,
        "tool_calls": None,
        "cost_usd": None,
        "error": None,
    }

    async with semaphore:
        if await budget.exhausted():
            record["error"] = "skipped: experiment budget exhausted"
            return record
        dossier_out: dict | None = {} if variant.dossier else None
        try:
            evidence, verdict, reply = await verify_claim_async(
                claim,
                audit_id,
                run_id=f"eval/{variant.name}/{claim.claim_id}",
                model=model,
                system_prompt=system_prompt,
                graph_tools=variant.graph_tools,
                rlm=variant.rlm,
                dossier=variant.dossier,
                dossier_out=dossier_out,
            )
        except VerifyClaimError as e:
            record["error"] = str(e)
            record["tool_calls"] = len(e.tool_trace)
            record["cost_usd"] = e.cost_usd
            record["tool_trace"] = e.tool_trace
            record["final_text"] = e.final_text
            await budget.add(e.cost_usd)
            record_path.write_text(json.dumps(record, indent=2) + "\n")
            return record
        except Exception as e:
            record["error"] = str(e)
            record_path.write_text(json.dumps(record, indent=2) + "\n")
            return record

    await budget.add(reply.cost_usd)
    record["status"] = verdict.status.value
    record["correct"] = expected is not None and verdict.status.value == expected
    record["tool_calls"] = len(reply.tool_trace)
    record["cost_usd"] = reply.cost_usd
    record["verdict"] = json.loads(verdict.model_dump_json())
    record["evidence"] = [json.loads(ev.model_dump_json()) for ev in evidence]
    record["tool_trace"] = reply.tool_trace
    record["final_text"] = reply.text
    record_path.write_text(json.dumps(record, indent=2) + "\n")
    if dossier_out:
        dossier_path.write_text(json.dumps(dossier_out["dossier"], indent=2) + "\n")
    return record


# Judge-replay stage (MUL-10): a dossier already gathered by a prior
# `dossier`-variant run is fed straight to verify_claim_async as
# dossier_data, skipping re-gathering (real API spend) so judge prompt/logic
# changes can be tested in isolation from gathering drift, and repeated to
# get verdict-consistency data a single run can't provide.


def _strip_conflicts(dossier: dict) -> dict:
    out = copy.deepcopy(dossier)
    out["cross_source_conflicts"] = []
    return out


def _strip_authority_rank(dossier: dict) -> dict:
    out = copy.deepcopy(dossier)
    for occ in out.get("occurrences", []):
        occ.pop("authority_rank", None)
    return out


def _drop_researcher(dossier: dict) -> dict:
    out = copy.deepcopy(dossier)
    out["occurrences"] = [occ for occ in out.get("occurrences", []) if occ.get("source") != "researcher"]
    return out


ABLATIONS: dict[str, Callable[[dict], dict]] = {
    "strip_conflicts": _strip_conflicts,
    "strip_authority_rank": _strip_authority_rank,
    "drop_researcher": _drop_researcher,
}


def _load_cached_dossier(dossiers_from: str, variant_name: str, suffix: str) -> dict:
    path = EXPERIMENTS_DIR / dossiers_from / variant_name / f"{suffix}.dossier.json"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found -- rerun experiment {dossiers_from!r} with the '{variant_name}' "
            "variant first (only that variant persists dossiers) before replaying from it"
        )
    return json.loads(path.read_text())


async def _judge_claim(
    claim: Claim,
    audit_id: str,
    model: str,
    system_prompt: str,
    budget: _Budget,
    semaphore: asyncio.Semaphore,
    gold: dict[str, str],
    variant_dir: Path,
    dossiers_from: str,
    dossiers_variant: str,
    ablation: str | None,
    repeat: int,
) -> dict:
    suffix = claim.claim_id.split("/")[-1]
    # Repeat-suffixed filenames even for repeats=1: judge and gather stages
    # can in principle share an experiment_id (nothing here forbids it), so
    # this naming can never collide with the gather stage's plain
    # claim-XXXX.json/dossier.json records.
    record_path = variant_dir / f"{suffix}.repeat-{repeat:02d}.json"
    if record_path.exists():
        return json.loads(record_path.read_text())

    expected = gold.get(claim.claim_id)
    record: dict = {
        "claim_id": claim.claim_id,
        "repeat": repeat,
        "expected_status": expected,
        "status": None,
        "correct": False,
        "tool_calls": None,
        "cost_usd": None,
        "error": None,
    }

    dossier_data = _load_cached_dossier(dossiers_from, dossiers_variant, suffix)
    if ablation is not None:
        if ablation not in ABLATIONS:
            raise ValueError(f"unknown ablation {ablation!r} (available: {sorted(ABLATIONS)})")
        dossier_data = ABLATIONS[ablation](dossier_data)

    async with semaphore:
        if await budget.exhausted():
            # Not written to disk (matching _eval_claim): budget exhaustion
            # is transient, unlike a real result -- a rerun with a higher
            # cap should retry this claim rather than replay "skipped"
            # forever.
            record["error"] = "skipped: experiment budget exhausted"
            return record
        try:
            evidence, verdict, reply = await verify_claim_async(
                claim,
                audit_id,
                run_id=f"eval-judge/{dossiers_from}/repeat-{repeat:02d}/{claim.claim_id}",
                model=model,
                system_prompt=system_prompt,
                graph_tools=False,
                rlm=False,
                dossier=True,
                dossier_data=dossier_data,
            )
        except VerifyClaimError as e:
            record["error"] = str(e)
            record["tool_calls"] = len(e.tool_trace)
            record["cost_usd"] = e.cost_usd
            record["tool_trace"] = e.tool_trace
            record["final_text"] = e.final_text
            await budget.add(e.cost_usd)
            record_path.write_text(json.dumps(record, indent=2) + "\n")
            return record
        except Exception as e:
            record["error"] = str(e)
            record_path.write_text(json.dumps(record, indent=2) + "\n")
            return record

    await budget.add(reply.cost_usd)
    record["status"] = verdict.status.value
    record["correct"] = expected is not None and verdict.status.value == expected
    record["tool_calls"] = len(reply.tool_trace)
    record["cost_usd"] = reply.cost_usd
    record["verdict"] = json.loads(verdict.model_dump_json())
    record["evidence"] = [json.loads(ev.model_dump_json()) for ev in evidence]
    record["tool_trace"] = reply.tool_trace
    record["final_text"] = reply.text
    record_path.write_text(json.dumps(record, indent=2) + "\n")
    return record


def _summarize_judge(records_by_repeat: list[list[dict]], claim_ids: list[str]) -> dict:
    per_repeat = [_summarize(Variant("judge"), records) for records in records_by_repeat]
    # Verdict consistency: for each claim, do all repeats that produced a
    # status agree? Claims with fewer than 2 statuses recorded (e.g. every
    # repeat errored) are excluded from the denominator -- there's nothing
    # to be consistent or inconsistent about.
    statuses_by_claim: dict[str, list[str]] = {cid: [] for cid in claim_ids}
    for records in records_by_repeat:
        for r in records:
            if r["status"] is not None:
                statuses_by_claim[r["claim_id"]].append(r["status"])
    considered = {cid: statuses for cid, statuses in statuses_by_claim.items() if len(statuses) >= 2}
    unanimous = sum(1 for statuses in considered.values() if len(set(statuses)) == 1)
    return {
        "per_repeat": per_repeat,
        "n_claims": len(claim_ids),
        "repeats": len(records_by_repeat),
        "n_claims_with_multiple_repeats": len(considered),
        "unanimous_claims": unanimous,
        "verdict_consistency": unanimous / len(considered) if considered else None,
    }


async def run_judge_eval_async(
    audit_id: str,
    dossiers_from: str,
    *,
    variant_name: str = "dossier",
    ablation: str | None = None,
    repeats: int = 1,
    max_concurrency: int = 2,
    max_budget_usd: float | None = None,
    experiment_id: str | None = None,
    model: str | None = None,
    claim_suffixes: list[str] | None = None,
) -> dict:
    await check_credentials_async()
    if not db_path(audit_id).exists():
        raise FileNotFoundError(f"{db_path(audit_id)} not found -- run `proofbench index {audit_id}` first")
    if ablation is not None and ablation not in ABLATIONS:
        raise ValueError(f"unknown ablation {ablation!r} (available: {sorted(ABLATIONS)})")

    gold = load_gold(audit_id)
    claims = _load_claims(audit_id)
    if claim_suffixes is not None:
        wanted = set(claim_suffixes)
        claims = [c for c in claims if c.claim_id.split("/")[-1] in wanted]
        missing = wanted - {c.claim_id.split("/")[-1] for c in claims}
        if missing:
            raise ValueError(f"no such claims: {sorted(missing)}")

    resolved_model = resolve_model(model)
    experiment_id = experiment_id or f"exp-judge-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    experiment_dir = EXPERIMENTS_DIR / experiment_id
    variant_dir = experiment_dir / "judge"
    variant_dir.mkdir(parents=True, exist_ok=True)
    budget = _Budget(max_budget_usd)

    system_prompt = SYSTEM_PROMPT + DOSSIER_PROMPT

    semaphore = asyncio.Semaphore(max_concurrency)
    records_by_repeat: list[list[dict]] = []
    for repeat in range(1, repeats + 1):
        records = await asyncio.gather(
            *(
                _judge_claim(
                    claim,
                    audit_id,
                    resolved_model,
                    system_prompt,
                    budget,
                    semaphore,
                    gold,
                    variant_dir,
                    dossiers_from,
                    variant_name,
                    ablation,
                    repeat,
                )
                for claim in claims
            )
        )
        records_by_repeat.append(list(records))

    claim_ids = [c.claim_id for c in claims]
    summary = _summarize_judge(records_by_repeat, claim_ids)
    (variant_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    report = {
        "experiment_id": experiment_id,
        "audit_id": audit_id,
        "stage": "judge",
        "dossiers_from": dossiers_from,
        "dossiers_variant": variant_name,
        "ablation": ablation,
        "repeats": repeats,
        "model": resolved_model,
        "max_concurrency": max_concurrency,
        "max_budget_usd": max_budget_usd,
        "total_cost_usd": budget.total_usd,
        "summary": summary,
    }
    (experiment_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def run_judge_eval(
    audit_id: str,
    dossiers_from: str,
    *,
    variant_name: str = "dossier",
    ablation: str | None = None,
    repeats: int = 1,
    max_concurrency: int = 2,
    max_budget_usd: float | None = None,
    experiment_id: str | None = None,
    model: str | None = None,
    claim_suffixes: list[str] | None = None,
) -> dict:
    return asyncio.run(
        run_judge_eval_async(
            audit_id,
            dossiers_from,
            variant_name=variant_name,
            ablation=ablation,
            repeats=repeats,
            max_concurrency=max_concurrency,
            max_budget_usd=max_budget_usd,
            experiment_id=experiment_id,
            model=model,
            claim_suffixes=claim_suffixes,
        )
    )


def _summarize(variant: Variant, records: list[dict]) -> dict:
    scored = [r for r in records if r["error"] is None or r["tool_calls"] is not None]
    costs = [r["cost_usd"] for r in records if r["cost_usd"] is not None]
    tool_calls = [r["tool_calls"] for r in records if r["tool_calls"] is not None]
    n = len(records)
    correct = sum(1 for r in records if r["correct"])
    return {
        "variant": variant.name,
        "n_claims": n,
        "correct": correct,
        "accuracy": correct / n if n else None,
        "failures": sum(1 for r in records if r["error"] is not None),
        "avg_tool_calls": sum(tool_calls) / len(tool_calls) if tool_calls else None,
        "avg_cost_usd": sum(costs) / len(costs) if costs else None,
        "total_cost_usd": sum(costs) if costs else 0.0,
        "n_attempted": len(scored),
    }


async def run_eval_async(
    audit_id: str,
    variant_names: list[str],
    *,
    max_concurrency: int = 2,
    max_budget_usd: float | None = None,
    experiment_id: str | None = None,
    model: str | None = None,
    claim_suffixes: list[str] | None = None,
) -> dict:
    await check_credentials_async()
    if not db_path(audit_id).exists():
        raise FileNotFoundError(f"{db_path(audit_id)} not found -- run `proofbench index {audit_id}` first")

    unknown = [name for name in variant_names if name not in VARIANTS]
    if unknown:
        raise ValueError(f"unknown variants: {unknown} (available: {sorted(VARIANTS)})")

    gold = load_gold(audit_id)
    claims = _load_claims(audit_id)
    # claim_suffixes subsets the run (e.g. ["claim-0004"]) for cheap smoke
    # tests -- accuracy over a subset is directional only, not comparable
    # to full-batch numbers.
    if claim_suffixes is not None:
        wanted = set(claim_suffixes)
        claims = [c for c in claims if c.claim_id.split("/")[-1] in wanted]
        missing = wanted - {c.claim_id.split("/")[-1] for c in claims}
        if missing:
            raise ValueError(f"no such claims: {sorted(missing)}")
    resolved_model = resolve_model(model)
    experiment_id = experiment_id or f"exp-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    experiment_dir = EXPERIMENTS_DIR / experiment_id
    budget = _Budget(max_budget_usd)

    summaries: list[dict] = []
    # Variants run sequentially (claims within one run concurrently) so a
    # noisy variant can't starve its siblings of budget mid-flight.
    for name in variant_names:
        variant = VARIANTS[name]
        # Skip a whole variant once the budget is gone.
        if await budget.exhausted():
            summaries.append(
                {
                    "variant": variant.name,
                    "skipped": "experiment budget exhausted before this variant started",
                    "n_claims": 0,
                    "correct": 0,
                    "accuracy": None,
                    "failures": 0,
                    "avg_tool_calls": None,
                    "avg_cost_usd": None,
                    "total_cost_usd": 0.0,
                    "n_attempted": 0,
                }
            )
            continue

        system_prompt = SYSTEM_PROMPT
        if variant.graph_tools:
            system_prompt += GRAPH_TOOLS_PROMPT
        if variant.rlm:
            from proofbench.rlm import RLM_PROMPT

            system_prompt += RLM_PROMPT
        if variant.dossier:
            system_prompt += DOSSIER_PROMPT

        # Created before the gather (not after) so per-claim records can be
        # checkpointed to disk as each claim finishes, rather than only
        # after the whole variant completes -- a mid-variant crash no
        # longer loses paid work on a rerun with the same --experiment-id.
        variant_dir = experiment_dir / variant.name
        variant_dir.mkdir(parents=True, exist_ok=True)

        semaphore = asyncio.Semaphore(max_concurrency)
        records = await asyncio.gather(
            *(
                _eval_claim(
                    variant, claim, audit_id, resolved_model, system_prompt, budget, semaphore, gold, variant_dir
                )
                for claim in claims
            )
        )
        records = list(records)

        summary = _summarize(variant, records)
        (variant_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        summaries.append(summary)

    report = {
        "experiment_id": experiment_id,
        "audit_id": audit_id,
        "model": resolved_model,
        "max_concurrency": max_concurrency,
        "max_budget_usd": max_budget_usd,
        "started_variants": variant_names,
        "total_cost_usd": budget.total_usd,
        "summaries": summaries,
    }
    (experiment_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def run_eval(
    audit_id: str,
    variant_names: list[str],
    *,
    max_concurrency: int = 2,
    max_budget_usd: float | None = None,
    experiment_id: str | None = None,
    model: str | None = None,
    claim_suffixes: list[str] | None = None,
) -> dict:
    return asyncio.run(
        run_eval_async(
            audit_id,
            variant_names,
            max_concurrency=max_concurrency,
            max_budget_usd=max_budget_usd,
            experiment_id=experiment_id,
            model=model,
            claim_suffixes=claim_suffixes,
        )
    )
