"""Tests for the provider-credential preflight check (MUL-6).

Regression coverage for the incident that motivated it: an expired provider
API key was present (env var set) but rejected by the provider, and the old
code only checked presence -- so verification/eval started anyway and
produced a partially populated experiment full of zero-cost failures.
"""

from __future__ import annotations

import httpx
import pytest

from proofbench import preflight

ALL_CREDENTIAL_ENV_VARS = ["OPENROUTER_API_KEY", "ZAI_API_KEY", "Z_AI", "ANTHROPIC_API_KEY", "PROOFBENCH_PROVIDER"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Every test starts with no provider credentials set, so each one
    controls its own provider/credential combination explicitly."""
    for var in ALL_CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


_RealAsyncClient = httpx.AsyncClient


def _mock_async_client(handler):
    """A drop-in replacement for httpx.AsyncClient that always talks to a
    MockTransport, so no test makes a real network call."""

    def factory(*args, **kwargs):
        return _RealAsyncClient(transport=httpx.MockTransport(handler))

    return factory


async def test_missing_credentials_raises(monkeypatch):
    # No provider env vars set at all -- resolve_provider() falls back to
    # "anthropic", and ANTHROPIC_API_KEY is unset too.
    with pytest.raises(preflight.PreflightError, match="No credentials found"):
        await preflight.check_credentials_async()


@pytest.mark.parametrize(
    "provider,env,expected_url_fragment",
    [
        ("openrouter", {"OPENROUTER_API_KEY": "sk-or-good"}, "openrouter.ai"),
        ("zai", {"ZAI_API_KEY": "zai-good"}, "api.z.ai"),
        ("anthropic", {"ANTHROPIC_API_KEY": "sk-ant-good"}, "api.anthropic.com"),
    ],
)
async def test_accepted_credentials_pass(monkeypatch, provider, env, expected_url_fragment):
    for var, value in env.items():
        monkeypatch.setenv(var, value)

    def handler(request: httpx.Request) -> httpx.Response:
        assert expected_url_fragment in str(request.url)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(preflight.httpx, "AsyncClient", _mock_async_client(handler))

    await preflight.check_credentials_async()  # must not raise


async def test_expired_key_rejected(monkeypatch):
    """The exact regression case: OPENROUTER_API_KEY is set (present) but
    the provider rejects it as expired -- preflight must still fail fast."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-expired")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "expired API key"})

    monkeypatch.setattr(preflight.httpx, "AsyncClient", _mock_async_client(handler))

    with pytest.raises(preflight.PreflightError, match="rejected the OPENROUTER_API_KEY credential"):
        await preflight.check_credentials_async()


async def test_forbidden_key_rejected(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-revoked")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"})

    monkeypatch.setattr(preflight.httpx, "AsyncClient", _mock_async_client(handler))

    with pytest.raises(preflight.PreflightError, match="rejected the ANTHROPIC_API_KEY credential"):
        await preflight.check_credentials_async()


async def test_provider_error_response_raises(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-good")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    monkeypatch.setattr(preflight.httpx, "AsyncClient", _mock_async_client(handler))

    with pytest.raises(preflight.PreflightError, match="preflight check failed"):
        await preflight.check_credentials_async()


async def test_network_failure_raises_preflight_error(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-good")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(preflight.httpx, "AsyncClient", _mock_async_client(handler))

    with pytest.raises(preflight.PreflightError, match="Could not reach provider"):
        await preflight.check_credentials_async()


async def test_zai_credential_falls_back_to_z_ai_env_var(monkeypatch):
    monkeypatch.setenv("Z_AI", "zai-good")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(preflight.httpx, "AsyncClient", _mock_async_client(handler))

    await preflight.check_credentials_async()  # must not raise


async def test_explicit_provider_override_is_honored(monkeypatch):
    monkeypatch.setenv("PROOFBENCH_PROVIDER", "anthropic")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-good")  # would win without the override

    with pytest.raises(preflight.PreflightError, match="No credentials found for provider 'anthropic'"):
        await preflight.check_credentials_async()
