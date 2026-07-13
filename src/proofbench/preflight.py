"""Preflight credential check for `proofbench verify` and `proofbench eval`.

An expired provider API key once let an evaluation begin anyway, producing a
partially populated experiment full of zero-cost agent failures instead of a
valid measurement (MUL-6). This check must run before any verifier,
researcher, or claim work is scheduled: it confirms the resolved provider's
credential is both present and accepted by the provider itself -- presence
alone (an env var being set) doesn't catch an expired or revoked key.
"""

from __future__ import annotations

import asyncio
import os

import httpx

from proofbench.llm import _zai_key, resolve_provider

ANTHROPIC_VERSION = "2023-06-01"


class PreflightError(RuntimeError):
    """Raised when the resolved provider's credentials are missing or rejected."""


def _credential(provider: str) -> tuple[str, str] | None:
    """(env var name shown in errors, key value) for `provider`, or None if unset."""
    if provider == "openrouter":
        key = os.environ.get("OPENROUTER_API_KEY")
        return ("OPENROUTER_API_KEY", key) if key else None
    if provider == "zai":
        key = _zai_key()
        var = "ZAI_API_KEY" if os.environ.get("ZAI_API_KEY") else "Z_AI"
        return (var, key) if key else None
    if provider == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY")
        return ("ANTHROPIC_API_KEY", key) if key else None
    raise PreflightError(f"unknown provider: {provider!r} (expected openrouter, zai, or anthropic)")


def _validation_request(provider: str, key: str) -> tuple[str, dict[str, str]]:
    """(url, headers) for a lightweight authenticated GET that the provider
    will reject with 401/403 if the key is invalid or expired, and accept
    (2xx) if it's live -- without spending on a real model call."""
    if provider == "openrouter":
        return "https://openrouter.ai/api/v1/key", {"Authorization": f"Bearer {key}"}
    headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
    if provider == "zai":
        return "https://api.z.ai/api/anthropic/v1/models", headers
    return "https://api.anthropic.com/v1/models", headers


async def check_credentials_async(*, timeout: float = 15.0) -> None:
    """Raise PreflightError if the resolved provider's credentials are
    missing or rejected. No-op (returns) if they're accepted."""
    provider = resolve_provider()
    credential = _credential(provider)
    if credential is None:
        var_hint = {
            "openrouter": "OPENROUTER_API_KEY",
            "zai": "ZAI_API_KEY (or Z_AI)",
            "anthropic": "ANTHROPIC_API_KEY",
        }[provider]
        raise PreflightError(f"No credentials found for provider {provider!r}. Set {var_hint}.")
    var, key = credential

    url, headers = _validation_request(provider, key)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as e:
        raise PreflightError(
            f"Could not reach provider {provider!r} to validate {var}: {e}"
        ) from e

    if resp.status_code in (401, 403):
        raise PreflightError(
            f"Provider {provider!r} rejected the {var} credential (HTTP {resp.status_code}). "
            "The key may be expired, revoked, or invalid -- fix it before retrying."
        )
    if resp.status_code >= 400:
        raise PreflightError(
            f"Provider {provider!r} preflight check failed validating {var} "
            f"(HTTP {resp.status_code}): {resp.text[:200]}"
        )


def check_credentials(*, timeout: float = 15.0) -> None:
    asyncio.run(check_credentials_async(timeout=timeout))
