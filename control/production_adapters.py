"""Credentialed hosted adapters; all account credentials stay user-scoped."""

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
        "github_factory": GitHubFactory(),
        "codex_provider": NativeCodexProvider(),
    }
