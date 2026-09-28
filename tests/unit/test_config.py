from __future__ import annotations

import json
from pathlib import Path

import pytest

from arxiv_filter.config import SettingsStore, validate_proxy_url


def test_defaults_atomic_save_and_secret_safe_display(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "config.json"
    store = SettingsStore(path)
    assert store.settings.proxy_mode == "system"
    assert store.settings.trust_env is True
    saved = store.update(
        proxy_mode="custom",
        proxy_url="https://alice:top-secret@proxy.example:8443/private?token=do-not-show",
        open_marks_read=True,
    )
    assert path.exists()
    assert saved.effective_proxy == "https://alice:top-secret@proxy.example:8443/private?token=do-not-show"
    assert saved.trust_env is False
    assert saved.public() == {
        "proxy_mode": "custom",
        "proxy_configured": True,
        "proxy_display": "https://***@proxy.example:8443",
        "open_marks_read": True,
    }
    assert "top-secret" not in json.dumps(saved.public())
    assert "do-not-show" not in json.dumps(saved.public())
    assert not list(path.parent.glob("*.tmp"))

    reopened = SettingsStore(path)
    preserved = reopened.update(proxy_mode="system", proxy_url=None, open_marks_read=False)
    assert preserved.proxy_url == saved.proxy_url
    assert preserved.trust_env is True


def test_corrupt_configuration_falls_back_without_overwriting_source(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    original = b"{broken json"
    path.write_bytes(original)
    store = SettingsStore(path)
    assert store.settings.proxy_mode == "system"
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "value",
    [
        "ftp://proxy.example",
        "http:///missing-host",
        "http://proxy.example:70000",
        "http://proxy.example/#fragment",
        "http://proxy.example/with space",
        "http://proxy.example\x7f",
    ],
)
def test_proxy_url_rejects_unsupported_or_ambiguous_values(value: str) -> None:
    with pytest.raises(ValueError):
        validate_proxy_url(value)


def test_custom_mode_requires_existing_or_new_proxy_url(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "config.json")
    with pytest.raises(ValueError, match="需要提供"):
        store.update(proxy_mode="custom", proxy_url=None, open_marks_read=False)
    assert store.settings.proxy_mode == "system"
