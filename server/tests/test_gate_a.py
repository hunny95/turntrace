"""Gate A smoke tests: config validation and app wiring.

These do not exercise real provider calls (no network); they verify the
backend fails fast on missing credentials and that the FastAPI app exposes
the expected SmallWebRTC offer endpoint without leaking secret values.
"""

from __future__ import annotations

import importlib

import pytest


def test_missing_env_vars_lists_names_only(monkeypatch):
    import config

    # Prevent config's module-level load_dotenv from repopulating the vars
    # we're about to delete (it reads the real server/.env on disk).
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None, raising=False)
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("CARTESIA_API_KEY", raising=False)

    with pytest.raises(RuntimeError) as exc_info:
        config.check_required_env_vars()

    message = str(exc_info.value)
    assert "DEEPGRAM_API_KEY" in message
    assert "GOOGLE_API_KEY" in message
    assert "CARTESIA_API_KEY" in message


def test_present_env_vars_pass_check(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-value")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-value")
    monkeypatch.setenv("CARTESIA_API_KEY", "test-value")

    import config

    config.check_required_env_vars()  # should not raise


def test_app_exposes_offer_endpoint(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-value")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-value")
    monkeypatch.setenv("CARTESIA_API_KEY", "test-value")

    import server

    importlib.reload(server)

    from fastapi.testclient import TestClient

    with TestClient(server.app) as client:
        response = client.get("/api/health")
        assert response.status_code == 200

        # Malformed body should hit Pydantic validation (422), proving the
        # route exists, rather than 404 (route missing).
        response = client.post("/api/offer", json={})
        assert response.status_code == 422
