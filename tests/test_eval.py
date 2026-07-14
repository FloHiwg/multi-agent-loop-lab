"""Tests for the eval harness's per-claim dossier persistence (MUL-7).

The judge-stage replay ticket needs cached per-claim dossiers on disk so it
can re-run just the judge against `build_dossier()` output without
re-gathering (re-gathering costs real API money). This module covers only
the persistence step: `_eval_claim` writes `claim-XXXX.dossier.json`
alongside `claim-XXXX.json` when the variant builds a dossier, and skips it
entirely on the checkpoint-hit path where `verify_claim_async` never runs.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from proofbench import eval as eval_mod
from proofbench.llm import AgentReply
from proofbench.models import Claim

# asyncio_mode = "auto" in pyproject.toml already runs async defs under
# pytest-asyncio -- no module-level marker needed, and this file also has
# plain sync tests (the ablation unit tests) that a blanket marker would
# wrongly tag.


def _make_claim(suffix: str = "claim-0004") -> Claim:
    return Claim(
        claim_id=f"acme-audit/{suffix}",
        label="FY2025 revenue",
        raw_text="Revenue was $10.0M in FY2025.",
        canonical_value=10.0,
        unit="currency",
        currency="USD",
        entity="Acme Corp",
        time_scope="FY2025",
        source_doc_id="master-doc",
        source_page=1,
    )


_FAKE_REPLY_TEXT = json.dumps(
    {
        "evidence": [],
        "verdict": {
            "status": "supported",
            "confidence": 0.9,
            "rationale": "matches vault span",
        },
    }
)

_FAKE_DOSSIER = {"occurrences": [{"doc_id": "vault-doc-1", "span_text": "Revenue: $10.0M"}]}


@pytest.fixture
def budget():
    return eval_mod._Budget(None)


async def test_dossier_variant_writes_dossier_file(tmp_path, monkeypatch, budget):
    """The dossier variant must persist build_dossier()'s output next to the
    claim record, so the judge-replay ticket has something to replay from."""

    async def fake_build_dossier(claim, audit_id, **kwargs):
        return _FAKE_DOSSIER

    async def fake_run_agent(*args, **kwargs):
        return AgentReply(text=_FAKE_REPLY_TEXT, cost_usd=0.01, tool_trace=[])

    monkeypatch.setattr("proofbench.dossier.build_dossier", fake_build_dossier)
    monkeypatch.setattr("proofbench.verification.run_agent", fake_run_agent)

    claim = _make_claim()
    variant_dir = tmp_path / "dossier"
    variant_dir.mkdir()

    record = await eval_mod._eval_claim(
        eval_mod.VARIANTS["dossier"],
        claim,
        "acme-audit",
        "test-model",
        "system prompt",
        budget,
        asyncio.Semaphore(1),
        {claim.claim_id: "supported"},
        variant_dir,
    )

    assert record["status"] == "supported"
    assert record["correct"] is True

    record_path = variant_dir / "claim-0004.json"
    dossier_path = variant_dir / "claim-0004.dossier.json"
    assert record_path.exists()
    assert dossier_path.exists()
    assert json.loads(dossier_path.read_text()) == _FAKE_DOSSIER


async def test_non_dossier_variant_does_not_write_dossier_file(tmp_path, monkeypatch, budget):
    """baseline/graph/rlm variants never build a dossier -- no file should
    appear even though a record is written."""

    async def fake_run_agent(*args, **kwargs):
        return AgentReply(text=_FAKE_REPLY_TEXT, cost_usd=0.01, tool_trace=[])

    monkeypatch.setattr("proofbench.verification.run_agent", fake_run_agent)

    claim = _make_claim()
    variant_dir = tmp_path / "baseline"
    variant_dir.mkdir()

    await eval_mod._eval_claim(
        eval_mod.VARIANTS["baseline"],
        claim,
        "acme-audit",
        "test-model",
        "system prompt",
        budget,
        asyncio.Semaphore(1),
        {claim.claim_id: "supported"},
        variant_dir,
    )

    assert (variant_dir / "claim-0004.json").exists()
    assert not (variant_dir / "claim-0004.dossier.json").exists()


async def test_checkpoint_hit_does_not_build_or_write_dossier(tmp_path, monkeypatch, budget):
    """When the record file already exists from a prior run, _eval_claim
    must return early without calling build_dossier or verify_claim_async
    at all -- and must not write a dossier file."""

    def fail_build_dossier(*args, **kwargs):
        raise AssertionError("build_dossier must not be called on a checkpoint hit")

    async def fail_verify_claim_async(*args, **kwargs):
        raise AssertionError("verify_claim_async must not be called on a checkpoint hit")

    monkeypatch.setattr("proofbench.dossier.build_dossier", fail_build_dossier)
    monkeypatch.setattr(eval_mod, "verify_claim_async", fail_verify_claim_async)

    claim = _make_claim()
    variant_dir = tmp_path / "dossier"
    variant_dir.mkdir()

    existing_record = {
        "claim_id": claim.claim_id,
        "variant": "dossier",
        "expected_status": "supported",
        "status": "supported",
        "correct": True,
        "tool_calls": 0,
        "cost_usd": 0.0,
        "error": None,
    }
    (variant_dir / "claim-0004.json").write_text(json.dumps(existing_record))

    record = await eval_mod._eval_claim(
        eval_mod.VARIANTS["dossier"],
        claim,
        "acme-audit",
        "test-model",
        "system prompt",
        budget,
        asyncio.Semaphore(1),
        {claim.claim_id: "supported"},
        variant_dir,
    )

    assert record == existing_record
    assert not (variant_dir / "claim-0004.dossier.json").exists()


async def test_verify_claim_async_without_dossier_out_is_unaffected(monkeypatch):
    """Backward compatibility: existing callers (e.g. _process_claim) that
    don't pass dossier_out must see identical behavior to before -- a plain
    3-tuple, no error, dossier still built internally for the prompt."""
    from proofbench.verification import verify_claim_async

    async def fake_build_dossier(claim, audit_id, **kwargs):
        return _FAKE_DOSSIER

    async def fake_run_agent(*args, **kwargs):
        return AgentReply(text=_FAKE_REPLY_TEXT, cost_usd=0.01, tool_trace=[])

    monkeypatch.setattr("proofbench.dossier.build_dossier", fake_build_dossier)
    monkeypatch.setattr("proofbench.verification.run_agent", fake_run_agent)

    claim = _make_claim()

    evidence, verdict, reply = await verify_claim_async(
        claim,
        "acme-audit",
        run_id="test-run",
        model="test-model",
        graph_tools=False,
        dossier=True,
    )

    assert evidence == []
    assert verdict.status.value == "supported"
    assert reply.cost_usd == 0.01


async def test_verify_claim_async_with_dossier_data_skips_build_dossier(monkeypatch):
    """The judge-replay path (MUL-10) passes an already-built dossier in --
    build_dossier must not be called at all."""
    from proofbench.verification import verify_claim_async

    def fail_build_dossier(*args, **kwargs):
        raise AssertionError("build_dossier must not be called when dossier_data is provided")

    async def fake_run_agent(*args, **kwargs):
        return AgentReply(text=_FAKE_REPLY_TEXT, cost_usd=0.03, tool_trace=[])

    monkeypatch.setattr("proofbench.dossier.build_dossier", fail_build_dossier)
    monkeypatch.setattr("proofbench.verification.run_agent", fake_run_agent)

    claim = _make_claim()

    evidence, verdict, reply = await verify_claim_async(
        claim,
        "acme-audit",
        run_id="test-run",
        model="test-model",
        graph_tools=False,
        dossier=True,
        dossier_data=_FAKE_DOSSIER,
    )

    assert evidence == []
    assert verdict.status.value == "supported"
    assert reply.cost_usd == 0.03


# --- Judge-replay stage (MUL-10) -------------------------------------------


def test_ablation_strip_conflicts():
    dossier = {
        "occurrences": [{"doc_id": "d1", "source": "table"}],
        "cross_source_conflicts": [{"period": "FY2025", "values": [1, 2]}],
    }
    out = eval_mod.ABLATIONS["strip_conflicts"](dossier)
    assert out["cross_source_conflicts"] == []
    # original untouched
    assert dossier["cross_source_conflicts"] != []


def test_ablation_strip_authority_rank():
    dossier = {
        "occurrences": [
            {"doc_id": "d1", "authority_rank": 1},
            {"doc_id": "d2", "authority_rank": None},
        ]
    }
    out = eval_mod.ABLATIONS["strip_authority_rank"](dossier)
    assert all("authority_rank" not in occ for occ in out["occurrences"])
    # original untouched
    assert dossier["occurrences"][0]["authority_rank"] == 1


def test_ablation_drop_researcher():
    dossier = {
        "occurrences": [
            {"doc_id": "d1", "source": "table"},
            {"doc_id": "d2", "source": "researcher"},
            {"doc_id": "d3", "source": "prose"},
        ]
    }
    out = eval_mod.ABLATIONS["drop_researcher"](dossier)
    assert [occ["source"] for occ in out["occurrences"]] == ["table", "prose"]
    # original untouched
    assert len(dossier["occurrences"]) == 3


async def test_judge_claim_reads_cached_dossier_and_writes_record(tmp_path, monkeypatch, budget):
    dossiers_from_dir = tmp_path / "src-exp" / "dossier"
    dossiers_from_dir.mkdir(parents=True)
    (dossiers_from_dir / "claim-0004.dossier.json").write_text(json.dumps(_FAKE_DOSSIER))
    monkeypatch.setattr(eval_mod, "EXPERIMENTS_DIR", tmp_path)

    seen_dossier_data = {}

    async def fake_verify_claim_async(claim, audit_id, *, run_id, model, system_prompt, graph_tools, rlm, dossier, dossier_data):
        seen_dossier_data["value"] = dossier_data
        from proofbench.jsonutil import extract_json
        from proofbench.models import Verdict, VerdictStatus

        raw = extract_json(_FAKE_REPLY_TEXT)
        verdict = Verdict(
            claim_id=claim.claim_id,
            status=VerdictStatus(raw["verdict"]["status"]),
            matched_evidence_ids=[],
            confidence=raw["verdict"]["confidence"],
            rationale=raw["verdict"]["rationale"],
            produced_by_run_id=run_id,
        )
        reply = AgentReply(text=_FAKE_REPLY_TEXT, cost_usd=0.03, tool_trace=[])
        return [], verdict, reply

    monkeypatch.setattr(eval_mod, "verify_claim_async", fake_verify_claim_async)

    claim = _make_claim()
    variant_dir = tmp_path / "out-exp" / "judge"
    variant_dir.mkdir(parents=True)

    record = await eval_mod._judge_claim(
        claim,
        "acme-audit",
        "test-model",
        "system prompt",
        budget,
        asyncio.Semaphore(1),
        {claim.claim_id: "supported"},
        variant_dir,
        "src-exp",
        "dossier",
        None,
        1,
    )

    assert record["status"] == "supported"
    assert record["correct"] is True
    assert seen_dossier_data["value"] == _FAKE_DOSSIER
    record_path = variant_dir / "claim-0004.repeat-01.json"
    assert record_path.exists()


async def test_judge_claim_missing_dossier_raises_file_not_found(tmp_path, monkeypatch, budget):
    monkeypatch.setattr(eval_mod, "EXPERIMENTS_DIR", tmp_path)
    claim = _make_claim()
    variant_dir = tmp_path / "out-exp" / "judge"
    variant_dir.mkdir(parents=True)

    with pytest.raises(FileNotFoundError):
        await eval_mod._judge_claim(
            claim,
            "acme-audit",
            "test-model",
            "system prompt",
            budget,
            asyncio.Semaphore(1),
            {claim.claim_id: "supported"},
            variant_dir,
            "no-such-exp",
            "dossier",
            None,
            1,
        )


async def test_judge_claim_repeats_disagree_and_agree(tmp_path, monkeypatch, budget):
    dossiers_from_dir = tmp_path / "src-exp" / "dossier"
    dossiers_from_dir.mkdir(parents=True)
    (dossiers_from_dir / "claim-0004.dossier.json").write_text(json.dumps(_FAKE_DOSSIER))
    monkeypatch.setattr(eval_mod, "EXPERIMENTS_DIR", tmp_path)

    from proofbench.models import Verdict, VerdictStatus

    call_count = {"n": 0}

    async def fake_verify_claim_async(claim, audit_id, *, run_id, model, system_prompt, graph_tools, rlm, dossier, dossier_data):
        call_count["n"] += 1
        status = VerdictStatus.SUPPORTED if call_count["n"] == 1 else VerdictStatus.CONTRADICTED
        verdict = Verdict(
            claim_id=claim.claim_id,
            status=status,
            matched_evidence_ids=[],
            confidence=0.5,
            rationale="r",
            produced_by_run_id=run_id,
        )
        reply = AgentReply(text=_FAKE_REPLY_TEXT, cost_usd=0.01, tool_trace=[])
        return [], verdict, reply

    monkeypatch.setattr(eval_mod, "verify_claim_async", fake_verify_claim_async)

    claim = _make_claim()
    variant_dir = tmp_path / "out-exp" / "judge"
    variant_dir.mkdir(parents=True)

    records = []
    for repeat in (1, 2):
        record = await eval_mod._judge_claim(
            claim,
            "acme-audit",
            "test-model",
            "system prompt",
            budget,
            asyncio.Semaphore(1),
            {claim.claim_id: "supported"},
            variant_dir,
            "src-exp",
            "dossier",
            None,
            repeat,
        )
        records.append(record)

    assert records[0]["status"] == "supported"
    assert records[1]["status"] == "contradicted"
    summary = eval_mod._summarize_judge([[records[0]], [records[1]]], [claim.claim_id])
    assert summary["unanimous_claims"] == 0
    assert summary["verdict_consistency"] == 0.0
    assert call_count["n"] == 2

    # checkpointing: rerunning repeat 1 must not call the fake judge again
    record_again = await eval_mod._judge_claim(
        claim,
        "acme-audit",
        "test-model",
        "system prompt",
        budget,
        asyncio.Semaphore(1),
        {claim.claim_id: "supported"},
        variant_dir,
        "src-exp",
        "dossier",
        None,
        1,
    )
    assert record_again == records[0]
    assert call_count["n"] == 2


def _minimal_record(status: str) -> dict:
    return {
        "claim_id": "c/claim-0001",
        "status": status,
        "correct": True,
        "error": None,
        "tool_calls": 0,
        "cost_usd": 0.0,
    }


def test_judge_claim_repeats_agree():
    summary = eval_mod._summarize_judge(
        [[_minimal_record("supported")], [_minimal_record("supported")]],
        ["c/claim-0001"],
    )
    assert summary["unanimous_claims"] == 1
    assert summary["verdict_consistency"] == 1.0


# --- Gather-only stage (MUL-9) ---------------------------------------------


def test_score_recall_matches_by_doc_id_and_quote():
    gold_evidence = [
        {"doc_id": "d1", "source": "table", "quote": "Net revenue | 12.480"},
        {"doc_id": "d2", "source": "prose", "quote": "revenue grew to EUR 12.48 million"},
    ]
    occurrences = [
        {"doc_id": "d1", "source": "table", "quote": "Net revenue | 12.480"},
        {"doc_id": "d2", "source": "researcher", "quote": "in Q1, revenue grew to EUR 12.48 million overall"},
    ]
    result = eval_mod._score_recall(gold_evidence, occurrences)
    assert result["n_gold_evidence"] == 2
    assert result["n_matched"] == 2
    assert result["recall"] == 1.0
    assert {m["found_via"] for m in result["matched"]} == {"table", "researcher"}
    assert result["unmatched"] == []


def test_score_recall_reports_unmatched_when_no_occurrence_found():
    gold_evidence = [{"doc_id": "d1", "source": "table", "quote": "Net revenue | 12.480"}]
    occurrences = [{"doc_id": "d1", "source": "table", "quote": "totally different span"}]
    result = eval_mod._score_recall(gold_evidence, occurrences)
    assert result["n_matched"] == 0
    assert result["recall"] == 0.0
    assert result["unmatched"] == [{"doc_id": "d1", "gold_source": "table", "quote": "Net revenue | 12.480"}]


def test_score_recall_empty_gold_evidence_is_not_scored():
    result = eval_mod._score_recall([], [{"doc_id": "d1", "source": "table", "quote": "x"}])
    assert result["n_gold_evidence"] == 0
    assert result["recall"] is None


def _fts_conn(rows: list[tuple[str, str, str]]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE spans_fts USING fts5(doc_id, location, text)")
    conn.executemany("INSERT INTO spans_fts (doc_id, location, text) VALUES (?, ?, ?)", rows)
    return conn


def test_verify_researcher_quotes_marks_verbatim_quote_verified():
    conn = _fts_conn([("d1", "page-1", "Net revenue was EUR 12.48 million in the quarter.")])
    occurrences = [{"source": "researcher", "doc_id": "d1", "quote": "Net revenue was EUR 12.48 million"}]
    result = eval_mod._verify_researcher_quotes(conn, occurrences)
    assert result["n_researcher_occurrences"] == 1
    assert result["n_verified"] == 1
    assert result["n_hallucinated"] == 0
    assert result["hallucination_rate"] == 0.0


def test_verify_researcher_quotes_flags_quote_not_found_in_doc():
    conn = _fts_conn([("d1", "page-1", "Net revenue was EUR 12.48 million in the quarter.")])
    occurrences = [{"source": "researcher", "doc_id": "d1", "quote": "a number that was never said"}]
    result = eval_mod._verify_researcher_quotes(conn, occurrences)
    assert result["n_verified"] == 0
    assert result["n_hallucinated"] == 1
    assert result["hallucination_rate"] == 1.0
    assert result["hallucinated"] == [{"doc_id": "d1", "quote": "a number that was never said"}]


def test_verify_researcher_quotes_ignores_non_researcher_occurrences():
    conn = _fts_conn([])
    occurrences = [{"source": "table", "doc_id": "d1", "quote": "anything"}]
    result = eval_mod._verify_researcher_quotes(conn, occurrences)
    assert result["n_researcher_occurrences"] == 0
    assert result["hallucination_rate"] is None


_FAKE_GATHER_DOSSIER = {
    "claim_id": "acme-audit/claim-0004",
    "occurrences": [
        {"source": "table", "doc_id": "vault-doc-1", "quote": "Revenue: $10.0M"},
        {"source": "researcher", "doc_id": "vault-doc-1", "quote": "some researcher span"},
    ],
}


async def test_gather_claim_builds_dossier_and_writes_record_and_dossier_file(tmp_path, monkeypatch, budget):
    async def fake_build_dossier(claim, audit_id, *, use_researcher, sub_costs=None):
        if sub_costs is not None:
            sub_costs.append(0.02)
        return _FAKE_GATHER_DOSSIER

    fake_conn = _fts_conn([("vault-doc-1", "p1", "some researcher span, in context")])
    monkeypatch.setattr(eval_mod, "build_dossier", fake_build_dossier)
    monkeypatch.setattr(eval_mod, "db_path", lambda audit_id: ":memory:")
    monkeypatch.setattr(eval_mod.sqlite3, "connect", lambda path: fake_conn)

    claim = _make_claim()
    variant_dir = tmp_path / "gather"
    variant_dir.mkdir()
    gold_evidence = [{"doc_id": "vault-doc-1", "source": "table", "quote": "Revenue: $10.0M"}]

    record = await eval_mod._gather_claim(
        claim, "acme-audit", True, budget, asyncio.Semaphore(1), gold_evidence, variant_dir
    )

    assert record["error"] is None
    assert record["cost_usd"] == 0.02
    assert record["gold_evidence"]["recall"] == 1.0
    assert record["researcher_quotes"]["n_verified"] == 1

    record_path = variant_dir / "claim-0004.json"
    dossier_path = variant_dir / "claim-0004.dossier.json"
    assert record_path.exists()
    assert dossier_path.exists()
    assert json.loads(dossier_path.read_text()) == _FAKE_GATHER_DOSSIER


async def test_gather_claim_checkpoint_hit_skips_build_dossier(tmp_path, monkeypatch, budget):
    def fail_build_dossier(*args, **kwargs):
        raise AssertionError("build_dossier must not be called on a checkpoint hit")

    monkeypatch.setattr(eval_mod, "build_dossier", fail_build_dossier)

    claim = _make_claim()
    variant_dir = tmp_path / "gather"
    variant_dir.mkdir()
    existing_record = {"claim_id": claim.claim_id, "use_researcher": True, "cost_usd": 0.0, "error": None}
    (variant_dir / "claim-0004.json").write_text(json.dumps(existing_record))

    record = await eval_mod._gather_claim(claim, "acme-audit", True, budget, asyncio.Semaphore(1), [], variant_dir)

    assert record == existing_record
    assert not (variant_dir / "claim-0004.dossier.json").exists()


def test_summarize_gather_aggregates_recall_by_source_and_hallucination_rate():
    records = [
        {
            "claim_id": "c/claim-0001",
            "cost_usd": 0.01,
            "error": None,
            "gold_evidence": {
                "n_gold_evidence": 1,
                "n_matched": 1,
                "recall": 1.0,
                "matched": [{"doc_id": "d1", "gold_source": "table", "found_via": "table"}],
                "unmatched": [],
            },
            "researcher_quotes": {"n_researcher_occurrences": 1, "n_verified": 1, "n_hallucinated": 0},
        },
        {
            "claim_id": "c/claim-0002",
            "cost_usd": 0.02,
            "error": None,
            "gold_evidence": {
                "n_gold_evidence": 1,
                "n_matched": 0,
                "recall": 0.0,
                "matched": [],
                "unmatched": [{"doc_id": "d2", "gold_source": "prose", "quote": "x"}],
            },
            "researcher_quotes": {"n_researcher_occurrences": 1, "n_verified": 0, "n_hallucinated": 1},
        },
        {
            "claim_id": "c/claim-0003",
            "cost_usd": 0.0,
            "error": None,
            "gold_evidence": {"n_gold_evidence": 0, "n_matched": 0, "recall": None, "matched": [], "unmatched": []},
            "researcher_quotes": {"n_researcher_occurrences": 0, "n_verified": 0, "n_hallucinated": 0},
        },
    ]
    summary = eval_mod._summarize_gather(records)
    assert summary["n_claims"] == 3
    assert summary["n_claims_with_gold_evidence"] == 2
    assert summary["n_gold_evidence"] == 2
    assert summary["n_matched"] == 1
    assert summary["recall"] == 0.5
    assert summary["by_source"]["table"] == {"matched": 1, "total": 1, "recall": 1.0}
    assert summary["by_source"]["prose"] == {"matched": 0, "total": 1, "recall": 0.0}
    assert summary["found_via"]["table"] == {"matched": 1, "share_of_gold": 0.5}
    assert summary["researcher_total"] == 2
    assert summary["researcher_verified"] == 1
    assert summary["researcher_hallucinated"] == 1
    assert summary["researcher_hallucination_rate"] == 0.5
