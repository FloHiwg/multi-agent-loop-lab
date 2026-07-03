"""Thin wrapper around the Claude Agent SDK for bounded, tool-scoped agent calls.

Every Proofbench agent (Claim Extractor, Verifier) is one stateless
query() session: fixed system prompt in, JSON text out, parsed and
validated by the caller against a Pydantic model. They may be handed a
small set of read-only search/read tools scoped to one document `kind`
(see tools.py) -- never file or shell tools. The tool-execution loop
(calling a tool, feeding the result back, deciding the next step) is
handled entirely by the Claude Code runtime underneath query(); this
wrapper just waits for the final turn's text.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    McpSdkServerConfig,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

# Pinned so every run is reproducible (see RunManifest.model): an unpinned
# "CLI default" model can silently change out from under old runs, breaking
# CONCEPT.md's replayability success criterion. Override with PROOFBENCH_MODEL.
#
# z-ai/glm-5.2 over OpenRouter: ~10x cheaper than claude-sonnet-5 on this
# workload (measured ~$0.018 vs ~$0.18 per call) with no observed accuracy
# loss on the Northstar fixture. Requires OPENROUTER_API_KEY -- a direct
# ANTHROPIC_API_KEY setup can't reach non-Anthropic models this way.
DEFAULT_MODEL = "z-ai/glm-5.2"


def resolve_model(model: str | None = None) -> str:
    return model or os.environ.get("PROOFBENCH_MODEL", DEFAULT_MODEL)


def _openrouter_env() -> dict[str, str] | None:
    """Build the env overrides needed to route the Agent SDK through OpenRouter's
    Anthropic-compatible endpoint (see openrouter.ai/docs/guides/community/anthropic-agent-sdk).
    Returns None if OPENROUTER_API_KEY isn't set, so a direct Anthropic key
    (already in the ambient environment) is used unmodified instead.
    """
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return None
    return {
        "ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
        "ANTHROPIC_AUTH_TOKEN": key,
        "ANTHROPIC_API_KEY": "",
    }


@dataclass
class AgentReply:
    text: str
    cost_usd: float | None
    tool_trace: list[dict] = field(default_factory=list)
    """One entry per tool call this session made, in call order: {seq, at,
    tool, input, output, is_error}. This is what RunManifest.tool_trace
    stores -- the workbench UI's whole reason for existing is to make this
    visible, not just the final claim/verdict."""


async def run_agent(
    system_prompt: str,
    user_prompt: str,
    *,
    model: str | None = None,
    mcp_servers: dict[str, McpSdkServerConfig] | None = None,
    allowed_tools: list[str] | None = None,
    max_budget_usd: float | None = None,
) -> AgentReply:
    """Run one bounded agent session and return the final turn's text reply plus its cost.

    With no mcp_servers/allowed_tools, this is a plain one-shot call (no
    tools granted). With them, the model may call tools across several
    turns before its final answer -- only the last AssistantMessage's text
    is returned, since intermediate turns may carry commentary alongside
    tool calls rather than the final JSON answer.

    max_budget_usd is a per-call hard cap enforced by the Claude Code
    runtime itself (defense in depth under the Manager's run-level budget,
    see manager.py).

    Raises RuntimeError if no credentials (OPENROUTER_API_KEY or an ambient
    ANTHROPIC_API_KEY) are available.
    """
    env_overrides = _openrouter_env()
    if env_overrides is None and not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "No LLM credentials found. Set OPENROUTER_API_KEY (routed via OpenRouter's "
            "Anthropic-compatible endpoint) or ANTHROPIC_API_KEY (direct Anthropic access)."
        )

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        allowed_tools=allowed_tools or [],
        mcp_servers=mcp_servers or {},
        permission_mode="bypassPermissions",
        model=resolve_model(model),
        env=env_overrides or {},
        max_budget_usd=max_budget_usd,
    )

    final_text = ""
    cost_usd: float | None = None
    trace: list[dict] = []
    pending_by_id: dict[str, dict] = {}
    seq = 0

    async for message in query(prompt=user_prompt, options=options):
        if isinstance(message, AssistantMessage):
            texts = [block.text for block in message.content if isinstance(block, TextBlock)]
            if texts:
                final_text = "".join(texts)
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    seq += 1
                    entry = {
                        "seq": seq,
                        "at": datetime.now(timezone.utc).isoformat(),
                        "tool": block.name,
                        "input": block.input,
                        "output": None,
                        "is_error": None,
                    }
                    trace.append(entry)
                    pending_by_id[block.id] = entry
        elif isinstance(message, UserMessage):
            for block in message.content if isinstance(message.content, list) else []:
                if isinstance(block, ToolResultBlock):
                    entry = pending_by_id.get(block.tool_use_id)
                    if entry is not None:
                        entry["output"] = block.content
                        entry["is_error"] = block.is_error
        elif isinstance(message, ResultMessage):
            cost_usd = message.total_cost_usd

    return AgentReply(text=final_text, cost_usd=cost_usd, tool_trace=trace)
