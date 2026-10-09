"""Opt-in: run the real connector validators with the benchmark credentials (statuses only)."""

from __future__ import annotations

import os

from control.integrations.connectors import github, inference_api, modal

from tests.e2e_modal.inference import inference_credential


def main() -> None:
    credential = inference_api.normalize(inference_credential())
    inference = inference_api.validate(credential)
    print(
        "inference",
        inference.status,
        {p: r["status"] for p, r in inference.details["endpoints"].items()},
        (inference.catalog or {}).get("preferred_model"),
        len((inference.catalog or {}).get("models", [])),
    )
    bad = inference_api.validate({**credential, "api_key": "sk-invalid-0000000000000000000000"})
    print("inference-bad", bad.status, bad.details.get("reason"))
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
