"""Proofbench CLI."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from dotenv import load_dotenv

from proofbench import models
from proofbench.extraction import extract_claims
from proofbench.index_db import build_index
from proofbench.ingest import ingest_audit
from proofbench.verification import verify_audit

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS_DIR = REPO_ROOT / "schemas"

load_dotenv(REPO_ROOT / ".env")

app = typer.Typer(add_completion=False, help="Local claim-evidence audit workbench.")
schemas_app = typer.Typer(help="Manage exported JSON Schema files.")
app.add_typer(schemas_app, name="schemas")

EXPORTED_MODELS = {
    "claim": models.Claim,
    "evidence_candidate": models.EvidenceCandidate,
    "verdict": models.Verdict,
    "run_manifest": models.RunManifest,
    "audit_config": models.AuditConfig,
}


@schemas_app.command("export")
def schemas_export() -> None:
    """Regenerate schemas/*.schema.json from the Pydantic models in models.py."""
    SCHEMAS_DIR.mkdir(parents=True, exist_ok=True)
    for name, model in EXPORTED_MODELS.items():
        out_path = SCHEMAS_DIR / f"{name}.schema.json"
        out_path.write_text(json.dumps(model.model_json_schema(), indent=2) + "\n")
        typer.echo(f"wrote {out_path.relative_to(REPO_ROOT)}")


@app.command()
def new(audit_id: str) -> None:
    """Scaffold a new, empty audit folder under audits/<audit-id>/."""
    audit_dir = REPO_ROOT / "audits" / audit_id
    for sub in ("master", "claims", "results", "review_queue"):
        (audit_dir / sub).mkdir(parents=True, exist_ok=True)
    typer.echo(f"created {audit_dir.relative_to(REPO_ROOT)}")


@app.command()
def init(audit_id: str) -> None:
    """Parse this audit's registered documents (master + vault) into index/parsed/."""
    written = ingest_audit(audit_id)
    for path in written:
        typer.echo(f"wrote {path.relative_to(REPO_ROOT)}")


@app.command()
def index(audit_id: str) -> None:
    """Build the searchable index (full-text spans + entity/attribute facts) from index/parsed/."""
    path = build_index(audit_id)
    typer.echo(f"wrote {path.relative_to(REPO_ROOT)}")


@app.command()
def extract(
    audit_id: str,
    max_budget_usd: float | None = typer.Option(None, help="Abort before the extraction call if this would be exceeded."),
) -> None:
    """Run the Claim Extractor over the master document, writing audits/<id>/claims/."""
    claims = extract_claims(audit_id, max_budget_usd=max_budget_usd)
    typer.echo(f"extracted {len(claims)} claims into audits/{audit_id}/claims/")


@app.command()
def verify(
    audit_id: str,
    max_concurrency: int = typer.Option(4, help="Max claims verified at once."),
    max_budget_usd: float | None = typer.Option(None, help="Soft cap on total run cost; stops starting new claims once reached."),
) -> None:
    """Run the Verifier over every extracted claim, writing results/ and review_queue/."""
    report = verify_audit(audit_id, max_concurrency=max_concurrency, max_budget_usd=max_budget_usd)

    results_dir = REPO_ROOT / "audits" / audit_id / "results"
    supported = 0
    for claim_id in report.succeeded:
        suffix = claim_id.split("/")[-1]
        result = json.loads((results_dir / f"{suffix}.json").read_text())
        if result["verdict"]["status"] == models.VerdictStatus.SUPPORTED.value:
            supported += 1
    need_review = len(report.succeeded) - supported

    typer.echo(
        f"verified {len(report.succeeded)} claims: {supported} supported, {need_review} need review "
        f"({len(report.failed)} failed, {len(report.skipped_budget)} skipped by budget) "
        f"-- total cost ${report.total_cost_usd:.4f}"
    )
    if report.failed:
        for claim_id, error in report.failed.items():
            typer.echo(f"  FAILED {claim_id}: {error}", err=True)


@app.command()
def enrich(audit_id: str) -> None:
    """Generate entity aliases for the facts index (one LLM call, stored in
    the fact_aliases table). Used by the `catalog_aliases` eval variant."""
    import asyncio

    from proofbench.catalog import enrich_aliases_async

    written, reply = asyncio.run(enrich_aliases_async(audit_id))
    cost = f"${reply.cost_usd:.4f}" if reply.cost_usd is not None else "unknown"
    typer.echo(f"wrote {written} aliases into index/search/{audit_id}.db (cost {cost})")


@app.command()
def embed(audit_id: str) -> None:
    """Embed entity names for semantic name resolution in entity_profile
    (one OpenRouter embeddings call, stored in the entity_embeddings table).
    Rerun after `proofbench index` -- the index is rebuilt from scratch."""
    from proofbench.embeddings import embed_entities, resolve_embedding_model

    count = embed_entities(audit_id)
    typer.echo(
        f"embedded {count} entity names into index/search/{audit_id}.db "
        f"(model {resolve_embedding_model()})"
    )


@app.command()
def mentions(audit_id: str) -> None:
    """Extract prose numeric mentions (narrative body text) into the
    prose_mentions table: deterministic sentence/number candidates, one
    lightweight-model labeling call per document, embedded metric phrases.
    Rerun after `proofbench index` -- the index is rebuilt from scratch."""
    import asyncio

    from proofbench.mentions import extract_mentions_async

    count, cost = asyncio.run(extract_mentions_async(audit_id))
    typer.echo(
        f"extracted {count} prose mentions into index/search/{audit_id}.db (cost ${cost:.4f})"
    )


@app.command("eval")
def eval_cmd(
    audit_id: str,
    variants: str = typer.Option(
        "baseline,graph",
        help="Comma-separated variant names to run (see eval.py VARIANTS).",
    ),
    max_concurrency: int = typer.Option(2, help="Max claims verified at once per variant."),
    max_budget_usd: float | None = typer.Option(None, help="Soft cap on total experiment cost, across all variants."),
    experiment_id: str | None = typer.Option(None, help="Name for runs/experiments/<id>/; defaults to a timestamp."),
    claims: str | None = typer.Option(
        None, help="Comma-separated claim suffixes (e.g. 'claim-0004,claim-0007') to run a cheap subset smoke test."
    ),
) -> None:
    """Run Verifier variants against gold.yaml and compare cost vs accuracy.

    Writes only to runs/experiments/ -- never touches audits/<id>/results/,
    review_queue/, or the real run log.
    """
    from proofbench.eval import run_eval

    variant_names = [v.strip() for v in variants.split(",") if v.strip()]
    claim_suffixes = [c.strip() for c in claims.split(",") if c.strip()] if claims else None
    report = run_eval(
        audit_id,
        variant_names,
        max_concurrency=max_concurrency,
        max_budget_usd=max_budget_usd,
        experiment_id=experiment_id,
        claim_suffixes=claim_suffixes,
    )

    typer.echo(f"experiment {report['experiment_id']} (model {report['model']}) -- total cost ${report['total_cost_usd']:.4f}\n")
    header = f"{'variant':<18} {'accuracy':>9} {'correct':>8} {'failures':>9} {'avg tools':>10} {'avg cost':>10} {'total cost':>11}"
    typer.echo(header)
    typer.echo("-" * len(header))
    for s in report["summaries"]:
        accuracy = f"{s['accuracy']:.0%}" if s["accuracy"] is not None else "-"
        avg_tools = f"{s['avg_tool_calls']:.1f}" if s["avg_tool_calls"] is not None else "-"
        avg_cost = f"${s['avg_cost_usd']:.4f}" if s["avg_cost_usd"] is not None else "-"
        total = f"${s['total_cost_usd']:.4f}"
        if s["enrichment_cost_usd"] is not None:
            total += "+e"
        typer.echo(
            f"{s['variant']:<18} {accuracy:>9} {s['correct']:>5}/{s['n_claims']:<2} {s['failures']:>9} "
            f"{avg_tools:>10} {avg_cost:>10} {total:>11}"
        )
    typer.echo(f"\nfull records in runs/experiments/{report['experiment_id']}/")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address."),
    port: int = typer.Option(8420, help="Port to serve the workbench on."),
) -> None:
    """Serve the workbench UI: audits, claims, vault, and agent run traces.

    Reads live off the on-disk state -- run `extract`/`verify` in another
    terminal and the UI picks up new files on its next poll (every 3s).
    """
    import uvicorn

    typer.echo(f"Workbench at http://{host}:{port}")
    uvicorn.run("proofbench.web.api:app", host=host, port=port, log_level="warning")


if __name__ == "__main__":
    app()
