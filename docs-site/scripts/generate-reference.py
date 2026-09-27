"""Generate machine-readable CLI/config/provider reference assets from code."""

from __future__ import annotations

import dataclasses
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

SITE = Path(__file__).resolve().parents[1]
ROOT = SITE.parent
PUBLIC = SITE / "public"
PUBLIC_ZH = PUBLIC / "zh-cn"
PUBLIC.mkdir(parents=True, exist_ok=True)
PUBLIC_ZH.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))

from runtime.provider_runtime import provider_runtime_specs  # noqa: E402

from sbx.config import _FIELD_MAP, BootstrapConfig  # noqa: E402


def jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, tuple):
        return list(value)
    return value


def write_json(name: str, value: Any) -> None:
    text = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    (PUBLIC / name).write_text(text, encoding="utf-8")
    (PUBLIC_ZH / name).write_text(text, encoding="utf-8")


def config_reference() -> list[dict[str, Any]]:
    defaults = BootstrapConfig()
    rows: list[dict[str, Any]] = []
    for field in dataclasses.fields(BootstrapConfig):
        name = field.name
        (section, key), envs = _FIELD_MAP[name]
        rows.append(
            {
                "field": name,
                "toml": f"{section}.{key}",
                "environment": list(envs),
                "default": jsonable(getattr(defaults, name)),
            }
        )
    return rows


def provider_reference() -> list[dict[str, Any]]:
    return [
        {
            "provider": spec.provider,
            "support": spec.support,
            "cli": spec.cli,
            "install_kind": spec.install_kind,
            "reproducible": spec.reproducible,
            "summary": spec.summary,
            "default_models": list(spec.default_models),
        }
        for spec in provider_runtime_specs()
    ]


TRACKER = re.compile(r"\bSOR-\d+(?:/SOR-\d+)*\b\s*")


def help_text(argv: list[str]) -> str:
    proc = subprocess.run(
        [str(ROOT / "sbx"), *argv, "--help"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
    )
    # argparse help is product documentation, but implementation-ticket tags
    # in source help strings are not part of the public CLI contract.
    return TRACKER.sub(" ", proc.stdout).rstrip()


def cli_reference() -> str:
    sections: list[tuple[str, list[str]]] = [
        ("sbx", []),
        *[
            (f"sbx {cmd}", [cmd])
            for cmd in (
                "deploy",
                "doctor",
                "status",
                "open",
                "smoke",
                "upgrade",
                "uninstall",
                "config",
                "init",
                "credentials",
            )
        ],
        ("sbx github", ["github"]),
        ("sbx github connect", ["github", "connect"]),
        ("sbx auth", ["auth"]),
        *[
            (f"sbx auth {cmd}", ["auth", cmd])
            for cmd in ("status", "login", "import-existing", "verify", "relink", "pair", "logout")
        ],
    ]
    chunks = ["sbx-browser CLI exact help\nGenerated from the current argparse command model.\n"]
    for title, argv in sections:
        chunks.append(f"\n===== {title} =====\n{help_text(argv)}\n")
    return "".join(chunks)


write_json("config-reference.json", config_reference())
write_json("provider-reference.json", provider_reference())
cli = cli_reference()
(PUBLIC / "cli-help.txt").write_text(cli, encoding="utf-8")
(PUBLIC_ZH / "cli-help.txt").write_text(cli, encoding="utf-8")
print("generated cli-help.txt, config-reference.json, provider-reference.json")
