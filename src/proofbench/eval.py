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

The first experiment's variants target the measured vocabulary-mismatch
waste (avg 4.8 tool calls/claim, blind entity-name guessing):
- baseline: the production prompt, unchanged.
- catalog: + facts catalog (entity/attribute names) in the system prompt.
- catalog_aliases: catalog + LLM-generated entity aliases in search_facts
  (one enrichment call per audit, amortized -- see catalog.py).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from proofbench.catalog import alias_count, catalog_prompt_section, enrich_aliases_async
from proofbench.index_db import db_path
from proofbench.llm import resolve_model
from proofbench.models import Claim
from proofbench.verification import (
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
    inject_catalog: bool = False
    use_aliases: bool = False


VARIANTS: dict[str, Variant] = {
    "baseline": Variant("baseline"),
    "catalog": Variant("catalog", inject_catalog=True),
    "catalog_aliases": Variant("catalog_aliases", inject_catalog=True, use_aliases=True),
}


def load_gold(audit_id: str) -> dict[str, str]:
    """claim_id -> expected_status from the audit's gold.yaml fixture."""
    gold_path = REPO_ROOT / "audits" / audit_id / "gold.yaml"
    if not gold_path.exists():
        raise FileNotFoundError(f"{gold_path} not found -- the eval harness needs a gold fixture to score against")
    gold = yaml.safe_load(gold_path.read_text())
    return {c["claim_id"]: c["expected_status"] for c in gold["claims"]}


class _Budget:
    """Soft cap shared across the whole experiment (all variants plus the
    alias-enrichment call): once total spend reaches the cap, no new claim
    is started; in-flight claims finish. Same semantics as the Manager's."""

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
) -> dict:
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
        try:
            evidence, verdict, reply = await verify_claim_async(
                claim,
                audit_id,
                run_id=f"eval/{variant.name}/{claim.claim_id}",
                model=model,
                system_prompt=system_prompt,
                use_aliases=variant.use_aliases,
            )
        except VerifyClaimError as e:
            record["error"] = str(e)
            record["tool_calls"] = len(e.tool_trace)
            record["cost_usd"] = e.cost_usd
            record["tool_trace"] = e.tool_trace
            record["final_text"] = e.final_text
            await budget.add(e.cost_usd)
            return record
        except Exception as e:
            record["error"] = str(e)
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
    return record


def _summarize(variant: Variant, records: list[dict], enrichment_cost_usd: float | None) -> dict:
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
        "enrichment_cost_usd": enrichment_cost_usd,
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
) -> dict:
    if not db_path(audit_id).exists():
        raise FileNotFoundError(f"{db_path(audit_id)} not found -- run `proofbench index {audit_id}` first")

    unknown = [name for name in variant_names if name not in VARIANTS]
    if unknown:
        raise ValueError(f"unknown variants: {unknown} (available: {sorted(VARIANTS)})")

    gold = load_gold(audit_id)
    claims = _load_claims(audit_id)
    resolved_model = resolve_model(model)
    experiment_id = experiment_id or f"exp-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    experiment_dir = EXPERIMENTS_DIR / experiment_id
    budget = _Budget(max_budget_usd)

    summaries: list[dict] = []
    # Variants run sequentially (claims within one run concurrently) so a
    # noisy variant can't starve its siblings of budget mid-flight.
    for name in variant_names:
        variant = VARIANTS[name]
        enrichment_cost_usd: float | None = None
        if variant.use_aliases and alias_count(audit_id) == 0:
            written, reply = await enrich_aliases_async(audit_id, model=resolved_model)
            enrichment_cost_usd = reply.cost_usd
            await budget.add(reply.cost_usd)

        system_prompt = SYSTEM_PROMPT
        if variant.inject_catalog:
            system_prompt += catalog_prompt_section(audit_id)

        semaphore = asyncio.Semaphore(max_concurrency)
        records = await asyncio.gather(
            *(
                _eval_claim(variant, claim, audit_id, resolved_model, system_prompt, budget, semaphore, gold)
                for claim in claims
            )
        )
        records = list(records)

        variant_dir = experiment_dir / variant.name
        variant_dir.mkdir(parents=True, exist_ok=True)
        for record in records:
            suffix = record["claim_id"].split("/")[-1]
            (variant_dir / f"{suffix}.json").write_text(json.dumps(record, indent=2) + "\n")

        summary = _summarize(variant, records, enrichment_cost_usd)
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
) -> dict:
    return asyncio.run(
        run_eval_async(
            audit_id,
            variant_names,
            max_concurrency=max_concurrency,
            max_budget_usd=max_budget_usd,
            experiment_id=experiment_id,
            model=model,
        )
    )
