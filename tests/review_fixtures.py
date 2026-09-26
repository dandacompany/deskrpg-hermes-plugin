"""Shared fixtures for the review-hook routes: an approval store in a temp dir and the hooks capability on.

The real `deskrpg_plugin.review_store` is built in parallel; until it exists, the plan's interface stand-in
(`tests/fakes_review_store.py`) is installed under its name, so the route code imports the same module either way.
"""

import importlib
import importlib.util
import sys

import pytest

from deskrpg_plugin import contract_fields


def _review_store_module():
    if importlib.util.find_spec("deskrpg_plugin.review_store") is not None:
        return importlib.import_module("deskrpg_plugin.review_store")
    from tests import fakes_review_store

    sys.modules["deskrpg_plugin.review_store"] = fakes_review_store
    import deskrpg_plugin

    deskrpg_plugin.review_store = fakes_review_store
    return fakes_review_store


rs = _review_store_module()


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
