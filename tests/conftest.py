import pytest


@pytest.fixture(autouse=True)
def isolate_web_credentials_from_local_runtime(monkeypatch):
    """Keep a developer's production Basic Auth secrets out of Web UI tests."""
    monkeypatch.delenv("VN_WEB_USERNAME", raising=False)
    monkeypatch.delenv("VN_WEB_PASSWORD", raising=False)
