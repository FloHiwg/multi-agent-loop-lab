# Experiment protocol

Running log of Verifier retrieval experiments: what each run tested, what
changed since the previous one, and what decision it produced. One entry
per `runs/experiments/exp-*/`. Raw per-claim records with full tool traces
live next to each entry's `report.json`; the workbench Eval view renders
them.

**Rules for a comparable entry** (so "what led to which improvement"
stays answerable):

1. **One change under test per experiment.** Name it, and name the commit
   that introduced it. Everything else (model, fixture, claim subset)
   stays fixed relative to the run being compared against.
2. **Fixed claim sets.** Meridian smoke set = claims 0001, 0018, 0021,
   0024, 0027. Full sets = every claim in the audit's gold.yaml. Never
   compare across different subsets.
3. **Model pinned** per experiment (`report.json` records it). All runs so
   far: `z-ai/glm-5.2`.
4. **Failures count against a variant**, but check *why* before reading
   the accuracy number — budget starvation and provider outages produce
   0% rows that measure nothing (see exp-1 and the deleted 2026-07-09
   zai run).
5. Metrics quoted: accuracy (verdict status == gold), failures, avg tool
   calls/claim, avg cost/claim.

Fixtures: **Northstar** (`audit-2025-q4-northstar`, 10 claims) is the
friendly fixture -- claim wording largely matches vault vocabulary.
**Meridian** (`audit-2026-q1-meridian`, 28 claims, gold.yaml) is the
adversarial one -- deliberate renames ("Net sales" vs "Net revenue"),
superseded document versions, cross-doc contradictions.

---

## exp-20260703T123727Z — catalog & alias injection (Northstar, 10 claims)

- **Question:** does pushing the vault's vocabulary into the prompt
  (facts catalog, LLM aliases) cut the vocabulary-mismatch search waste?
- **Change under test:** `catalog` / `catalog_aliases` variants
  (catalog.py, eval harness commit 9331ade).
- **Result:** baseline 8/10, 5.4 tools, $0.089/claim. Catalog variants
  **not measured** -- the $0.80 experiment budget was exhausted by
  baseline + enrichment, and both catalog variants show as 10 failures at
  $0. (Led to commit 03c36f0: skip a whole variant cleanly once budget is
  gone, and to sizing budgets ~20% above the estimate.)
- **Decision:** rethink direction rather than re-run — prompt injection
  (push) was dropped in favor of deepening retrieval itself (pull):
  graph tools first, RLM-style recursion later. Catalog variants remain
  registered for comparison only.

## exp-20260703T145203Z — graph tools smoke (Northstar, 2 claims)

- **Question:** do the new graph tools (`list_entities`,
  `entity_profile`; commit f22f8fd) work end-to-end at all?
- **Result:** 2/2 correct, 5.5 tools. Also surfaced confirmation-waste
  (fixed in c3811b5) and the Verifier JSON-shape failure, later diagnosed
  as a parser bug (4174a06).
- **Decision:** proceed to a full comparison.

## exp-20260706T073809Z + exp-20260706T074153Z — baseline vs graph, twice (Northstar, 10 claims)

- **Question:** does the graph variant beat baseline on the friendly
  fixture? Two identical runs to gauge run-to-run noise.
- **Result:** run 1: baseline 10/10 vs graph 9/10 (tools 5.0 vs 4.6);
  run 2: 9/10 vs 9/10 (4.9 vs 4.8). **Parity, within noise** (~1 claim of
  variance between identical runs).
- **Decision:** Northstar can't discriminate -- its claim wording matches
  vault vocabulary, so there's no gap for the graph to bridge. Built the
  adversarial Meridian fixture (ee760d6) specifically to create that gap.

## exp-20260706T133150Z — baseline vs graph (Meridian smoke set, 5 claims)

- **Question:** same comparison on the adversarial fixture.
- **Result:** baseline 2/5 with 2 failures (8.8 tools, $0.139/claim);
  graph **4/5, 0 failures** (10.2 tools, $0.145/claim).
- **Reading:** graph wins accuracy but *not* efficiency -- traces show
  `entity_profile`'s exact/substring resolution missed on **all five**
  claims (renames like "turnover" aren't substrings), so the agent fell
  back to blind searching after the wasted profile call.
- **Decision:** fix name resolution semantically → entity-name
  embeddings (embeddings.py, `proofbench embed`): top-8
  nearest-entity *suggestions* on a resolution miss, never auto-resolved.

## exp-20260709T122044Z — graph + embedding suggestions (Meridian smoke set, 5 claims)

- **Change under test:** embedding fallback in `entity_profile`
  (embeddings.py). Direct before/after against exp-20260706T133150Z:
  same claims, model, variant.
- **Result:** 4/5, 0 failures, **7.4 tools (was 10.2, −27%)**,
  $0.134/claim (was $0.145).
- **Reading:** the redirect works as designed -- claim-0001 dropped 10→5
  calls (miss → "Net revenue" suggested at rank 1 → exact retry),
  claim-0018 10→6 via the rank-5 suggestion. The one wrong verdict is the
  same claim as before (claim-0024, "turnover") and is **no longer a
  retrieval failure**: both runs read the right v1+v2 figures but
  misjudged the supersession rule (gold `outdated`, model said
  `supported` then `contradicted`).
- **Decision / next:** (a) sharpen the `outdated` rule in the Verifier's
  verdict rubric -- claim-0024 is the regression test; (b) full 28-claim
  Meridian baseline-vs-graph run (~$8 at current rates) to confirm at
  scale.

## exp-20260709T124637Z — outdated rubric + evidence-ready profile (Meridian smoke set, 5 claims)

- **Changes under test (two, deliberately bundled -- both were expected to
  act on different claims):** (a) verdict rubric now defines "outdated" to
  cover superseded document versions/restatements and spells out the
  contradicted/outdated boundary (verification.py); (b) entity_profile
  facts carry verbatim span_text so the agent cites evidence directly
  instead of trailing read_span calls (graph.py).
- **Result:** **5/5 (first perfect smoke run)**, 0 failures, 8.4 tools,
  $0.139/claim.
- **Reading:** claim-0024 flips to correct (`outdated`) -- rubric fix
  confirmed. The intended cheap path works end-to-end on claim-0001:
  profile miss → embedding suggestion → exact retry → cite span_text,
  **3 calls total** (was 10 two runs ago), zero read_span. The avg-tools
  rise (7.4 → 8.4) is entirely claim-0027, the `missing_evidence` claim,
  which ballooned 9 → 15 calls of exhaustive absence-searching; the other
  four claims averaged 6.75 (was 7.0). Absence proofs are now the
  dominant cost and are high-variance between runs.
- **Decision / next:** full 28-claim baseline-vs-graph run to lock the
  scale baseline; then the RLM variant -- bounded absence-scanning by a
  cheap sub-model is exactly the sub-task it exists for. (A prompt-level
  absence shortcut was considered and rejected as fixture-overfitted.)

## exp-20260709T125305Z — baseline vs graph at full scale (Meridian, all 28 claims)

- **Question:** does the graph variant's smoke-set advantage hold across
  the full adversarial fixture?
- **Result:** baseline 27/28, 7.9 tools, $0.106/claim; graph 27/28,
  7.2 tools, $0.109/claim. Zero failures either side.
- **Reading:**
  - **Accuracy parity.** The July 6 smoke-set gap (graph 4/5 vs baseline
    2/5) is explained by since-shared fixes -- the parser fix (baseline's
    2 failures) and the outdated-rubric fix -- not by retrieval. On a
    5-document vault, retrieval architecture does not decide accuracy.
  - **Efficiency is a dollar wash, structured differently:** graph does
    fewer calls on supported claims (5.9 vs 7.4) but pays more tokens per
    call (profile payloads). The scaling argument for graph (blind FTS
    guessing degrades with vault size, entity_profile doesn't) is real
    but unmeasurable at 5 documents.
  - **missing_evidence claims cost 12-14 calls in both variants** -- the
    dominant, high-variance cost class, confirmed at scale.
  - **Shared miss, claim-0022** (supported vs gold ambiguous: finance
    pack 8.4 vs regional review 8.1, no authority ordering): baseline
    under-searched and never saw the 8.1; graph found it and reasoned it
    away with a scope distinction the documents don't state. A judgment
    failure, not retrieval.
- **Decision / next:** (a) conservatism rule in the verdict rubric --
  competing value + no stated authority ordering = ambiguous, don't
  invent reconciliations (aligned with CONCEPT.md's "the system never
  decides alone"; noting the gold label here is itself a judgment call);
  (b) the RLM variant, whose cheap-sub-model scan is aimed at both
  remaining cost/accuracy classes: exhaustive absence proofs and
  exhaustive competing-value sweeps; (c) any further graph-vs-baseline
  claims need a bigger vault to be measurable.

---

## Non-experiment incident log

- **2026-07-09, deleted `exp-20260709T120356Z`:** 5/5 failures at $0 --
  Z.ai account out of balance (provider was `zai` via .env); the SDK
  surfaced it as the misleading "error result: success". Not a
  measurement. Switched `PROOFBENCH_PROVIDER` back to `openrouter`.
  Open TODO: pre-flight credential check in the eval so a dead provider
  aborts immediately instead of producing a 0% row.
