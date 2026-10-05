"""Credentialed hosted adapters; all account credentials stay user-scoped."""

import os

from control.real_codex import NativeCodexProvider
from control.real_github import GitHubFactory
from control.real_modal import RealModalProvider
from control.resend_email import ResendEmailSender


def create_adapters():
    compute = RealModalProvider()
    return {
        "email_sender": ResendEmailSender.from_env(),
        "modal_provider": compute,
        "compute_provider": compute,
        "github_factory": GitHubFactory()
        if all(
            os.environ.get(key)
            for key in (
                "SBX_GITHUB_APP_ID",
                "SBX_GITHUB_APP_SLUG",
                "SBX_GITHUB_APP_PRIVATE_KEY_PATH",
            )
        )
        else None,
        "codex_provider": NativeCodexProvider(),
    }
