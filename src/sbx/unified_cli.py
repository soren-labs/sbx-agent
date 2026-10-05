"""``sbx`` unified product CLI (RFC 167 §08).

Product surface against the single ``/api``: ``sessions``,
``connections``, ``changesets``, ``deliveries``, ``delegations``,
``operations``, plus ``login``/``execute`` convenience. Secrets are read
from protected stdin/files — never argv.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections.abc import Sequence
from typing import Any

from sbx.sdk.unified import UnifiedClient


def _client(args: argparse.Namespace) -> UnifiedClient:
    base = getattr(args, "api_url", None) or os.environ.get("SBX_API_URL", "http://localhost:8000")
    token = getattr(args, "token", None) or os.environ.get("SBX_API_TOKEN")
    return UnifiedClient(base, token=token)


def _workspace(args: argparse.Namespace, client: UnifiedClient) -> str:
    if getattr(args, "workspace", None):
        return args.workspace
    return client.me()["workspaces"][0]


def _emit(payload: Any, as_json: bool = True) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(payload)


def _read_secret(prompt: str, args: argparse.Namespace) -> str:
    """Protected input order: --*-file → stdin (piped) → getpass."""
    path = getattr(args, "secret_file", None)
    if path:
        with open(path, "rb") as fh:
            return fh.read().decode().strip()
    if not sys.stdin.isatty():
        data = sys.stdin.read().strip()
        if data:
            return data
    return getpass.getpass(prompt)


# ---------------------------------------------------------------------------
# command handlers


def cmd_login(args: argparse.Namespace, env) -> int:
    pw = _read_secret("Password: ", args)
    with _client(args) as c:
        out = c.login(args.email, pw)
    print(out["token"])
    print(
        "workspace: " + ", ".join(out["user"]["workspaces"]),
        file=sys.stderr,
    )
    return 0


def cmd_register(args: argparse.Namespace, env) -> int:
    pw = _read_secret("Password: ", args)
    with _client(args) as c:
        out = c.register(args.email, pw, args.display_name)
    _emit(out)
    return 0


def cmd_execute(args: argparse.Namespace, env) -> int:
    with _client(args) as c:
        ws = _workspace(args, c)
        out = c.execute(
            args.prompt,
            workspace_id=ws,
            repository=args.repository,
            base_ref=args.base_ref,
            model=args.model,
            timeout_s=args.timeout,
        )
    _emit(out)
    return 0 if out["turn_state"] == "succeeded" else 1


def cmd_sessions(args: argparse.Namespace, env) -> int:
    with _client(args) as c:
        ws = _workspace(args, c)
        act = args.sessions_cmd
        if act == "list":
            _emit(c.sessions.list(ws, lifecycle=args.lifecycle))
        elif act == "create":
            out = c.sessions.create(
                ws,
                title=args.title,
                harness={"provider_id": "opencode", "model": args.model},
                projectless_spec=(
                    {"repository": args.repository, "base_ref": args.base_ref}
                    if args.repository
                    else None
                ),
                message=({"content": {"text": args.prompt}} if args.prompt else None),
            )
            _emit(out)
        elif act == "show":
            _emit(c.sessions.get(args.session_id))
        elif act == "send":
            _emit(c.messages.send(args.session_id, {"text": args.prompt}))
        elif act == "events":
            _emit(c.sessions.events(args.session_id, after_seq=args.after_seq))
        elif act == "cancel":
            _emit(c.sessions.cancel(args.session_id))
        elif act == "continue":
            _emit(c.sessions.continue_(args.session_id))
        elif act == "export":
            _emit(c.sessions.export(args.session_id))
        elif act == "close":
            _emit(c.sessions.close(args.session_id))
        elif act == "models":
            _emit(c.list_models(ws))
    return 0


def cmd_projects(args: argparse.Namespace, env) -> int:
    with _client(args) as c:
        ws = _workspace(args, c)
        act = args.projects_cmd
        if act == "list":
            _emit(c.projects.list(ws))
        elif act == "create":
            _emit(c.projects.create(ws, slug=args.slug, name=args.name))
        elif act == "publish-version":
            _emit(
                c.projects.publish_version(
                    args.project_id,
                    repository=args.repository,
                    base_ref=args.base_ref,
                )
            )
    return 0


def cmd_connections(args: argparse.Namespace, env) -> int:
    with _client(args) as c:
        ws = _workspace(args, c)
        act = args.connections_cmd
        if act == "list":
            _emit(c.connections.list(ws))
        elif act == "add":
            cred = _credential_payload(args)
            _emit(c.connections.add(ws, args.kind, cred, label=args.label))
        elif act == "replace":
            cred = _credential_payload(args)
            _emit(c.connections.replace(args.connection_id, cred))
        elif act == "validate":
            _emit(c.connections.validate(args.connection_id))
        elif act == "capabilities":
            _emit(c.connections.capabilities(args.connection_id))
        elif act == "disconnect":
            _emit(c.connections.disconnect(args.connection_id))
    return 0


_FORMAT_FIELDS = {
    "opencode_zen": ("api_key", ["api_key"]),
    "modal": ("token_pair", ["token_id", "token_secret"]),
    "github": ("personal_token", ["token"]),
}


def _credential_payload(args: argparse.Namespace) -> dict:
    """Assemble a credential dict from a JSON file or protected prompts —
    never argv."""
    if getattr(args, "credential_file", None):
        with open(args.credential_file, "rb") as fh:
            doc = json.loads(fh.read().decode())
        return {"format": doc["format"], "payload": doc["payload"]}
    fmt, fields = _FORMAT_FIELDS[args.kind]
    payload = {}
    for f in fields:
        payload[f] = getpass.getpass(f"{args.kind}.{f}: ")
    return {"format": fmt, "payload": payload}


def cmd_changesets(args: argparse.Namespace, env) -> int:
    with _client(args) as c:
        act = args.changesets_cmd
        if act == "list":
            _emit(c.changesets.list(args.session_id))
        elif act == "capture":
            _emit(c.changesets.capture(args.session_id, source_turn_id=args.source_turn_id))
        elif act == "apply":
            _emit(c.changesets.apply(args.changeset_id, args.session_id))
        elif act == "files":
            _emit(c.changesets.files(args.changeset_id))
    return 0


def cmd_deliveries(args: argparse.Namespace, env) -> int:
    with _client(args) as c:
        act = args.deliveries_cmd
        if act == "show":
            _emit(c.deliveries.get(args.delivery_id))
        elif act == "request":
            _emit(
                c.deliveries.request(
                    args.changeset_id,
                    {"repository": args.repository, "ref": args.ref},
                    transport=args.transport,
                    connection_id=args.connection_id,
                )
            )
        elif act == "retry":
            _emit(c.deliveries.retry(args.delivery_id))
        elif act == "merge":
            _emit(
                c.deliveries.merge(
                    args.delivery_id,
                    expected_head_sha=args.expected_head_sha,
                    merge_method=args.merge_method,
                )
            )
    return 0


def cmd_delegations(args: argparse.Namespace, env) -> int:
    with _client(args) as c:
        act = args.delegations_cmd
        if act == "list":
            _emit(c.delegations.list(args.session_id))
        elif act == "spawn":
            _emit(
                c.delegations.spawn(
                    args.session_id,
                    args.role,
                    args.prompt,
                    result_contract={"kind": args.contract},
                )
            )
        elif act == "wait":
            _emit(
                c.delegations.wait(
                    args.delegation_id,
                    deadline_seconds=args.deadline_seconds,
                )
            )
        elif act == "result":
            _emit(c.delegations.result(args.delegation_id))
        elif act == "cancel":
            _emit(c.delegations.cancel(args.delegation_id))
    return 0


def cmd_operations(args: argparse.Namespace, env) -> int:
    with _client(args) as c:
        _emit(c.operations.get(args.operation_id))
    return 0


def cmd_jobs(args: argparse.Namespace, env) -> int:
    with _client(args) as c:
        _emit(c.operations.job(args.job_id))
    return 0


# ---------------------------------------------------------------------------
# parser


def _ws(p: argparse.ArgumentParser) -> None:
    p.add_argument("--workspace", help="workspace id (defaults to your first)")


def _api_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--api-url", dest="api_url", help="unified API base URL (env SBX_API_URL)")
    p.add_argument("--token", help="login token or sbx_k_ key (env SBX_API_TOKEN)")


def add_unified_commands(sub, sub_common) -> None:
    """Attach the unified product command groups to the sbx parser."""

    p = sub.add_parser(
        "login", parents=[sub_common], help="email/password login → prints a session token once"
    )
    _api_args(p)
    p.add_argument("--email", required=True)
    p.add_argument(
        "--secret-file", help="read the password from this file (mode 600) instead of a prompt"
    )
    p.set_defaults(func=cmd_login)

    p = sub.add_parser(
        "register", parents=[sub_common], help="create an account (password via protected input)"
    )
    _api_args(p)
    p.add_argument("--email", required=True)
    p.add_argument("--display-name")
    p.add_argument("--secret-file")
    p.set_defaults(func=cmd_register)

    p = sub.add_parser(
        "execute", parents=[sub_common], help="one-shot: session + prompt → wait for the turn"
    )
    _api_args(p)
    _ws(p)
    p.add_argument("prompt")
    p.add_argument("--repository")
    p.add_argument("--base-ref", default="main")
    p.add_argument("--model")
    p.add_argument("--timeout", type=float, default=600.0)
    p.set_defaults(func=cmd_execute)

    # --- sessions ------------------------------------------------------------
    p = sub.add_parser("sessions", parents=[sub_common], help="session lifecycle")
    _api_args(p)
    ss = p.add_subparsers(dest="sessions_cmd", required=True)
    c = ss.add_parser("create")
    _ws(c)
    c.add_argument("--title")
    c.add_argument("--repository")
    c.add_argument("--base-ref", default="main")
    c.add_argument("--model", default="opencode/big-pickle")
    c.add_argument("--prompt")
    lp = ss.add_parser("list")
    _ws(lp)
    lp.add_argument("--lifecycle")
    s = ss.add_parser("show")
    s.add_argument("session_id")
    s2 = ss.add_parser("send")
    s2.add_argument("session_id")
    s2.add_argument("prompt")
    e = ss.add_parser("events")
    e.add_argument("session_id")
    e.add_argument("--after-seq", type=int, default=0)
    x = ss.add_parser("cancel")
    x.add_argument("session_id")
    cn = ss.add_parser("continue")
    cn.add_argument("session_id")
    xo = ss.add_parser("export")
    xo.add_argument("session_id")
    cl = ss.add_parser("close")
    cl.add_argument("session_id")
    m = ss.add_parser("models", help="usable models for your workspace")
    _ws(m)
    p.set_defaults(func=cmd_sessions)

    # --- projects -------------------------------------------------------------
    p = sub.add_parser("projects", parents=[sub_common], help="projects + versions")
    _api_args(p)
    ps = p.add_subparsers(dest="projects_cmd", required=True)
    lp = ps.add_parser("list")
    _ws(lp)
    c = ps.add_parser("create")
    _ws(c)
    c.add_argument("--slug", required=True)
    c.add_argument("--name", required=True)
    v = ps.add_parser("publish-version")
    v.add_argument("project_id")
    v.add_argument("--repository", required=True)
    v.add_argument("--base-ref", default="main")
    p.set_defaults(func=cmd_projects)

    # --- connections -----------------------------------------------------------
    p = sub.add_parser(
        "connections",
        parents=[sub_common],
        help="credential lifecycle (secrets via protected input)",
    )
    _api_args(p)
    cs = p.add_subparsers(dest="connections_cmd", required=True)
    lp = cs.add_parser("list")
    _ws(lp)
    a = cs.add_parser("add")
    _ws(a)
    a.add_argument("kind", choices=["modal", "github", "opencode_zen"])
    a.add_argument("--label")
    a.add_argument("--credential-file", help='JSON file {"format":...,"payload":{...}} (protected)')
    r = cs.add_parser("replace")
    r.add_argument("connection_id")
    r.add_argument("kind", choices=["modal", "github", "opencode_zen"])
    r.add_argument("--credential-file")
    v = cs.add_parser("validate")
    v.add_argument("connection_id")
    ca = cs.add_parser("capabilities")
    ca.add_argument("connection_id")
    d = cs.add_parser("disconnect")
    d.add_argument("connection_id")
    p.set_defaults(func=cmd_connections)

    # --- changesets -------------------------------------------------------------
    p = sub.add_parser("changesets", parents=[sub_common])
    _api_args(p)
    chs = p.add_subparsers(dest="changesets_cmd", required=True)
    lp = chs.add_parser("list")
    lp.add_argument("session_id")
    c = chs.add_parser("capture")
    c.add_argument("session_id")
    c.add_argument("--source-turn-id")
    a = chs.add_parser("apply")
    a.add_argument("changeset_id")
    a.add_argument("session_id")
    f = chs.add_parser("files")
    f.add_argument("changeset_id")
    p.set_defaults(func=cmd_changesets)

    # --- deliveries ---------------------------------------------------------------
    p = sub.add_parser("deliveries", parents=[sub_common])
    _api_args(p)
    ds = p.add_subparsers(dest="deliveries_cmd", required=True)
    s = ds.add_parser("show")
    s.add_argument("delivery_id")
    r = ds.add_parser("request")
    r.add_argument("changeset_id")
    r.add_argument("--repository", required=True)
    r.add_argument("--ref")
    r.add_argument(
        "--transport", choices=["export", "git_branch", "pull_request"], default="pull_request"
    )
    r.add_argument("--connection-id")
    rt = ds.add_parser("retry")
    rt.add_argument("delivery_id")
    m = ds.add_parser("merge")
    m.add_argument("delivery_id")
    m.add_argument("--expected-head-sha", required=True)
    m.add_argument("--merge-method", default="squash")
    p.set_defaults(func=cmd_deliveries)

    # --- delegations ----------------------------------------------------------------
    p = sub.add_parser("delegations", parents=[sub_common])
    _api_args(p)
    dgs = p.add_subparsers(dest="delegations_cmd", required=True)
    lp = dgs.add_parser("list")
    lp.add_argument("session_id")
    s = dgs.add_parser("spawn")
    s.add_argument("session_id")
    s.add_argument("--role", default="reviewer")
    s.add_argument("--contract", default="GenericResult")
    s.add_argument("prompt")
    w = dgs.add_parser("wait")
    w.add_argument("delegation_id")
    w.add_argument("--deadline-seconds", type=int, default=1800)
    r = dgs.add_parser("result")
    r.add_argument("delegation_id")
    c = dgs.add_parser("cancel")
    c.add_argument("delegation_id")
    p.set_defaults(func=cmd_delegations)

    # --- operations / jobs -----------------------------------------------------------
    p = sub.add_parser(
        "operations", parents=[sub_common], help="diagnostics: an operation's evidence view"
    )
    _api_args(p)
    p.add_argument("operation_id")
    p.set_defaults(func=cmd_operations)

    p = sub.add_parser("jobs", parents=[sub_common], help="job state")
    _api_args(p)
    p.add_argument("job_id")
    p.set_defaults(func=cmd_jobs)


def main(argv: Sequence[str] | None = None) -> int:
    """Standalone entry: ``python -m sbx.unified_cli``."""
    parser = argparse.ArgumentParser(prog="sbx")
    sub = parser.add_subparsers(dest="command", required=True)
    sub_common = argparse.ArgumentParser(add_help=False)
    add_unified_commands(sub, sub_common)
    args = parser.parse_args(argv)
    return int(args.func(args, os.environ) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
