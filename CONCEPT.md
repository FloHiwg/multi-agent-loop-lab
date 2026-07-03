# Proofbench — Concept

**A local claim–evidence audit workbench.** An agent extracts numeric claims from a master document, verifies each one against a vault of basis documents, and produces reviewable evidence cards with full provenance. A human resolves what the agent can't.

Working title: **Proofbench** (alternatives: Audit Workbench, Number Ledger, Evidence Workbench).

---

## 1. Problem

Audit and diligence work is full of a single repetitive task: *does this number in the master document match what the underlying documents say?* Today that means a person opening PDFs side by side, finding the right page, checking units, dates, and entities, and noting discrepancies. It is slow, error-prone, and leaves no structured trace of what was checked against what.

LLMs can read documents, but "chat over the corpus" fails audits for two reasons:

1. **No provenance.** A chat answer can't show *which span on which page* supports a number.
2. **Structural binding errors.** The dominant extraction failure mode is not misreading a number — it's binding a correctly-read number to the wrong role, entity, period, or unit.

## 2. Product idea

Not an autonomous auditor. A **number-checking workbench**: the agent does one narrow thing — claim-by-claim numeric verification with provenance — and everything else stays human.

The verification loop:

```
master doc ──▶ atomic claims ──▶ retrieve candidates ──▶ verify ──▶ verdict + evidence card ──▶ human review
```

Each master-document number becomes an **atomic claim** (value + unit + entity + time scope). The agent searches the vault for candidate evidence spans, compares under explicit rules (exact match, tolerance match, formula check, contradiction), and emits one of five verdicts:

`supported` · `contradicted` · `ambiguous` · `outdated` · `missing_evidence`

Anything not cleanly `supported` lands in a review queue as an **evidence card**: the claim, all candidate spans with page crops, the delta, and a suggested next step. The human accepts, rejects, or escalates. The system never decides alone.

### Example claim card

> **Claim:** Revenue: EUR 4.28m, FY2025 (master doc, p. 3)
> **Evidence:** Annual report p. 14 → EUR 4.28m · Board deck p. 6 → EUR 4.31m
> **Verdict:** `ambiguous` — two authoritative sources conflict
> **Suggested action:** Prefer signed annual report unless a newer amendment exists.

## 3. Why local-first

The repo *is* the workbench state. No server, no SaaS, everything inspectable and replayable:

- **Git** tracks configs, schemas, claims, verdicts, and accepted review outcomes.
- **Raw documents** live in the vault, untouched — they are evidence, never rewritten.
- **Derived artifacts** (parses, spans, embeddings) live in a separate index directory and are always rebuildable. Swapping the parser never touches evidence.
- **Runs are append-only logs** — every prompt, tool call, and output is recorded, so an audit can be replayed and disputed later.

This separation — evidence vs. machine working state vs. human decisions — is the core architectural stance.

## 4. Repository layout

```text
audit-repo/
  audits/
    audit-2026-07-acme/
      audit.yaml          # config: master doc, tolerance rules, evidence priorities
      master/             # the document whose numbers are being audited
      claims/             # extracted atomic claims (one YAML/JSON per claim)
      results/            # verdicts + evidence bindings
      review_queue/       # open evidence cards awaiting a human
  vault/                  # basis documents: contracts, board packs, invoices, statements
  index/
    parsed/               # extracted text / OCR per doc
    spans/                # located spans with bbox / char offsets
    facts/                # normalized numeric facts (value, unit, role, date)
    embeddings/           # retrieval index
  runs/                   # append-only agent run logs (prompts, traces, outputs)
  reviews/                # human decisions, overrides, accepted evidence packs
  schemas/                # the data contracts everything validates against
```

One audit = one folder = one manifest. Branch-per-audit-experiment works naturally.

## 5. Data contracts

The system stands or falls on the claim schema. Strictness here is deliberate.

**Claim:**

| Field | Purpose |
|---|---|
| `claim_id` | stable reference |
| `label` | human-readable name ("FY2025 revenue") |
| `raw_text` | verbatim span from the master doc |
| `canonical_value` | normalized number |
| `unit`, `currency` | disambiguation ("4.28" vs "4.28m EUR") |
| `entity` | which company/subsidiary/party |
| `time_scope` | period or as-of date |
| `tolerance_rule` | exact / ±x% / formula |
| `expected_evidence_type` | which vault doc class should support this |
| `status` | current verdict |

**Evidence candidate:**

| Field | Purpose |
|---|---|
| `doc_id`, `page` | location |
| `span_text` | verbatim source text |
| `bbox` / `char_offsets` | pixel-level grounding for the UI |
| `canonical_value`, `unit` | normalized comparison basis |
| `role` | what the number *is* in its source (guards against wrong-binding) |
| `effective_date` | freshness ranking |
| `extractor_confidence` | triage signal |

Every field exists to prevent one specific failure: a right number bound to the wrong thing.

## 6. Agent harness

One manager, four bounded workers, typed outputs everywhere. No open-ended loops.

| Agent | Job | Bound |
|---|---|---|
| **Manager** | reads audit config, enumerates claims, schedules jobs, enforces budgets | orchestration only, never judges evidence |
| **Claim Extractor** | master text → typed claims | one document, emits schema-valid claims or nothing |
| **Vault Retriever** | claim → candidate docs + spans | metadata filter + search, returns top-k candidates |
| **Verifier** | claim + candidates → verdict, normalized values, deltas | explicit comparison rules only |
| **Repair Agent** | ambiguous claims only: widen search, inspect adjacent pages/tables | invoked on demand, capped retries |

Each worker gets a scoped workspace, produces schema-validated output, and its full trace lands in `runs/`. The manager never free-forms; it executes a deterministic loop over the claim list.

## 7. Workbench UI

Three views, one audit at a time. Modeled on claim–evidence interfaces (PaperTrail-style): claims and their support are first-class, not buried in citations.

| View | Shows | Primary action |
|---|---|---|
| **Audit** | master doc, claim list, coverage %, risk flags | start / rerun verification |
| **Vault** | folder tree, doc metadata, parse status, fact counts | add / reindex documents |
| **Claim review** | one claim, candidate spans, verdict, deltas, page crops | accept / reject / escalate |

Core interaction: click a claim → see all candidate evidence on the right → inspect the exact source span or page crop. That single interaction is what makes it feel like an audit tool instead of a chat app.

## 8. MVP scope

**In:**

1. `audit init` — parse and index master + vault documents
2. `audit verify <audit-id>` — extract claims, retrieve, verify, write results
3. Workbench UI showing the three views and the review queue
4. Claim/evidence schemas with validation
5. Replayable run logs

**Explicitly out (v1):**

- Open-ended chat over the vault
- Auto-drafted audit memos
- Legal or accounting judgment
- Any autonomous decision without human review
- Multi-audit dashboards, multi-user, sync

**MVP happy path:** drop a master PDF into `audits/<id>/master/`, drop basis docs into `vault/`, run init + verify, open the workbench, work through the review queue.

## 9. Success criteria

The MVP works if, on a real audit case with ~50–200 numeric claims:

- **Claim extraction recall** — ≥90% of the master document's material numbers become claims (missed claims are silent failures; this matters most).
- **Binding precision** — spot-checked evidence bindings have the right role/entity/period ≥95% of the time; a wrong-but-confident binding is the worst outcome.
- **Honest verdicts** — `supported` is only emitted with a grounded span; everything uncertain routes to review rather than guessing.
- **Time saved** — reviewing the queue is meaningfully faster than manual side-by-side checking, measured on one real case.
- **Replayability** — any past verdict can be traced to the exact run, prompt, and span that produced it.

## 10. Open questions

1. **Parser choice** — PyMuPDF for born-digital PDFs is the easy start; OCR strategy for scans (and spreadsheets/emails) is the first real fork in the road.
2. **UI substrate** — local web app (FastAPI/Flask + simple frontend) vs. TUI vs. static HTML report per run. Web app is the default assumption; the review interaction (span highlighting on page crops) drives this choice.
3. **Retrieval** — start with metadata + numeric-fact lookup and full-text search; add embeddings only if recall demands it. Numbers are searchable strings — semantic search may be optional far longer than expected.
4. **Claim granularity** — are derived numbers (subtotals, ratios) claims with `formula` tolerance rules, or v2? Suggest: formula checks in schema from day one, implementation in v2.
5. **Domain of the first test case** — which real document set to validate against (financial statements? contract terms? board pack figures?). This decides the first parser and schema pressure-test.

## 11. Build order

1. **Schemas first** (`schemas/`) — claim, evidence, verdict, run manifest. Everything else validates against these.
2. **Ingest** — parse master + vault into `index/`, extract normalized numeric facts.
3. **Extract + verify loop** — CLI-only, no UI: `audit verify` producing results and a review queue as files. The repo is already inspectable, so the CLI version is fully usable.
4. **Workbench UI** — the three views over the existing files.
5. **Repair agent + tolerance/formula rules** — once the happy path holds on a real case.

Steps 1–3 are the actual MVP test: if claim extraction and verification don't work at the file level, no UI will save it.
