"""Manager: schedules bounded worker jobs, enforces budgets, isolates failures.

Per CONCEPT.md §6, the Manager's job is orchestration only -- it never
judges evidence or decides verdicts. Concretely, that means:

- **Bounded concurrency**: run up to `max_concurrency` jobs (claim
  verifications, currently) at once instead of one Python `for` loop at a
  time.
- **Failure isolation**: if one job's LLM call raises or its reply fails
  schema validation, that's recorded as a failed RunManifest and the run
  continues -- it no longer takes down every other claim in the batch.
- **A soft cost budget**: once the running total (from each job's actual
  reported cost, not an estimate) reaches `max_budget_usd`, no new jobs are
  started; already-running jobs finish. This is approximate under
  concurrency (a burst of jobs can start before the total updates) -- it's
  a backstop against a runaway batch, not a precise cutoff. `run_agent`'s
  own `max_budget_usd` is a tighter, per-call version of the same idea.

Each job is responsible for its own success-path writes (results,
review cards, its own succeeded RunManifest) -- the Manager only knows
enough to schedule it and to record a generic failure if it raises.

The Manager also writes exactly one RunManifest for itself
(`agent_role=AgentRole.MANAGER`) per `run_jobs()` call, once every job has
finished: the concurrency/budget it was given, and the outcome (and
run_id, for linking) of every job it scheduled. Without this, there was no
single record of what the orchestrator actually decided -- only many
individual job manifests a person would have to cross-reference by hand.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable

from proofbench.models import AgentRole, RunManifest
from proofbench.runlog import write_run_manifest

DEFAULT_MAX_CONCURRENCY = 4


@dataclass
class Job:
    job_id: str
    audit_id: str
    agent_role: AgentRole
    model: str
    run_fn: Callable[[str], Awaitable[float | None]]
    """Takes the run_id the Manager assigned this job, does its own
    success-path writes (using that run_id), and returns the job's
    cost_usd (or None if unknown)."""


@dataclass
class ManagerReport:
    succeeded: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    skipped_budget: list[str] = field(default_factory=list)
    total_cost_usd: float = 0.0


def _job_run_id(job: Job) -> str:
    # job_id may already be "<audit_id>/claim-000X" (verification) or a
    # bare id like "master" (extraction) -- take just the last segment so
    # run_ids don't end up with the audit_id duplicated in them.
    job_suffix = job.job_id.rsplit("/", 1)[-1]
    return f"{job.audit_id}/{job.agent_role.value}-{job_suffix}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"


async def run_jobs(
    jobs: list[Job],
    *,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    max_budget_usd: float | None = None,
) -> ManagerReport:
    if not jobs:
        return ManagerReport()

    report = ManagerReport()
    lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(max_concurrency)
    job_outcomes: list[dict] = []
    manager_started_at = datetime.now(timezone.utc)

    async def run_one(job: Job) -> None:
        run_id = _job_run_id(job)

        async with semaphore:
            async with lock:
                if max_budget_usd is not None and report.total_cost_usd >= max_budget_usd:
                    report.skipped_budget.append(job.job_id)
                    job_outcomes.append({"job_id": job.job_id, "run_id": run_id, "status": "skipped_budget", "cost_usd": None})
                    write_run_manifest(
                        RunManifest(
                            run_id=run_id,
                            audit_id=job.audit_id,
                            agent_role=job.agent_role,
                            model=job.model,
                            started_at=datetime.now(timezone.utc),
                            finished_at=datetime.now(timezone.utc),
                            input_refs=[job.job_id],
                            status="skipped_budget",
                            error=f"budget exhausted: total_cost_usd={report.total_cost_usd} >= max_budget_usd={max_budget_usd}",
                        )
                    )
                    return

            started_at = datetime.now(timezone.utc)
            try:
                cost_usd = await job.run_fn(run_id)
            except Exception as e:
                async with lock:
                    report.failed[job.job_id] = str(e)
                    job_outcomes.append({"job_id": job.job_id, "run_id": run_id, "status": "failed", "cost_usd": None})
                # Some exceptions (e.g. verification.py's VerifyClaimError)
                # carry the agent's tool_trace/final_text from before it
                # failed -- duck-typed here so the Manager stays agnostic
                # of which agent raised it, while the failure record still
                # shows what the agent actually did, not just the error.
                write_run_manifest(
                    RunManifest(
                        run_id=run_id,
                        audit_id=job.audit_id,
                        agent_role=job.agent_role,
                        model=job.model,
                        started_at=started_at,
                        finished_at=datetime.now(timezone.utc),
                        input_refs=[job.job_id],
                        status="failed",
                        error=str(e),
                        tool_trace=getattr(e, "tool_trace", []),
                        final_text=getattr(e, "final_text", None),
                    )
                )
                return

            async with lock:
                if cost_usd is not None:
                    report.total_cost_usd += cost_usd
                report.succeeded.append(job.job_id)
                job_outcomes.append({"job_id": job.job_id, "run_id": run_id, "status": "succeeded", "cost_usd": cost_usd})

    await asyncio.gather(*(run_one(job) for job in jobs))

    # job_outcomes fills in as jobs complete, in whatever order that
    # happens to be under concurrency -- reorder to match the original job
    # list so the Manager's own record reads in a stable, predictable order.
    outcome_by_job_id = {o["job_id"]: o for o in job_outcomes}
    ordered_outcomes = [outcome_by_job_id[job.job_id] for job in jobs if job.job_id in outcome_by_job_id]

    write_run_manifest(
        RunManifest(
            run_id=f"{jobs[0].audit_id}/manage-{jobs[0].agent_role.value}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}",
            audit_id=jobs[0].audit_id,
            agent_role=AgentRole.MANAGER,
            model=jobs[0].model,
            started_at=manager_started_at,
            finished_at=datetime.now(timezone.utc),
            input_refs=[job.job_id for job in jobs],
            output_refs=list(report.succeeded),
            cost_usd=report.total_cost_usd,
            max_concurrency=max_concurrency,
            max_budget_usd=max_budget_usd,
            job_outcomes=ordered_outcomes,
            status="succeeded",
        )
    )

    return report
