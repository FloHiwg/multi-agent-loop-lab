"""Thin wrapper around the Claude Agent SDK for one-shot, tool-free agent calls.

Every Proofbench agent (Claim Extractor, Verifier) is a single stateless
query: fixed system prompt in, JSON text out, parsed and validated by the
caller against a Pydantic model. None of them get file or shell tools --
CONCEPT.md's bounded-worker design means these agents read what they're
handed and return typed output, nothing else.
"""

from __future__ import annotations

import os

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

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


async def run_agent(system_prompt: str, user_prompt: str, *, model: str | None = None) -> str:
    """Send one stateless prompt to Claude and return the concatenated text reply.

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
        allowed_tools=[],
        permission_mode="bypassPermissions",
        model=resolve_model(model),
        env=env_overrides or {},
    )

    chunks: list[str] = []
    async for message in query(prompt=user_prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
    return "".join(chunks)
