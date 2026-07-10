# Handoff — Evidence Dossier build (2026-07-10)

Written mid-session for whoever continues (human or agent). Read
`runs/experiments/PROTOCOL.md` for the full experimental history and
`ARCHITECTURE.md` for the system; this file is only the live state.

## What is being built and why

The **Evidence Dossier**: brain/hands split for the Verifier, decided by
Florian after exp-20260709T185721Z showed all variants missing
`prose_table_clash` (finance table 13.51m vs quarterly-update prose
13.26m — nobody searched the prose, and entity_profile's table-only
`conflicts: []` was read as corpus-wide clearance). Three gatherers feed
a prepared dossier of every occurrence of the claim's fact; the Verifier
judges instead of retrieving:

1. graph → table facts (`graph.py`, existed)
2. prose mentions → `mentions.py` (NEW): deterministic sentence/number
   candidates from indexed page spans, gpt-5-nano labels
   metric_phrase/period, quotes are code-extracted verbatim,
   `prose_mentions` + `mention_embeddings` tables, CLI
   `proofbench mentions <audit-id>` (rerun after `proofbench index`)
3. researcher gap-check → one bounded `ask_researcher` call (`rlm.py`)

`dossier.py` (NEW) assembles: occurrences with provenance + `doc_tag` +
`authority_rank` (from audit.yaml `evidence_priority` — first time it's
used) + deterministic `cross_source_conflicts`. `verification.py` got
`dossier=True` (dossier JSON in first user message, `DOSSIER_PROMPT`
judge instructions). `eval.py` got the `dossier` variant AND per-claim
checkpointing (records written immediately; rerun with same
--experiment-id resumes instead of re-paying).

The full approved plan: `/Users/flohiwg/.claude/plans/twinkly-dazzling-twilight.md`.

## State at handoff

- All code above is committed (this commit). Imports verified. The
  `prose_mentions` extraction ran on `audit-2026-q2-vantage`:
  **100 mentions, $0.04, zero junk**, incl. the clash mention
  (value 13260000, metric_phrase "qualified pipeline",
  doc vantage-update-2026q2). NOTE: its period was labeled **2026-Q3**
  (sentence says "entering the next quarter"), so the deterministic
  cross-conflict summary may not pair it with the table's 2026-Q2 value
  — the judge must catch it from the occurrence list. Deliberately not
  "fixed": forcing period=doc-quarter would break genuinely
  forward-looking statements. Watch this in the smoke results.
- **Smoke eval was RUNNING at handoff**: `exp-20260710T062010Z`,
  variant `dossier`, claims 0001, 0009, 0022, 0023, 0027, 0028 on
  audit-2026-q2-vantage. Records land incrementally in
  `runs/experiments/exp-20260710T062010Z/dossier/claim-*.json`.
  Gold (with failure_class per claim): `audits/audit-2026-q2-vantage/gold.yaml`.
  Expected: 0001 supported (control), 0009 supported (narrative_only),
  0022 ambiguous (prose_table_clash — THE fix target), 0023 ambiguous
  (regional_clash), 0027/0028 outdated (stale_quarter — tests the
  restored period-staleness rubric).
- A Sonnet subagent had been driving implementation+validation; if its
  session is gone, everything needed is on disk — do not wait for it.

## Smoke results (exp-20260710T062010Z, read after handoff was first written)

4/6 correct, judge pattern confirmed (0-3 tool calls/claim):
- OK: 0001 supported (3 calls), 0009 supported (3), 0023 ambiguous (2),
  **0027 outdated (2) — the stale_quarter rubric fix works.**
- MISS 0022 (got supported, 0 calls): the dossier DID contain the
  13260000 prose occurrence — recall is solved — but the judge dismissed
  it as a "forward snapshot, different measurement point" because of the
  "entering the next quarter" phrasing (and leaned on authority_rank 1
  of the finance packs). Fix candidate: one general DOSSIER_PROMPT line —
  a quarter-end value phrased as "entering the next quarter" states the
  SAME quarter-end fact, not a different period; treat it as competing.
- FAILED 0028 (error, $0): **code bug in dossier assembly**:
  `'<' not supported between instances of 'NoneType' and 'str'` — some
  sort in dossier.py/mentions.py hits a None key (suspect: a labeled
  mention's period or unit being None/odd type in a sorted()). Reproduce
  with: build_dossier for claim-0028; fix; it's isolated to gathering.

## Post-handoff update — validated 2026-07-10

- The graph `None`-safe sorting fix made a checkpoint-only retry of 0028
  correctly `outdated`, bringing `exp-20260710T062010Z` to 5/6 at an
  accumulated $0.2444.
- A temporal-semantics instruction says that values phrased as
  entering/starting/closing a subsequent period are normally snapshots of
  the just-completed period unless explicitly identified as a forecast,
  target, or later remeasurement. It made the judge recognize the 13.26m
  quarterly-update sentence as Q2 evidence, but the judge initially still
  let authority_rank resolve the conflict (064833Z: 5/6).
- The final clarification distinguishes audit-configured evidence priority
  from a source's explicit reconciliation. A same-fact, same-period
  disagreement is `ambiguous` without an explicit supersession,
  restatement, scope distinction, or reconciliation. The targeted claim
  0022 regression passed (065923Z), then the fresh complete smoke
  `exp-20260710T070219Z` passed **6/6** at $0.3271.
- Full Vantage validation (`exp-20260710T091036Z`, 31 claims) is complete:
  baseline 30/31 ($0.958, 8.26 tools/claim), graph 30/31 with one shape
  failure ($1.044, 7.94), and dossier **31/31, zero failures** ($1.342,
  3.06). Dossier is the only variant to pass both regional-clash claims;
  it makes 63% fewer tool calls than baseline but costs 40% more because
  of its prepared dossier and researcher work. See PROTOCOL.md for the
  class slice and the checkpoint-resume cost detail.
- An expired OpenRouter key created `exp-20260710T083949Z`, a zero-cost
  excluded run (31 baseline and 4 graph failures). It is not a measurement;
  the key was replaced and the valid 091036Z rerun succeeded. This makes
  the pre-flight credential check an immediate reliability task.

## Next steps, in order

1. **Validate dossier on Meridian** (`audit-2026-q1-meridian`) —
   it needs `proofbench mentions audit-2026-q1-meridian` first (and has
   embeddings already). Its smoke set: claims 0001,0018,0021,0024,0027.
2. **Add evaluator credential preflight.** It must fail clearly before
   any claims start when the selected provider/key cannot authenticate;
   `exp-20260710T083949Z` is the regression incident.
3. **PROTOCOL/HANDOFF commit** for this full run (commit style: see recent
   git log; trailer "Co-Authored-By: Claude Fable 5
   <noreply@anthropic.com>").
4. Parked: checklist-agent dataset (waiting on the real checklist).

## Standing constraints (from memory — respect these)

- No fixture-overfitted optimizations; ask "does this help at 100 docs?"
- Delegate mechanical work (running evals, boilerplate from specs) to
  Sonnet subagents; keep judgment in the main session.
- Costs are real now: `llm.py` reprices from token usage at OpenRouter
  prices (the CLI's own numbers are ~120x inflated for non-Claude
  models). `--max-budget-usd` is real dollars. Check credit:
  `curl -s https://openrouter.ai/api/v1/credits -H "Authorization: Bearer $OPENROUTER_API_KEY"`.
- `.env` sets PROOFBENCH_PROVIDER=openrouter; plain python scripts need
  `load_dotenv('.env')`; `proofbench index` wipes derived tables →
  rerun `embed` and `mentions` after it.
