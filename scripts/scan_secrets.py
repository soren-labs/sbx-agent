"""Stacked diff scanner. Findings never include matching values or source lines."""

import argparse
import json
import re
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--live-known-values", action="store_true")
    args = parser.parse_args()
    diff = subprocess.check_output(["git", "diff", "--no-ext-diff", args.base])
    signatures = re.compile(
        rb"(?:gh[pousr]_[A-Za-z0-9_]{25,}|sk-[A-Za-z0-9_-]{25,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"
    )
    added = b"\n".join(
        line[1:]
        for line in diff.splitlines()
        if line.startswith(b"+") and not line.startswith(b"+++")
    )
    findings = []
    if signatures.search(added):
        findings.append("added_secret_signature")
    known = []
    if args.live_known_values:
        from scripts.real_mvp import credentials

        _, password, modal, zen, github = credentials()
        known = [
            password,
            zen,
            github,
            modal["SBX_TEST_MODAL_TOKEN_ID"],
            modal["SBX_TEST_MODAL_TOKEN_SECRET"],
        ]
        if any(value.encode() in diff for value in known):
            findings.append("known_value_in_stacked_diff")
    paths = subprocess.check_output(["git", "ls-files", "-co", "--exclude-standard", "-z"]).split(
        b"\0"
    )
    for item in set(filter(None, paths)):
        path = Path(item.decode())
        if not path.is_file():
            continue
        content = path.read_bytes()
        if any(value.encode() in content for value in known):
            findings.append("known_value_in_file:" + str(path))
    print(
        json.dumps(
            {
                "passed": not findings,
                "findings": findings,
                "known_classes_checked": len(known),
                "stack_bytes": len(diff),
            }
        )
    )
    return bool(findings)


if __name__ == "__main__":
    raise SystemExit(main())
