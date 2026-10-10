"""Codex subscription adapter: catalog normalization never invents a model or an effort."""

from __future__ import annotations

from control.integrations.subscriptions.codex import ADAPTER

RAW = {
    "complete": True,
    "items": [
        {
            "id": "model-a",
            "model": "model-a",
            "displayName": "Model A",
            "description": "Flagship",
            "isDefault": True,
            "hidden": False,
            "defaultReasoningEffort": "medium",
            "supportedReasoningEfforts": [
                {"reasoningEffort": "low", "description": "Fast"},
                {"reasoningEffort": "medium", "description": "Balanced"},
                {"reasoningEffort": "ultra", "description": "Maximum"},
            ],
        },
        {
            "id": "model-b",
            "model": "model-b",
            "displayName": "Model B",
            "isDefault": False,
            "hidden": False,
            "defaultReasoningEffort": "nonexistent",
            "supportedReasoningEfforts": [{"reasoningEffort": "low", "description": None}],
        },
        {"id": "model-hidden", "model": "model-hidden", "hidden": True},
        {"id": "model-plain", "model": "model-plain"},
        {"displayName": "no id"},
    ],
}


def test_catalog_keeps_only_reported_models_and_their_own_efforts() -> None:
    catalog = ADAPTER.catalog(RAW)
    assert [m["id"] for m in catalog["models"]] == ["model-a", "model-b", "model-plain"]
    assert catalog["default_model"] == "model-a" and catalog["complete"] is True
    a, b, plain = catalog["models"]
    assert [e["id"] for e in a["reasoning"]["efforts"]] == ["low", "medium", "ultra"]
    assert a["reasoning"]["kind"] == "levels" and a["reasoning"]["default"] == "medium"
    assert [e["id"] for e in b["reasoning"]["efforts"]] == ["low"]
    assert b["reasoning"]["default"] is None, "a default the model does not list is dropped"
    assert plain["reasoning"] == {"kind": "none", "efforts": [], "default": None}


def test_untrusted_or_empty_catalog_is_unavailable() -> None:
    assert ADAPTER.catalog(None) is None
    assert ADAPTER.catalog("models") is None
    assert ADAPTER.catalog({"items": []}) is None
    assert ADAPTER.catalog({"items": [{"hidden": True, "id": "x"}]}) is None


def test_setup_closes_stdin_free_commands_and_keeps_auth_on_the_volume() -> None:
    spec = ADAPTER.setup_spec("login")
    assert spec["login_argv"][-2:] == ["login", "--device-auth"]
    assert spec["mode"] == "login" and spec["profile_dir"] == "/profile"
    assert ADAPTER.profile_env["CODEX_HOME"] == "/profile/.codex"
    assert spec["catalog"]["requests"][-1]["method"] == "model/list"
    assert ADAPTER.catalog_source == "codex app-server model/list"
