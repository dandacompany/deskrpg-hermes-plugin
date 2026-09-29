"""Model list ordering for providers whose Hermes catalog arrives in models.dev insertion order."""

from deskrpg_plugin.model_order import order_models

# models.dev api.json, tool_call models in insertion order (2026-09-29). Hermes' picker for these
# providers passes this order through unchanged.
GOOGLE = [
    "gemini-flash-latest", "gemma-4-31b-it", "gemini-flash-lite-latest", "gemini-3.6-flash",
    "gemma-4-26b-a4b-it", "gemini-3.5-flash-lite", "gemini-3.1-pro-preview",
    "gemini-2.5-computer-use-preview-10-2025", "gemini-3.5-flash", "gemini-2.5-pro", "gemini-2.5-flash",
    "gemini-3.1-flash-lite-image", "deep-research-preview-04-2026", "deep-research-max-preview-04-2026",
    "gemini-3.7-flash", "gemini-3.1-pro-preview-customtools", "gemini-3-flash-preview", "gemini-3.8-flash",
    "gemini-2.5-flash-lite", "gemini-3.1-flash-lite-preview", "gemini-3.1-flash-live-preview",
    "gemini-3.1-flash-lite",
]
MISTRAL = [
    "open-mistral-nemo", "codestral-latest", "mistral-large-2411", "mistral-nemo", "mistral-medium-2508",
    "mistral-large-latest", "mistral-small-latest", "zai-glm-5-2", "ministral-8b-latest",
    "devstral-medium-latest", "open-mixtral-8x22b", "devstral-2512", "mistral-medium-2505",
    "magistral-medium-latest", "pixtral-12b", "devstral-small-2505", "mistral-small-2603",
    "devstral-medium-2507", "labs-devstral-small-2512", "mistral-large-2512", "devstral-latest",
    "devstral-small-2507", "pixtral-large-latest", "voxtral-small-latest", "open-mixtral-8x7b",
    "mistral-small-2506", "zai-glm-5-3", "mistral-medium-2604", "open-mistral-7b", "ministral-3b-latest",
    "mistral-medium-latest",
]
GROQ = [
    "llama-3.3-70b-versatile", "llama-3.1-8b-instant", "qwen/qwen3.8-27b", "qwen/qwen3.6-27b",
    "openai/gpt-oss-20b", "openai/gpt-oss-safeguard-20b", "openai/gpt-oss-120b",
]
# What Hermes hands over for xAI: the models.dev order with its headline model pinned first and
# its curated extras merged in.
XAI = [
    "grok-4.6", "grok-4.7", "grok-4.3", "grok-4.20-0309-reasoning", "grok-4.5",
    "grok-4.20-0309-non-reasoning", "grok-build-0.1", "grok-composer-2.5-fast",
]


def test_google_models_are_grouped_by_family_newest_first():
    assert order_models("gemini", GOOGLE) == [
        "gemini-flash-latest", "gemini-flash-lite-latest",
        "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash",
        "gemini-3.5-flash", "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite", "gemini-3.1-flash-lite-image",
        "gemini-3.1-flash-lite-preview", "gemini-3.1-flash-live-preview",
        "gemini-3.1-pro-preview", "gemini-3.1-pro-preview-customtools",
        "gemini-3-flash-preview",
        "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro",
        "gemini-2.5-computer-use-preview-10-2025",
        "gemma-4-26b-a4b-it", "gemma-4-31b-it",
        "deep-research-max-preview-04-2026", "deep-research-preview-04-2026",
    ]


def test_google_alias_gets_the_same_order():
    assert order_models("google", GOOGLE) == order_models("gemini", GOOGLE)


def test_mistral_puts_latest_aliases_then_dated_releases_newest_first():
    assert order_models("mistral", MISTRAL) == [
        "mistral-large-latest", "mistral-medium-latest", "mistral-small-latest",
        "mistral-medium-2604", "mistral-small-2603", "mistral-large-2512", "mistral-medium-2508",
        "mistral-small-2506", "mistral-medium-2505", "mistral-large-2411", "mistral-nemo",
        "devstral-latest", "devstral-medium-latest", "devstral-2512", "devstral-medium-2507",
        "devstral-small-2507", "devstral-small-2505",
        "open-mistral-7b", "open-mistral-nemo", "open-mixtral-8x22b", "open-mixtral-8x7b",
        "zai-glm-5-3", "zai-glm-5-2",
        "ministral-3b-latest", "ministral-8b-latest",
        "pixtral-large-latest", "pixtral-12b",
        "codestral-latest", "magistral-medium-latest", "labs-devstral-small-2512", "voxtral-small-latest",
    ]


def test_groq_reads_versions_glued_to_the_name_and_namespaced_families():
    assert order_models("groq", GROQ) == [
        "openai/gpt-oss-120b", "openai/gpt-oss-20b", "openai/gpt-oss-safeguard-20b",
        "llama-3.3-70b-versatile", "llama-3.1-8b-instant",
        "qwen/qwen3.8-27b", "qwen/qwen3.6-27b",
    ]


def test_xai_keeps_the_hermes_headline_first_and_sorts_the_rest():
    # Hermes pins its headline xAI model to the top on purpose; only the tail is unordered.
    expected = [
        "grok-4.6",
        "grok-4.7", "grok-4.5", "grok-4.3",
        "grok-4.20-0309-non-reasoning", "grok-4.20-0309-reasoning",
        "grok-composer-2.5-fast", "grok-build-0.1",
    ]
    assert order_models("xai", XAI) == expected
    assert order_models("xai-oauth", XAI) == expected


def test_a_provider_hermes_orders_itself_is_left_alone():
    # openai-codex comes back in the account catalog's backend priority order.
    codex = ["gpt-5.6-sol", "gpt-5.3-codex", "gpt-5.5", "gpt-5.4-mini"]
    assert order_models("openai-codex", codex) == codex


def test_ordering_is_independent_of_the_input_order():
    assert order_models("mistral", list(reversed(MISTRAL)))[:3] == [
        "mistral-large-latest", "mistral-medium-latest", "mistral-small-latest",
    ]
    assert sorted(order_models("gemini", GOOGLE)) == sorted(GOOGLE)
    assert order_models("gemini", []) == []


def test_a_stable_release_goes_above_a_preview_of_the_same_version():
    assert order_models("gemini", ["gemini-3.1-flash-preview", "gemini-3.1-pro"]) == [
        "gemini-3.1-pro", "gemini-3.1-flash-preview",
    ]


def test_a_version_glued_to_the_name_stays_in_one_family():
    assert order_models("groq", ["qwen3.6-plus", "llama-3.3-70b", "qwen3.8-max"]) == [
        "qwen3.8-max", "qwen3.6-plus", "llama-3.3-70b",
    ]
