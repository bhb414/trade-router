from __future__ import annotations

import os

import pytest

from trade_router.adapters.utils import redact_text
from trade_router.core.secrets import DpapiSecretStore


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI only")
def test_dpapi_store_round_trip_without_plaintext(tmp_path):
    path = tmp_path / "secrets.dpapi"
    store = DpapiSecretStore(path)
    store.set(
        "profile:test", {"app_key": "public-key", "app_secret": "never-write-this"}
    )
    assert store.get("profile:test")["app_secret"] == "never-write-this"
    raw = path.read_bytes()
    assert b"never-write-this" not in raw
    assert b"public-key" not in raw


def test_diagnostic_text_redacts_credentials_and_account_numbers():
    text = redact_text(
        "request app-secret for 12345678-01 failed",
        {"app_secret": "app-secret"},
        "12345678-01",
    )
    assert "app-secret" not in text
    assert "12345678-01" not in text
    assert text.count("<redacted>") == 2
