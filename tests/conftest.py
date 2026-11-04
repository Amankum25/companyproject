"""
tests/conftest.py — Shared pytest fixtures and configuration.
"""

import pytest


@pytest.fixture(autouse=True)
def no_real_credentials(monkeypatch):
    """
    Prevent any test from accidentally loading real credentials.
    
    This fixture is autouse=True — it runs for every test automatically.
    It overrides the environment variables with dummy values so tests
    never accidentally read real API keys from .env.
    """
    monkeypatch.setenv("BYBIT_TESTNET_API_KEY", "test_key_dummy_do_not_use")
    monkeypatch.setenv("BYBIT_TESTNET_API_SECRET", "test_secret_dummy_do_not_use")

