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

import pytest

from proofbench import eval as eval_mod
from proofbench.llm import AgentReply
from proofbench.models import Claim

pytestmark = pytest.mark.asyncio


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
