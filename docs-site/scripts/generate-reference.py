"""Generate machine-readable CLI/config/provider reference assets from the unified code."""

from __future__ import annotations

import json
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
sys.path.insert(0, str(ROOT / "src"))

from sbx.cli import build_parser  # noqa: E402
from sbx.config import reference  # noqa: E402


def write(name: str, text: str) -> None:
    (PUBLIC / name).write_text(text, encoding="utf-8")
    (PUBLIC_ZH / name).write_text(text, encoding="utf-8")


def provider_reference() -> list[dict[str, Any]]:
    manifests = json.loads((ROOT / "docs/specs/unified/harnesses/manifests.json").read_text())
    return [
        {
            "provider": m["provider_id"],
            "support": m["support_tier"],
            "transport": m["transport"],
            "distribution": m["distribution"],
            "capabilities": {k: v["status"] for k, v in m["capabilities"].items()},
        }
        for m in manifests
    ]


def cli_reference() -> str:
    parser = build_parser()
    chunks = [
        "sbx CLI exact help\nGenerated from the unified argparse command model.\n",
        f"\n===== sbx =====\n{parser.format_help()}",
    ]
    for action in parser._subparsers._group_actions:  # noqa: SLF001
        for name, sub in action.choices.items():
            chunks.append(f"\n===== sbx {name} =====\n{sub.format_help()}")
    return "".join(chunks)


write("config-reference.json", json.dumps(reference(), indent=2) + "\n")
write("provider-reference.json", json.dumps(provider_reference(), indent=2) + "\n")
write("cli-help.txt", cli_reference())
print("generated cli-help.txt, config-reference.json, provider-reference.json")
