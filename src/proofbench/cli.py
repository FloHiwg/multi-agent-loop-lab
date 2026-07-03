"""Proofbench CLI."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from dotenv import load_dotenv

from proofbench import models
from proofbench.extraction import extract_claims
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
def extract(audit_id: str) -> None:
    """Run the Claim Extractor over the master document, writing audits/<id>/claims/."""
    claims = extract_claims(audit_id)
    typer.echo(f"extracted {len(claims)} claims into audits/{audit_id}/claims/")


@app.command()
def verify(audit_id: str) -> None:
    """Run the Verifier over every extracted claim, writing results/ and review_queue/."""
    verdicts = verify_audit(audit_id)
    supported = sum(1 for v in verdicts if v.status == models.VerdictStatus.SUPPORTED)
    typer.echo(f"verified {len(verdicts)} claims: {supported} supported, {len(verdicts) - supported} need review")


if __name__ == "__main__":
    app()
