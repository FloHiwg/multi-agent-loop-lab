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

---

## Non-experiment incident log

- **2026-07-09, deleted `exp-20260709T120356Z`:** 5/5 failures at $0 --
  Z.ai account out of balance (provider was `zai` via .env); the SDK
  surfaced it as the misleading "error result: success". Not a
  measurement. Switched `PROOFBENCH_PROVIDER` back to `openrouter`.
  Open TODO: pre-flight credential check in the eval so a dead provider
  aborts immediately instead of producing a 0% row.
