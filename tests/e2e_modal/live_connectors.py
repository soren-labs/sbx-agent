"""Opt-in: run the real connector validators with the benchmark credentials (statuses only)."""

from __future__ import annotations

import os

from control.integrations.connectors import github, modal, opencode_zen


def main() -> None:
    zen = opencode_zen.validate(
        opencode_zen.normalize({"api_key": os.environ["OPENCODE_ZEN_API_KEY"]})
    )
    print(
        "zen",
        zen.status,
        zen.details.get("auth"),
        (zen.catalog or {}).get("preferred_model"),
        sum(m["free"] for m in (zen.catalog or {}).get("models", [])),
        "free models",
    )
    bad = opencode_zen.validate({"api_key": "sk-invalid-0000000000000000000000"})
    print("zen-bad", bad.status)
    m = modal.validate(
        modal.normalize(
            {
                "token_id": os.environ["SBX_TEST_MODAL_TOKEN_ID"],
                "token_secret": os.environ["SBX_TEST_MODAL_TOKEN_SECRET"],
            }
        )
    )
    print("modal", m.status)
    print(
        "modal-bad",
        modal.validate(
            {"token_id": "ak-invalid000000000", "token_secret": "as-invalid000000000"}
        ).status,
    )
    g = github.validate(
        github.normalize({"token": os.environ["SBX_TEST_GITHUB_TOKEN"]}),
        repositories=[os.environ["SBX_BENCHMARK_GITHUB_REPO"]],
    )
    print("github", g.status, bool(g.external_identity), list(g.details["repositories"].values()))


if __name__ == "__main__":
    main()
