"""Write append-only RunManifest records to runs/."""

from __future__ import annotations

from pathlib import Path

from proofbench.models import RunManifest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNS_DIR = REPO_ROOT / "runs"


def write_run_manifest(manifest: RunManifest) -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    suffix = manifest.run_id.replace("/", "__")
    path = RUNS_DIR / f"{suffix}.json"
    path.write_text(manifest.model_dump_json(indent=2) + "\n")
    return path
