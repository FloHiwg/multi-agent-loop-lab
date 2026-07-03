"""Data contracts for Proofbench.

These Pydantic models are the single source of truth for the schemas.
Run `proofbench schemas export` to regenerate the JSON Schema files in
`schemas/` from these classes -- never hand-edit the exported files.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Shared enums
# ---------------------------------------------------------------------------


class VerdictStatus(str, Enum):
    """The five (plus pending) states a claim can be in."""

    PENDING = "pending"
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    AMBIGUOUS = "ambiguous"
    OUTDATED = "outdated"
    MISSING_EVIDENCE = "missing_evidence"


class EvidenceRole(str, Enum):
    """What a number *is* in its source document.

    Guards against the dominant failure mode: a correctly-read number bound
    to the wrong role, entity, or period.
    """

    ACTUAL = "actual"
    BUDGET = "budget"
    FORECAST = "forecast"
    PRIOR_PERIOD = "prior_period"
    RESTATED = "restated"
    OTHER = "other"


# ---------------------------------------------------------------------------
# Tolerance rules (discriminated union)
# ---------------------------------------------------------------------------


class ExactMatch(BaseModel):
    kind: Literal["exact"] = "exact"


class PercentTolerance(BaseModel):
    kind: Literal["percent_tolerance"] = "percent_tolerance"
    tolerance_pct: float = Field(gt=0, le=100)


class FormulaCheck(BaseModel):
    kind: Literal["formula"] = "formula"
    formula: str = Field(description="Expression referencing other claim_ids, e.g. 'a + b - c'")
    tolerance_pct: float = Field(default=0.0, ge=0, le=100)


ToleranceRule = Annotated[
    Union[ExactMatch, PercentTolerance, FormulaCheck],
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------


class Claim(BaseModel):
    """An atomic, verifiable numeric assertion extracted from a master document."""

    claim_id: str = Field(description="Stable identifier, e.g. 'audit-2026-07-acme/claim-0001'")
    label: str = Field(description="Human-readable name, e.g. 'FY2025 revenue'")
    raw_text: str = Field(description="Verbatim span from the master document")
    canonical_value: float = Field(description="Normalized numeric value")
    unit: str = Field(description="e.g. 'currency', 'percent', 'count', 'ratio'")
    currency: str | None = Field(default=None, description="ISO 4217 code, if applicable")
    entity: str = Field(description="Which company / subsidiary / party this claim is about")
    time_scope: str = Field(description="Period or as-of date, e.g. 'FY2025' or '2026-03-31'")
    tolerance_rule: ToleranceRule = Field(default_factory=ExactMatch)
    expected_evidence_type: str | None = Field(
        default=None, description="Which vault document class should support this, e.g. 'annual_report'"
    )
    status: VerdictStatus = VerdictStatus.PENDING

    source_doc_id: str = Field(description="doc_id of the master document this claim was extracted from")
    source_page: int | None = None


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


class CharOffsets(BaseModel):
    start: int
    end: int


class BoundingBox(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float


class EvidenceCandidate(BaseModel):
    """A located span in a vault document that may support or contradict a claim."""

    evidence_id: str
    claim_id: str = Field(description="The claim this candidate was retrieved for")
    doc_id: str
    page: int | None = None
    span_text: str = Field(description="Verbatim source text")
    bbox: BoundingBox | None = None
    char_offsets: CharOffsets | None = None
    canonical_value: float | None = None
    unit: str | None = None
    role: EvidenceRole = EvidenceRole.OTHER
    effective_date: date | None = None
    extractor_confidence: float = Field(ge=0, le=1)


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


class Verdict(BaseModel):
    """The Verifier's output for a single claim."""

    claim_id: str
    status: VerdictStatus
    matched_evidence_ids: list[str] = Field(default_factory=list)
    delta: float | None = Field(default=None, description="canonical_value difference vs. best evidence, if any")
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(description="Short explanation of why this verdict was reached")
    suggested_action: str | None = None
    produced_by_run_id: str


# ---------------------------------------------------------------------------
# Run manifest
# ---------------------------------------------------------------------------


class AgentRole(str, Enum):
    MANAGER = "manager"
    CLAIM_EXTRACTOR = "claim_extractor"
    VAULT_RETRIEVER = "vault_retriever"
    VERIFIER = "verifier"
    REPAIR = "repair"


class RunManifest(BaseModel):
    """One append-only record of an agent run, for replay and audit."""

    run_id: str
    audit_id: str
    agent_role: AgentRole
    started_at: datetime
    finished_at: datetime | None = None
    input_refs: list[str] = Field(default_factory=list, description="claim_ids / doc_ids consumed")
    output_refs: list[str] = Field(default_factory=list, description="claim_ids / evidence_ids / verdict ids produced")
    prompt: str | None = None
    tool_trace: list[dict] = Field(default_factory=list)
    status: Literal["running", "succeeded", "failed"] = "running"
    error: str | None = None


# ---------------------------------------------------------------------------
# Audit config
# ---------------------------------------------------------------------------


class DocumentFormat(str, Enum):
    PDF = "pdf"
    XLSX = "xlsx"


class DocumentKind(str, Enum):
    MASTER = "master"
    VAULT = "vault"


class DocumentRef(BaseModel):
    """One entry in an audit's document registry, pointing at a file on disk."""

    doc_id: str
    path: str = Field(description="Path relative to the repo root")
    kind: DocumentKind
    format: DocumentFormat
    tag: str | None = Field(default=None, description="Evidence class, e.g. 'finance_pack' -- matches evidence_priority")


class AuditConfig(BaseModel):
    """audit.yaml -- the manifest for a single audit case."""

    audit_id: str
    master_doc_id: str = Field(description="doc_id of the master document under audits/<id>/master/")
    default_tolerance_rule: ToleranceRule = Field(default_factory=ExactMatch)
    evidence_priority: list[str] = Field(
        default_factory=list,
        description="Ordered document classes/tags preferred as evidence when sources conflict",
    )
    documents: list[DocumentRef] = Field(
        default_factory=list, description="Registry of master + vault documents this audit draws on"
    )
    created_at: datetime
