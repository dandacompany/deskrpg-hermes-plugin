"""Every third-party package the plugin imports is either declared in `plugin.yaml` or supplied by its host.

Upstream Hermes removed PyYAML from its core dependencies while the plugin kept `import yaml` undeclared, so on
an upstream install the plugin failed to import and no `/deskrpg/*` route came up. The test and CI environments
install PyYAML themselves, so only a check on the declarations catches that class of break.
"""

import ast
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "deskrpg_plugin"

# Import name → distribution name, where they differ.
DISTRIBUTION = {"yaml": "pyyaml"}

# Provided by the process the plugin runs in, not by a declaration:
# - aiohttp: the Hermes API server that hosts the plugin requires it;
# - Hermes' own top-level modules.
HOST_PROVIDED = {"aiohttp", "hermes_cli", "hermes_constants", "hermes_state", "hermes_time", "cron", "gateway",
                 "tools", "agent", "tui_gateway"}


def _third_party_imports() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in PACKAGE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top not in sys.stdlib_module_names and top != "deskrpg_plugin":
                    found.setdefault(top, set()).add(path.relative_to(ROOT).as_posix())
    return found


def _declared() -> set[str]:
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    requirements = manifest.get("python_dependencies") or []
    return {re.split(r"[\s\[<>=!~;]", req, maxsplit=1)[0].lower() for req in requirements}


def test_every_third_party_import_is_declared_or_host_provided():
    declared = _declared()
    undeclared = {
        name: sorted(files)
        for name, files in _third_party_imports().items()
        if name not in HOST_PROVIDED and DISTRIBUTION.get(name, name).lower() not in declared
    }
    assert undeclared == {}, f"declare these in plugin.yaml python_dependencies: {undeclared}"


def test_pyyaml_is_declared_with_an_upper_bound():
    # Hermes asks plugins to pin upper bounds; the plugin imports `yaml` in several modules.
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    assert any(re.fullmatch(r"PyYAML>=6,<7", req) for req in manifest["python_dependencies"])
