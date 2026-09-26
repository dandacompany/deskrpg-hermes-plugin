"""Shared fixtures for the review-hook routes: an approval store in a temp dir and the hooks capability on."""

import pytest

from deskrpg_plugin import contract_fields
from deskrpg_plugin import review_store as rs


@pytest.fixture
def store(tmp_path, monkeypatch):
    """The approval store every route and hook opens during the test (`DESKRPG_SHARED_DIR`)."""
    monkeypatch.setenv("DESKRPG_SHARED_DIR", str(tmp_path / "shared"))
    conn = rs.open_store(rs.sidecar_path(api=None))
    yield conn
    conn.close()


@pytest.fixture
def hooks_on(monkeypatch):
    """Hermes without the policy core patch, with the review hooks available."""
    monkeypatch.setattr(contract_fields, "has_review_hooks", lambda api: True, raising=False)
    monkeypatch.setattr(contract_fields, "has_review_policy", lambda api: False)
