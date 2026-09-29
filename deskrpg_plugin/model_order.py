"""Order model lists that Hermes passes through in models.dev insertion order.

For some providers Hermes' picker hands back the models.dev listing as-is, and models.dev keeps
entries in the order they were added: Gemini 3.8 lands 18th of 22, Mistral's dated releases and
``-latest`` aliases are interleaved. The picker then makes people scroll for the newest model.

Only the providers listed here are reordered. A provider whose catalog Hermes orders itself
(openai-codex by backend priority, curated-first providers) keeps Hermes' order.
"""

from __future__ import annotations

import re
from decimal import Decimal

# Hermes provider ids (``hermes_cli/auth.py`` registry, plus the ``google`` alias) whose picker list
# is the models.dev insertion order.
SORTED_PROVIDERS = frozenset({"gemini", "google", "xai", "xai-oauth", "mistral", "groq"})

# Hermes pins a headline model to the top of the xAI list on purpose; only the tail is unordered.
PINNED_HEAD_PROVIDERS = frozenset({"xai", "xai-oauth"})

_PRERELEASE = frozenset({"preview", "exp", "experimental", "beta", "alpha"})
# A version token: a bare number (``3.8``, ``2512``) or one glued to a name (``qwen3.8``).
# Sizes such as ``27b`` or ``8x22b`` do not match.
_VERSION = re.compile(r"^[a-z]*(\d+(?:\.\d+)?)$")
_FAMILY = re.compile(r"^[a-z]+")


def _family(model: str) -> str:
    """The leading name (``gemini``, ``devstral``) or, for ``ns/model`` ids, the namespace."""
    head = re.split(r"[-_/]", model.lower(), maxsplit=1)[0]
    match = _FAMILY.match(head)
    return match.group(0) if match else head


def _rank(model: str) -> tuple:
    """Sort key inside a family: ``-latest`` aliases, then newest version, stable before preview."""
    tokens = re.split(r"[-_/]", model.lower())
    numbers = [Decimal(m.group(1)) for m in map(_VERSION.match, tokens) if m]
    alias = "latest" in tokens
    version = numbers[0] if numbers else None
    prerelease = any(t in _PRERELEASE for t in tokens)
    return (
        0 if alias else 1 if version is not None else 2,
        -(version or 0),
        prerelease,
        tuple(-n for n in numbers[1:]),
        model.lower(),
        model,
    )


def _sorted(models: list[str]) -> list[str]:
    # Families are ordered by size, the provider's main line having the most entries, then by where
    # they first appear. Version schemes differ between families (dates vs. 3.8), so their numbers
    # are never compared with each other.
    first_seen: dict[str, int] = {}
    size: dict[str, int] = {}
    for index, model in enumerate(models):
        family = _family(model)
        first_seen.setdefault(family, index)
        size[family] = size.get(family, 0) + 1
    return sorted(models, key=lambda m: (-size[_family(m)], first_seen[_family(m)], _rank(m)))


def order_models(provider_id: str, models: list[str]) -> list[str]:
    """The list to show for ``provider_id``: reordered for listed providers, untouched otherwise."""
    if provider_id not in SORTED_PROVIDERS:
        return models
    if provider_id in PINNED_HEAD_PROVIDERS and models:
        return [models[0], *_sorted(models[1:])]
    return _sorted(models)
