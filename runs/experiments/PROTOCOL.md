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

## exp-20260709T131021Z / exp-20260709T131236Z / exp-20260709T131627Z — the claim-0022 conflict series (Meridian, claims 0001/0021/0022/0024)

Three-run iteration on the full run's shared miss (claim-0022: finance
pack 8.4 vs regional review 8.1, gold `ambiguous`). Guards in every run:
0001 must stay supported, 0021 ambiguous, 0024 outdated.

- **Run 1 (131021Z): conservatism rubric rule v1** ("competing value + no
  stated authority ordering = ambiguous, don't invent reconciliations").
  3/4 -- guards held, 0022 still supported: the model used the rule's
  "stated scope difference" escape hatch, reading the regional doc's
  different *methodology* as a scope difference.
- **Run 2 (131236Z): rule v2** (methodology is not scope; "not ranked
  against X" IS the no-ordering case). 2/3 -- 0022 still supported: the
  model quoted the disclaimer and inverted it ("not ranked → not
  comparable → not competing"). Conclusion: prompt wording alone cannot
  reliably force conflict recognition; stop iterating prompts.
- **Run 3 (131627Z): structural fix.** entity_profile now returns (a) a
  deterministic `conflicts` list -- same entity, same normalized
  period/role, materially different values across documents (the query
  period/role normalization was built for) -- and (b) `see_also`,
  near-name entities from stored embeddings (offline, no query call), so
  entity splits like "Qualified pipeline" vs "Qualified commercial
  pipeline" can't hide a cross-document conflict. **4/4**, and cheaper:
  0022 in 4 calls, run avg 5.8 tools.
- **Decision:** keep rule v2 (it's principled) but treat the structural
  conflict surface as the real mechanism: the tool computes the conflict,
  the model only judges it. Generalizes to any vault size; nothing in it
  is fixture-specific.

## exp-20260709T182802Z — first rlm variant run (Meridian, claims 0001/0022/0027/0028)

- **Change under test:** the `rlm` variant (rlm.py): graph tools plus
  `ask_researcher`, a depth-1 sub-agent on a cheap model
  (openai/gpt-5-nano, ~$0.003 real per question) for exhaustive sweeps.
  Sub-model selection notes: glm-4.7-flash disqualified (36-call flail,
  wrong sweep); qwen3.5-flash disqualified (fabricated a "searched" list
  with zero tool calls -- hallucinated diligence). gpt-5-nano probed
  honest and cheap. Guards: turn cap (12), CLI-dollar budget cap, and a
  zero-tool-call warning stamped on any effort-free report.
- **Result:** rlm 4/4 ($0.019/claim real), graph 3/4 ($0.023/claim real).
- **Reading, honestly:**
  - The Verifier delegated ONCE in four claims (claim-0028). The rlm
    accuracy win is not attributable to delegation; on this vault size
    glm-5.2 simply does the sweeps itself. The researcher's value
    hypothesis remains untested at fixture scale -- consistent with the
    "does this help at 100 documents?" test, it should be re-measured on
    a bigger vault rather than prompt-forced into use here.
  - **claim-0022 is unstable**: correct under graph in 131627Z, wrong
    again here (supported, 6 calls) -- the conflicts/see_also surface
    helps but glm-5.2's conflict judgment still flips between runs.
    Needs either repeated-run measurement or a stronger judge on
    conflict claims.
- **Decision / next:** keep rlm registered; before investing further,
  (a) quantify run-to-run variance (same variant, same claims, 3+ runs),
  (b) build or synthesize a larger vault where sweeps genuinely exceed
  what the Verifier can do itself.

## exp-20260709T185214Z — Vantage fixture first smoke (graph, claims 0001/0009/0020/0022/0029)

- **Change under test:** the scaled Vantage fixture
  (`scripts/generate_vantage_fixture.py`, `audit-2026-q2-vantage`): 41
  vault docs over 5 quarters from a seeded world model, with two
  prose-only document families -- narrative evidence is indexed
  page-level in spans_fts but never enters the facts graph. Every gold
  entry carries a `failure_class` tag (11 classes, 31 claims).
- **Result:** 3/5, 1 failure. Per class: table_vocab OK; **narrative_only
  OK in 3 calls** (prose evidence retrieval works); contradicted_prose OK;
  **prose_table_clash MISS** (supported vs gold ambiguous -- the
  deterministic conflict surface can't see prose values, so the
  update-vs-finance-table clash went unnoticed in 3 calls);
  absent_near_name FAILED on a verdict missing its "rationale" field,
  which escaped as a bare KeyError and discarded the trace (now fixed:
  field-level validation raises trace-preserving VerifyClaimError).
- **Reading:** the fixture discriminates exactly where intended --
  prose/table conflicts are invisible to the graph's deterministic
  conflict detection and need an exhaustive sweep, i.e. the researcher's
  job. This is the vault where the rlm variant's hypothesis is actually
  testable.
- **Decision / next:** full 31-claim baseline vs graph vs rlm comparison
  on Vantage, sliced by failure_class.

## exp-20260709T185721Z — baseline vs graph vs rlm at Vantage scale (31 claims, 41 docs)

- **Question:** does the graph/rlm machinery finally separate from
  baseline on a 41-doc vault with narrative evidence?
- **Result:** three-way tie at **28/31 (90%)** each. Tools 8.3 / 7.1 /
  6.7 (baseline/graph/rlm); real cost $0.50/$0.53/$0.54 per variant,
  $1.57 total.
- **Reading, by class:**
  - **Narrative evidence is a solved retrieval case for all variants:**
    narrative_only 4/4 and contradicted_prose 2/2 across the board --
    page-level FTS is enough to find and judge prose values.
  - **prose_table_clash is the universal blind spot (0/1 in all three).**
    Every rationale says "no source states a different value" -- nobody
    read the quarterly update's conflicting prose figure. Root cause
    identified: entity_profile's `conflicts: []` covers TABLE facts only,
    but the models treat it as corpus-wide clearance, so the prose
    disagreement is never looked for. The deterministic conflict surface
    created false confidence outside its coverage.
  - **stale_quarter misses are a rubric regression, not capability:** all
    variants say `contradicted` where gold says `outdated` for
    prior-quarter values asserted as current. The pre-2026-07-09 rubric
    covered "different, superseded period"; the rewrite emphasized
    version supersession and lost the period-staleness case.
  - **rlm delegated 3 of 31 claims** (all absence-type questions, all
    answered correctly), used the fewest tool calls of the three, and had
    the run's one hard failure (a bare-verdict shape slip, 1 in 93).
  - **The graph scaling hypothesis is still unconfirmed at 41 docs**:
    baseline FTS ties on accuracy; graph/rlm are ~15-20% leaner on calls
    but pay it back in payload tokens. Document count alone may not be
    the scaling axis that separates them -- vocabulary diversity and
    corpus size beyond FTS snippet quality might be.
- **Decision / next:** (a) scope-caveat the conflicts list in the graph
  prompt (tables only; one search_vault sweep before "supported") --
  general, not fixture-specific; (b) restore period-staleness to the
  outdated rubric; (c) per-claim checkpointing in eval.py (mid-variant
  crash currently loses finished claims); re-measure after (a)+(b).

## exp-20260710T062010Z / 064833Z / 065923Z / 070219Z — Evidence Dossier smoke series (Vantage, 6 claims)

- **Question:** does a prepared cross-source dossier eliminate the
  prose-table conflict blind spot without regressing narrative, regional,
  or stale-quarter judgments?
- **Changes under test:** prose mentions plus table facts and a bounded
  researcher sweep feed the new `dossier` judge mode. After the initial
  crash retry, the first completed smoke (062010Z) reached 5/6: the
  prose-table clash was found but dismissed as a forward-period snapshot.
  The first temporal-semantics instruction (064833Z) correctly made the
  judge treat "entering the next quarter" as the completed-quarter
  boundary, but it still returned `supported` by treating the configured
  `authority_rank` as automatic reconciliation. The final instruction
  distinguishes evidence priority from a source's explicit reconciliation:
  a same-fact, same-period disagreement remains ambiguous absent an
  explicit supersession, restatement, scope distinction, or reconciliation.
- **Result:** targeted regression claim 0022 passed 1/1 in 065923Z.
  The final fresh six-claim smoke (070219Z) was **6/6, zero failures, 3.2
  tools/claim, $0.327 real**: 0001 and 0009 supported; 0022 and 0023
  ambiguous; 0027 and 0028 outdated.
- **Reading:** the dossier solved retrieval recall; the required judgment
  rule has two parts. Quarter-boundary prose is evidence of the completed
  period, and configured source priority guides scrutiny but is not itself
  documentary proof that a conflicting value was superseded. The full
  smoke confirms neither clarification regressed the existing control,
  regional-conflict, or stale-period cases.
- **Decision / next:** run the full Vantage baseline-vs-graph-vs-dossier
  comparison, sliced by `failure_class`, before drawing a scale conclusion.

---

## Non-experiment incident log

- **2026-07-09, cost accounting was inflated ~120x for OpenRouter
  models:** the Claude Code runtime prices models it doesn't know at its
  default (Claude-level) rates -- measured CLI-reported $0.144 vs real
  OpenRouter spend $0.0012 for one gpt-5-nano session. **Every
  experiment cost figure before exp-20260709T182802Z is a CLI estimate,
  not dollars** (same-model comparisons stay valid proportionally; the
  real full-28-claim run cost was ~$0.55, not ~$3). Fixed in llm.py:
  run_agent now reprices from token usage at the model's actual
  OpenRouter prices (cache tokens at worst-case prompt rate when no
  cache price is listed, so it's a tight upper bound). Per-call
  max_budget_usd enforcement inside the runtime still uses CLI dollars.
- **2026-07-09, deleted `exp-20260709T120356Z`:** 5/5 failures at $0 --
  Z.ai account out of balance (provider was `zai` via .env); the SDK
  surfaced it as the misleading "error result: success". Not a
  measurement. Switched `PROOFBENCH_PROVIDER` back to `openrouter`.
  Open TODO: pre-flight credential check in the eval so a dead provider
  aborts immediately instead of producing a 0% row.
