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
    run_fn: Callable[[], Awaitable[float | None]]
    """Does its own success-path writes and returns the job's cost_usd (or None if unknown)."""


@dataclass
class ManagerReport:
    succeeded: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    skipped_budget: list[str] = field(default_factory=list)
    total_cost_usd: float = 0.0


async def run_jobs(
    jobs: list[Job],
    *,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    max_budget_usd: float | None = None,
) -> ManagerReport:
    report = ManagerReport()
    lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(max_concurrency)

    async def run_one(job: Job) -> None:
        # job_id may already be "<audit_id>/claim-000X" (verification) or a
        # bare id like "master" (extraction) -- take just the last segment
        # so run_ids don't end up with the audit_id duplicated in them.
        job_suffix = job.job_id.rsplit("/", 1)[-1]

        # The budget check must happen *after* acquiring the semaphore, not
        # before: asyncio.gather starts every job's coroutine immediately,
        # so if the check ran first, all of them would race to check the
        # budget at cost=0 before any sibling had a chance to update it --
        # the cap would never actually trigger under concurrency > 1.
        # Checking post-semaphore means a job only checks once it's
        # actually its turn to run, by which point earlier jobs (up to
        # max_concurrency of them) have finished and reported their cost.
        async with semaphore:
            async with lock:
                if max_budget_usd is not None and report.total_cost_usd >= max_budget_usd:
                    report.skipped_budget.append(job.job_id)
                    write_run_manifest(
                        RunManifest(
                            run_id=f"{job.audit_id}/skipped-{job_suffix}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}",
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
                cost_usd = await job.run_fn()
            except Exception as e:
                write_run_manifest(
                    RunManifest(
                        run_id=f"{job.audit_id}/failed-{job_suffix}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}",
                        audit_id=job.audit_id,
                        agent_role=job.agent_role,
                        model=job.model,
                        started_at=started_at,
                        finished_at=datetime.now(timezone.utc),
                        input_refs=[job.job_id],
                        status="failed",
                        error=str(e),
                    )
                )
                report.failed[job.job_id] = str(e)
                return

            async with lock:
                if cost_usd is not None:
                    report.total_cost_usd += cost_usd
                report.succeeded.append(job.job_id)

    await asyncio.gather(*(run_one(job) for job in jobs))
    return report
