"""``sbx`` CLI over the unified API. Secrets are read from stdin/files, never argv."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from typing import Any

from sbx.config import ClientConfig
from sbx.sdk import SBXClient, SBXError

CREDENTIAL_FIELDS = {
    "modal": ("token_id", "token_secret"),
    "github": ("token",),
    "inference_api": ("api_key",),
}
INFERENCE_PROTOCOLS = ("openai_chat", "openai_responses", "anthropic_messages")
HARNESSES = ("opencode", "codex", "claude", "grok", "commandcode")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sbx", description="SBX: durable Sessions running official provider CLIs."
    )
    parser.add_argument("--base-url", help="API base URL (env SBX_BASE_URL)")
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth", help="sign in and store an API key").add_subparsers(
        dest="action", required=True
    )
    login = auth.add_parser(
        "login", help="email/password sign-in; password read from stdin or prompt"
    )
    login.add_argument("--email", required=True)

    sub.add_parser("harnesses", help="official CLI Harnesses and the protocols they accept")

    projects = sub.add_parser("projects", help="Projects and versions").add_subparsers(
        dest="action", required=True
    )
    projects.add_parser("list")
    pc = projects.add_parser("create")
    pc.add_argument("slug")
    pc.add_argument("--spec-file", required=True, help="JSON ProjectVersion spec")

    cons = sub.add_parser(
        "connections", help="manual Connections (secrets via stdin)"
    ).add_subparsers(dest="action", required=True)
    cons.add_parser("list")
    add = cons.add_parser("add")
    add.add_argument("kind", choices=sorted(CREDENTIAL_FIELDS))
    add.add_argument("--label")
    add.add_argument("--credential-file", help="JSON credential file (default: stdin)")
    rep = cons.add_parser("replace")
    rep.add_argument("connection_id")
    rep.add_argument("--credential-file")
    for command in (add, rep):
        # inference_api settings are not secret; only the API key comes from stdin/file.
        command.add_argument(
            "--endpoint",
            action="append",
            default=[],
            metavar="PROTOCOL=BASE_URL",
            help=f"inference_api endpoint; PROTOCOL is one of {', '.join(INFERENCE_PROTOCOLS)}"
            " (repeat for providers that speak several protocols)",
        )
        command.add_argument("--model", help="inference_api default model id")
    for name in ("validate", "disconnect", "show"):
        cons.add_parser(name).add_argument("connection_id")

    sessions = sub.add_parser("sessions", help="durable Sessions").add_subparsers(
        dest="action", required=True
    )
    sc = sessions.add_parser("create")
    sc.add_argument("--project")
    sc.add_argument("--backend", default=None, choices=["modal", "local"])
    sc.add_argument("--harness", default="opencode", choices=HARNESSES, help="official CLI")
    sc.add_argument("--model", help="model id (default: the inference connection's model)")
    sc.add_argument("--repo", help="owner/name for projectless Sessions")
    sc.add_argument("--message")
    sessions.add_parser("list")
    for name in ("show", "close", "export"):
        sessions.add_parser(name).add_argument("session_id")
    ss = sessions.add_parser("send")
    ss.add_argument("session_id")
    ss.add_argument("message")
    se = sessions.add_parser("events")
    se.add_argument("session_id")
    se.add_argument("--after", type=int, default=0)
    se.add_argument("--follow", action="store_true")
    sx = sessions.add_parser("cancel")
    sx.add_argument("turn_id")
    scont = sessions.add_parser("continue")
    scont.add_argument("session_id")
    scont.add_argument("message")

    ex = sub.add_parser("execute", help="create/continue a Session and follow the Turn")
    ex.add_argument("prompt")
    ex.add_argument("--session")
    ex.add_argument("--project")

    cs = sub.add_parser("changesets").add_subparsers(dest="action", required=True)
    cap = cs.add_parser("capture")
    cap.add_argument("session_id")
    cap.add_argument("--salvage", action="store_true")
    ap = cs.add_parser("apply")
    ap.add_argument("changeset_id")
    ap.add_argument("destination_session_id")
    cs.add_parser("show").add_argument("changeset_id")

    dl = sub.add_parser("deliveries").add_subparsers(dest="action", required=True)
    rq = dl.add_parser("request")
    rq.add_argument("changeset_id")
    rq.add_argument("--title")
    dl.add_parser("retry").add_argument("delivery_id")
    dl.add_parser("show").add_argument("delivery_id")
    mg = dl.add_parser("merge")
    mg.add_argument("delivery_id")
    mg.add_argument("--method", default="squash")
    mg.add_argument("--mark-ready", action="store_true")

    dg = sub.add_parser("delegations").add_subparsers(dest="action", required=True)
    sp = dg.add_parser("spawn")
    sp.add_argument("session_id")
    sp.add_argument("role", choices=["review", "test", "research", "security", "integration"])
    sp.add_argument("--changeset")
    sp.add_argument("--context")
    for name in ("wait", "result", "cancel"):
        dg.add_parser(name).add_argument("delegation_id")

    ops = sub.add_parser("operations").add_subparsers(dest="action", required=True)
    ops.add_parser("show").add_argument("operation_id")

    serve = sub.add_parser("serve", help="run a local control plane (operator tool)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8800)
    sub.add_parser("migrate", help="apply database migrations (operator tool)")
    return parser


def _read_credential(
    kind: str, path: str | None, stdin: Any, args: argparse.Namespace | None = None
) -> dict[str, Any]:
    raw = open(path).read() if path else stdin.read()
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        fields = CREDENTIAL_FIELDS.get(kind, ())
        if len(fields) != 1:
            raise SystemExit(f"{kind} needs a JSON object with {fields}") from None
        data = {fields[0]: raw.strip()}
    if kind == "inference_api" and args is not None:
        endpoints = {}
        for item in args.endpoint:
            protocol, _, base_url = item.partition("=")
            if protocol not in INFERENCE_PROTOCOLS or not base_url:
                raise SystemExit(f"--endpoint must be PROTOCOL=BASE_URL, got {item!r}")
            endpoints[protocol] = base_url
        if endpoints:
            data["endpoints"] = endpoints
        if args.model:
            data["model"] = args.model
    return data


def _out(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str))


def run(args: argparse.Namespace, client: SBXClient, stdin: Any = sys.stdin) -> Any:
    cmd, action = args.command, getattr(args, "action", None)
    if cmd == "auth":
        password = (
            stdin.readline().rstrip("\n") if not stdin.isatty() else getpass.getpass("Password: ")
        )
        client.login(args.email, password)
        key = client.create_api_key("sbx-cli")
        cfg = ClientConfig.load()
        cfg.base_url, cfg.api_key = client.base_url, key["key"]
        return {"signed_in": args.email, "api_key_prefix": key["prefix"], "config": str(cfg.save())}
    if cmd == "harnesses":
        return client.harnesses()
    if cmd == "projects":
        return (
            client.projects.list()
            if action == "list"
            else client.projects.create(args.slug, json.loads(open(args.spec_file).read()))
        )
    if cmd == "connections":
        if action == "list":
            return client.connections.list()
        if action == "add":
            return client.connections.add(
                args.kind,
                _read_credential(args.kind, args.credential_file, stdin, args),
                args.label,
            )
        if action == "replace":
            view = client.connections.get(args.connection_id)
            return client.connections.replace(
                args.connection_id,
                _read_credential(view["kind"], args.credential_file, stdin, args),
                view["version"],
            )
        return {
            "validate": client.connections.validate,
            "disconnect": client.connections.disconnect,
            "show": client.connections.get,
        }[action](args.connection_id)
    if cmd == "sessions":
        if action == "create":
            body: dict[str, Any] = {}
            if args.project:
                body["project_id"] = args.project
            if args.backend:
                body["executor"] = {"backend": args.backend}
            body["harness"] = {
                "provider_id": args.harness,
                **({"model": args.model} if args.model else {}),
            }
            if args.repo:
                body["repository"] = {"full_name": args.repo}
            if args.message:
                body["message"] = {"content": args.message}
            return client.sessions.create(**body)
        if action == "list":
            return client.sessions.list()
        if action in ("send", "continue"):
            return client.sessions.send(args.session_id, args.message)
        if action == "events":
            return list(
                client.sessions.events(args.session_id, after=args.after, follow=args.follow)
            )
        if action == "cancel":
            return client.turns.cancel(args.turn_id)
        return {
            "show": client.sessions.get,
            "close": client.sessions.close,
            "export": client.sessions.export,
        }[action](args.session_id)
    if cmd == "execute":
        return client.execute(args.prompt, session_id=args.session, project_id=args.project)
    if cmd == "changesets":
        if action == "capture":
            return client.changesets.capture(
                args.session_id, origin="salvage" if args.salvage else "explicit"
            )
        if action == "apply":
            return client.changesets.apply(args.changeset_id, args.destination_session_id)
        return client.changesets.get(args.changeset_id)
    if cmd == "deliveries":
        if action == "request":
            return client.deliveries.request(
                args.changeset_id, **({"title": args.title} if args.title else {})
            )
        if action == "merge":
            return client.deliveries.merge(
                args.delivery_id, method=args.method, mark_ready=args.mark_ready
            )
        return {"retry": client.deliveries.retry, "show": client.deliveries.get}[action](
            args.delivery_id
        )
    if cmd == "delegations":
        if action == "spawn":
            return client.delegations.spawn(
                args.session_id,
                args.role,
                **{
                    k: v
                    for k, v in (("changeset_id", args.changeset), ("context", args.context))
                    if v
                },
            )
        return {
            "wait": client.delegations.wait_result,
            "result": client.delegations.result,
            "cancel": client.delegations.cancel,
        }[action](args.delegation_id)
    if cmd == "operations":
        return client.operations.get(args.operation_id)
    raise SystemExit(f"unknown command {cmd}")


def main(
    argv: list[str] | None = None, *, client: SBXClient | None = None, stdin: Any = None
) -> int:
    args = build_parser().parse_args(argv)
    if args.command in ("serve", "migrate"):
        from control.composition import main as control_main

        return control_main(
            [args.command]
            + (["--host", args.host, "--port", str(args.port)] if args.command == "serve" else [])
        )
    cfg = ClientConfig.load()
    client = client or SBXClient(
        args.base_url or cfg.base_url, cfg.api_key, workspace_id=cfg.workspace_id
    )
    try:
        _out(run(args, client, stdin or sys.stdin))
    except SBXError as exc:
        print(
            json.dumps({"error": {"code": exc.code, "message": exc.message, "action": exc.action}}),
            file=sys.stderr,
        )
        return 1
    return 0
