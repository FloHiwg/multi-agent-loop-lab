"""Tests for the workbench's Eval view backend (MUL-11).

The served /api/experiments* endpoints originally assumed every
report.json has a top-level "summaries" list (the "full" stage shape).
MUL-9/MUL-10 added "gather" and "judge" stage reports with a different
("summary", singular) shape, which crashed the existing endpoints. These
tests cover all three stages plus the pre-existing "claim-*.json" glob bug
(MUL-7's persisted *.dossier.json sidecars match that glob and aren't
real per-claim records).
"""

from __future__ import annotations

import json

import pytest

from proofbench.web import api


@pytest.fixture(autouse=True)
def _experiments_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "EXPERIMENTS_DIR", tmp_path)
    return tmp_path


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _make_full_experiment(root):
    exp_dir = root / "exp-full"
    _write(
        exp_dir / "report.json",
        {
            "experiment_id": "exp-full",
            "audit_id": "acme-audit",
            "model": "z-ai/glm-5.2",
            "started_variants": ["dossier"],
            "total_cost_usd": 0.05,
            "summaries": [
                {
                    "variant": "dossier",
                    "n_claims": 1,
                    "correct": 1,
                    "accuracy": 1.0,
                    "failures": 0,
                    "avg_tool_calls": 2.0,
                    "avg_cost_usd": 0.05,
                    "total_cost_usd": 0.05,
                    "n_attempted": 1,
                }
            ],
        },
    )
    _write(
        exp_dir / "dossier" / "claim-0001.json",
        {
            "claim_id": "acme-audit/claim-0001",
            "variant": "dossier",
            "expected_status": "supported",
            "status": "supported",
            "correct": True,
            "tool_calls": 2,
            "cost_usd": 0.05,
            "error": None,
        },
    )
    # Persisted dossier sidecar (MUL-7) -- matches the "claim-*.json" glob
    # but has none of a record's fields; must be excluded, not crash.
    _write(exp_dir / "dossier" / "claim-0001.dossier.json", {"occurrences": []})


def _make_gather_experiment(root):
    exp_dir = root / "exp-gather"
    _write(
        exp_dir / "report.json",
        {
            "experiment_id": "exp-gather",
            "audit_id": "acme-audit",
            "stage": "gather",
            "use_researcher": True,
            "total_cost_usd": 0.01,
            "summary": {
                "n_claims": 1,
                "n_claims_with_gold_evidence": 1,
                "n_gold_evidence": 2,
                "n_matched": 1,
                "recall": 0.5,
                "by_source": {"table": {"matched": 1, "total": 2, "recall": 0.5}},
                "found_via": {"table": {"matched": 1, "share_of_gold": 0.5}},
                "researcher_total": 2,
                "researcher_verified": 1,
                "researcher_hallucinated": 1,
                "researcher_hallucination_rate": 0.5,
                "failures": 0,
                "avg_cost_usd": 0.01,
                "total_cost_usd": 0.01,
            },
        },
    )
    _write(
        exp_dir / "gather" / "claim-0001.json",
        {
            "claim_id": "acme-audit/claim-0001",
            "use_researcher": True,
            "cost_usd": 0.01,
            "error": None,
            "gold_evidence": {
                "n_gold_evidence": 2,
                "n_matched": 1,
                "recall": 0.5,
                "matched": [{"doc_id": "doc-1", "gold_source": "table", "found_via": "table"}],
                "unmatched": [{"doc_id": "doc-2", "gold_source": "prose", "quote": "missed quote"}],
            },
            "researcher_quotes": {
                "n_researcher_occurrences": 2,
                "n_verified": 1,
                "n_hallucinated": 1,
                "hallucination_rate": 0.5,
                "hallucinated": [{"doc_id": "doc-3", "quote": "fabricated"}],
            },
        },
    )
    _write(exp_dir / "gather" / "claim-0001.dossier.json", {"occurrences": []})


def _make_judge_experiment(root):
    exp_dir = root / "exp-judge"
    _write(
        exp_dir / "report.json",
        {
            "experiment_id": "exp-judge",
            "audit_id": "acme-audit",
            "stage": "judge",
            "dossiers_from": "exp-gather",
            "dossiers_variant": "gather",
            "ablation": None,
            "repeats": 2,
            "model": "z-ai/glm-5.2",
            "total_cost_usd": 0.06,
            "summary": {
                "per_repeat": [
                    {"variant": "judge", "n_claims": 1, "correct": 1, "accuracy": 1.0, "failures": 0,
                     "avg_tool_calls": 0.0, "avg_cost_usd": 0.03, "total_cost_usd": 0.03, "n_attempted": 1},
                    {"variant": "judge", "n_claims": 1, "correct": 0, "accuracy": 0.0, "failures": 0,
                     "avg_tool_calls": 0.0, "avg_cost_usd": 0.03, "total_cost_usd": 0.03, "n_attempted": 1},
                ],
                "n_claims": 1,
                "repeats": 2,
                "n_claims_with_multiple_repeats": 1,
                "unanimous_claims": 0,
                "verdict_consistency": 0.0,
            },
        },
    )
    _write(
        exp_dir / "judge" / "claim-0001.repeat-01.json",
        {
            "claim_id": "acme-audit/claim-0001",
            "repeat": 1,
            "expected_status": "supported",
            "status": "supported",
            "correct": True,
            "tool_calls": 0,
            "cost_usd": 0.03,
            "error": None,
        },
    )
    _write(
        exp_dir / "judge" / "claim-0001.repeat-02.json",
        {
            "claim_id": "acme-audit/claim-0001",
            "repeat": 2,
            "expected_status": "supported",
            "status": "contradicted",
            "correct": False,
            "tool_calls": 0,
            "cost_usd": 0.03,
            "error": None,
        },
    )


def test_list_experiments_across_stages(tmp_path):
    _make_full_experiment(tmp_path)
    _make_gather_experiment(tmp_path)
    _make_judge_experiment(tmp_path)

    rows = {r["experiment_id"]: r for r in api.list_experiments()}
    assert set(rows) == {"exp-full", "exp-gather", "exp-judge"}

    assert rows["exp-full"]["stage"] == "full"
    assert rows["exp-full"]["label"] == "dossier"
    assert rows["exp-full"]["accuracy"] == 1.0

    assert rows["exp-gather"]["stage"] == "gather"
    assert rows["exp-gather"]["accuracy"] == 0.5

    assert rows["exp-judge"]["stage"] == "judge"
    assert rows["exp-judge"]["accuracy"] == 0.0  # last repeat's accuracy
    assert rows["exp-judge"]["dossiers_from"] == "exp-gather"


def test_list_experiments_filters_by_audit_id(tmp_path):
    _make_full_experiment(tmp_path)
    _make_gather_experiment(tmp_path)
    assert len(api.list_experiments(audit_id="acme-audit")) == 2
    assert api.list_experiments(audit_id="no-such-audit") == []


def test_full_stage_detail_excludes_dossier_sidecar(tmp_path):
    _make_full_experiment(tmp_path)
    detail = api.experiment_detail("exp-full")
    assert detail["stage"] == "full"
    rows = detail["claims_by_variant"]["dossier"]
    assert len(rows) == 1
    assert rows[0]["claim_id"] == "acme-audit/claim-0001"


def test_gather_stage_detail(tmp_path):
    _make_gather_experiment(tmp_path)
    detail = api.experiment_detail("exp-gather")
    assert detail["stage"] == "gather"
    rows = detail["claims"]
    assert len(rows) == 1
    assert rows[0]["recall"] == 0.5
    assert rows[0]["n_matched"] == 1
    assert rows[0]["researcher_hallucinated"] == 1


def test_judge_stage_detail_groups_repeats_and_flags_disagreement(tmp_path):
    _make_judge_experiment(tmp_path)
    detail = api.experiment_detail("exp-judge")
    assert detail["stage"] == "judge"
    rows = detail["claims"]
    assert len(rows) == 1
    row = rows[0]
    assert len(row["repeats"]) == 2
    assert row["unanimous"] is False
    assert row["majority_status"] in ("supported", "contradicted")  # tie broken deterministically by max()


def test_experiment_claim_judge_repeat_selects_correct_file(tmp_path):
    _make_judge_experiment(tmp_path)
    record = api.experiment_claim("exp-judge", "judge", "claim-0001", repeat=2)
    assert record["repeat"] == 2
    assert record["status"] == "contradicted"

    record1 = api.experiment_claim("exp-judge", "judge", "claim-0001", repeat=1)
    assert record1["status"] == "supported"


def test_experiment_claim_gather_no_repeat(tmp_path):
    _make_gather_experiment(tmp_path)
    record = api.experiment_claim("exp-gather", "gather", "claim-0001")
    assert record["gold_evidence"]["recall"] == 0.5
