"""PyYAML reaches the plugin on the installed Hermes, whichever way that Hermes provides it.

A Hermes whose core still requires PyYAML supplies it itself. A Hermes that reads plugin dependency declarations
(upstream `pm.plugin_declarations`) must see ours, so enabling the plugin installs it.
"""

import importlib.util
import pathlib
import re
from importlib.metadata import requires

import pytest

pytestmark = pytest.mark.integration

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _core_requires_pyyaml() -> bool:
    names = (re.split(r"[\s\[<>=!~;]", req, maxsplit=1)[0].lower() for req in requires("hermes-agent") or [])
    return "pyyaml" in names


def test_pyyaml_is_provided_by_the_core_or_by_our_declaration():
    if importlib.util.find_spec("pm") is not None and importlib.util.find_spec("pm.plugin_declarations") is not None:
        from pm.plugin_declarations import read_python_declaration

        declaration = read_python_declaration(ROOT)
        assert any(req.lower().startswith("pyyaml") for req in declaration.requirements), declaration
        return
    assert _core_requires_pyyaml(), "this Hermes neither ships PyYAML nor reads plugin dependency declarations"
