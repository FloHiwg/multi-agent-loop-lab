"""RLM-style recursive verification: a cheap researcher sub-agent under a
strong composing Verifier.

The full Meridian run (PROTOCOL.md, exp-20260709T125305Z) left two cost/
accuracy classes that are *exhaustive-sweep* shaped: absence proofs
(missing_evidence claims at 12-14 calls of search-phrasing iteration) and
competing-value sweeps (baseline under-searched claim-0022; graph
shortcut it). Sweeps need diligence, not judgment -- so they go to a
much cheaper model, while the strong model keeps decomposition and the
verdict.

Shape (depth 1, per ARCHITECTURE.md's scoping):

- The top-level Verifier (the normal model) gets one extra tool,
  `ask_researcher(question)`.
- Each call runs ONE bounded sub-agent session on the sub-model
  (PROOFBENCH_SUB_MODEL, default z-ai/glm-4.7-flash -- same family as the
  default main model, ~10x cheaper) with the same read-only vault tools,
  graph tools included, and a hard per-question budget cap enforced by
  the runtime (run_agent's max_budget_usd).
- The researcher answers the one question with verbatim span citations
  and NEVER issues verdicts; composition stays above.

Trust posture: the researcher has the same read-only, kind-scoped tools
as the Verifier -- it can relay what the deterministic index says but
cannot alter facts or reach outside the vault. Its spans are relayed
LLM output, though: the top-level prompt tells the Verifier to ground
any span it cites as final evidence via its own tools when the verdict
hinges on it.
"""

from __future__ import annotations

import os

from claude_agent_sdk import SdkMcpTool, tool

from proofbench.jsonutil import extract_json
from proofbench.llm import run_agent
from proofbench.tools import _tool_result, allowed_tool_names, build_server

DEFAULT_SUB_MODEL = "openai/gpt-5-nano"
# NOTE: budgets are in *CLI-estimated* dollars, which the Claude Code
# runtime prices at its default model rates -- measured ~120x above real
# OpenRouter spend for cheap sub-models (see PROTOCOL.md incident log,
# 2026-07-09). This cap is runaway protection; max_turns is the real bound.
DEFAULT_QUESTION_BUDGET_USD = 0.60
# The primary bound: small models flail in open-ended tool loops (the first
# glm-4.7-flash probe burned 36 calls re-searching spans it had already
# read). A hard turn cap forces answering from what's been gathered.
RESEARCHER_MAX_TURNS = 12
RESEARCH_SERVER_NAME = "proofbench_research"


def resolve_sub_model() -> str:
    return os.environ.get("PROOFBENCH_SUB_MODEL", DEFAULT_SUB_MODEL)


RESEARCHER_SYSTEM_PROMPT = """\
You are a research assistant inside Proofbench, an audit workbench. You are
given ONE narrow factual question about a vault of basis documents, and
read-only tools to answer it:
- entity_profile / list_entities: the facts graph (values, periods,
  conflicts, similarly-named entities)
- search_facts / search_vault / read_span: structured and full-text search

Answer ONLY the question asked. Never render audit verdicts or opinions on
whether a claim is supported -- that is someone else's job.

Rules:
- Cite everything: each statement of fact must carry doc_id, location, and
  the verbatim span text it comes from.
- For sweep questions ("every value any document states for X"), start with
  entity_profile of the metric, then profile EVERY entity in its "see_also"
  list -- near-name entities often carry the same metric under different
  wording -- and report every occurrence, especially ones that disagree.
  The profile's "conflicts" list is already a cross-document disagreement
  report: include it.
- For absence questions ("is there any document that mentions X?"), be
  exhaustive: check list_entities for the vocabulary, then search several
  phrasings and plausible synonyms before answering "nothing found", and
  list what you searched.
- You have a hard turn limit. Do not repeat a search whose answer you
  already have; when the limit nears, answer from what you gathered.

Respond with ONLY a JSON object, no prose, no markdown fences:
{"answer": "<direct answer in 1-3 sentences>",
 "spans": [{"doc_id": "...", "location": "...", "span_text": "..."}],
 "searched": ["<query or entity you tried>", ...]}
"""


RLM_PROMPT = """\

You additionally have a researcher -- a faster assistant with the same
read-only vault tools:
- ask_researcher: give it ONE narrow factual question; it returns a JSON
  report with cited spans and what it searched.
Delegate legwork, keep judgment:
- Absence sweeps ("does any vault document mention X under any phrasing?")
  and competing-value sweeps ("every value any document states for X in
  period Y") are researcher work -- one question each, instead of running
  many searches yourself.
- Never delegate the verdict, the comparison rules, or conflict
  resolution. The researcher reports; you decide.
- The researcher is a smaller model: treat its report as leads. If your
  verdict hinges on a span it cited, confirm that span with your own tools
  before citing it as evidence; its "nothing found" is trustworthy only if
  its "searched" list looks genuinely exhaustive.
- At most 3 researcher questions per claim.
"""


def ask_researcher_tool(
    audit_id: str,
    kind: str,
    *,
    sub_model: str | None = None,
    question_budget_usd: float = DEFAULT_QUESTION_BUDGET_USD,
    sub_costs: list[float] | None = None,
) -> SdkMcpTool:
    """Build the ask_researcher tool. `sub_costs` (if given) accumulates the
    cost of every sub-agent session, so the caller can fold researcher spend
    into the claim's cost for budget enforcement and reporting."""
    resolved_sub_model = sub_model or resolve_sub_model()

    @tool(
        "ask_researcher",
        "Delegate ONE narrow factual question about the vault to a fast research "
        "assistant with the same read-only search tools. Best for exhaustive "
        "sweeps: 'does any document mention X under any phrasing?' or 'list every "
        "value any document states for X in period Y'. Returns a JSON report with "
        "cited spans and the queries it tried. It never issues verdicts.",
        {
            "type": "object",
            "properties": {"question": {"type": "string", "description": "One narrow factual question"}},
            "required": ["question"],
        },
    )
    async def ask_researcher(args: dict) -> dict:
        server = build_server(audit_id, kind, RESEARCH_SERVER_NAME, include_graph_tools=True)
        try:
            reply = await run_agent(
                RESEARCHER_SYSTEM_PROMPT,
                f"QUESTION:\n{args['question']}",
                model=resolved_sub_model,
                mcp_servers={RESEARCH_SERVER_NAME: server},
                allowed_tools=allowed_tool_names(RESEARCH_SERVER_NAME, graph_tools=True),
                max_budget_usd=question_budget_usd,
                max_turns=RESEARCHER_MAX_TURNS,
            )
        except Exception as e:
            # A failed sub-session must not fail the claim: the Verifier can
            # always fall back to doing the sweep itself.
            return _tool_result({"error": f"researcher unavailable: {e}"})
        if sub_costs is not None and reply.cost_usd is not None:
            sub_costs.append(reply.cost_usd)
        report = extract_json(reply.text)
        payload = report if isinstance(report, dict) else {"answer": reply.text}
        payload["researcher_tool_calls"] = len(reply.tool_trace)
        if not reply.tool_trace:
            # A report without a single tool call is fabricated diligence
            # (observed: a sub-model returning a fully populated "searched"
            # list having searched nothing). Flag it so the Verifier
            # discards it rather than trusting a hallucinated sweep.
            payload["warning"] = (
                "researcher made ZERO tool calls -- this report is not based on "
                "the vault; disregard it and investigate yourself"
            )
        return _tool_result(payload)

    return ask_researcher
