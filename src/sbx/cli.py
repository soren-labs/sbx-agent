"""``sbx`` CLI over the unified API. Secrets are read from stdin/files, never argv."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Any

from sbx.config import ClientConfig
from sbx.sdk import SBXClient, SBXError

CREDENTIAL_FIELDS = {
    "modal": ("token_id", "token_secret"),
    "github": ("token",),
    "inference_api": ("api_key",),
}
INFERENCE_PROTOCOLS = ("openai_chat", "openai_responses", "anthropic_messages")
HARNESSES = ("opencode", "codex", "claude", "grok", "commandcode", "antigravity")


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
    login.add_argument(
        "--scope",
        choices=("read", "write", "full"),
        default="write",
        help="scope of the stored API key (default write: everything but API keys/password)",
    )
    register = auth.add_parser(
        "register",
        help="create an account; password read from stdin or prompt; verification email follows",
    )
    register.add_argument("--email", required=True)
    verify = auth.add_parser(
        "verify",
        help="confirm the email with the token from the verification link (stdin or prompt)",
    )
    verify.add_argument("--token-file", help="file holding the token (default: stdin or prompt)")

    sub.add_parser(
        "status",
        help="diagnose the connection: reachability, readiness, API key, executors",
        description="Read-only diagnosis of the configured control plane. Exit codes: 0 ok, "
        "4 unreachable or not ready, 5 authentication, 6 no usable executor.",
    )

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

    slots = sub.add_parser(
        "slots",
        help="subscription Machine Slots (one official login each)",
        description="Machine Slots: independent official subscription logins kept on private "
        "Modal Volumes. Exit codes: 0 ok, 1 API error, 2 slot not ready, 3 still pending.",
    ).add_subparsers(dest="action", required=True)
    slots.add_parser("list", help="slots, counts and providers")
    slots.add_parser("providers", help="subscription providers this deployment offers")
    sa = slots.add_parser("add", help="create a slot and start its official login")
    sa.add_argument(
        "--provider", default="codex", help="codex, antigravity or grok (see: sbx slots providers)"
    )
    sa.add_argument("--label")
    sa.add_argument("--account-alias", help="your own note, e.g. which account this is")
    sa.add_argument("--volume", help="adopt an existing Modal Volume instead of logging in")
    sa.add_argument("--connection", help="Modal connection id (default: the verified one)")
    for name, text in (
        ("login", "run the official login again"),
        ("verify", "re-check the login and refresh the model catalog"),
    ):
        sub_parser = slots.add_parser(name, help=text)
        sub_parser.add_argument("slot_id")
        sub_parser.add_argument("--wait", action="store_true", help="wait for the outcome")
        sub_parser.add_argument("--timeout", type=float, default=1200.0)
    sa.add_argument("--wait", action="store_true", help="wait for the outcome")
    sa.add_argument("--timeout", type=float, default=1200.0)
    sw = slots.add_parser("wait", help="wait until no login is pending")
    sw.add_argument("slot_id")
    sw.add_argument("--timeout", type=float, default=1200.0)
    scode = slots.add_parser(
        "code", help="send the code the browser showed after sign-in (Antigravity)"
    )
    scode.add_argument("slot_id")
    scode.add_argument(
        "--code",
        help="the code; prefer the prompt or stdin, which keep it out of your shell history",
    )
    for name, text in (
        ("show", "one slot"),
        ("models", "model catalog and per-model reasoning efforts, with their source"),
        ("cancel-login", "stop the pending login"),
        ("logout", "destroy the stored login; the slot stays"),
    ):
        slots.add_parser(name, help=text).add_argument("slot_id")
    sr = slots.add_parser("rename", help="change the label or the account alias")
    sr.add_argument("slot_id")
    sr.add_argument("--label")
    sr.add_argument("--account-alias")
    sd = slots.add_parser("delete", help="delete the slot and its volume")
    sd.add_argument("slot_id")
    sd.add_argument("--confirm", required=True, help="the slot label, to confirm")

    sessions = sub.add_parser("sessions", help="durable Sessions").add_subparsers(
        dest="action", required=True
    )
    sc = sessions.add_parser("create")
    sc.add_argument("--project")
    sc.add_argument("--backend", default=None, choices=["modal", "local"])
    sc.add_argument(
        "--harness",
        default=None,
        choices=HARNESSES,
        help="official CLI (default: opencode, or the slot's CLI with --slot)",
    )
    sc.add_argument("--model", help="model id (default: the inference connection's model)")
    sc.add_argument("--slot", help="run on this Machine Slot instead of an inference API key")
    sc.add_argument("--effort", help="reasoning effort the chosen model supports")
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
    pt = cs.add_parser("patch", help="write the sealed ChangeSet as a git patch file")
    pt.add_argument("changeset_id")
    pt.add_argument("--output", "-o", required=True, help="file to write (git apply --binary)")

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

    config_flag = argparse.ArgumentParser(add_help=False)
    config_flag.add_argument(
        "--config",
        help="server config file (env SBX_SERVER_CONFIG; default $XDG_CONFIG_HOME/sbx/server.env)",
    )
    serve = sub.add_parser(
        "serve",
        parents=[config_flag],
        help="run a local control plane in the foreground (operator)",
    )
    serve.add_argument("--host", help="listen address (default SBX_LISTEN_HOST or 127.0.0.1)")
    serve.add_argument("--port", type=int, help="listen port (default SBX_LISTEN_PORT or 8800)")
    sub.add_parser(
        "migrate", parents=[config_flag], help="apply database migrations (operator tool)"
    )
    mcp = sub.add_parser(
        "mcp", help="external MCP server for Codex and Claude Code (stdio)"
    ).add_subparsers(dest="action", required=True)
    mcp.add_parser(
        "serve",
        help="serve SBX tools over stdio; configure the client to run this command",
    )

    server = sub.add_parser(
        "server",
        help="set up and run a self-hosted control plane in the background (operator)",
        description="Self-hosted control plane lifecycle. Output is JSON. Exit codes: 0 ok, "
        "3 configuration, 4 unreachable/not running, 5 database authentication, "
        "6 no usable executor (check only).",
    ).add_subparsers(dest="action", required=True)
    init = server.add_parser(
        "init",
        parents=[config_flag],
        help="create a private server config with fresh keys and a private PostgreSQL",
    )
    init.add_argument("--data-dir", help="instance data (default $XDG_DATA_HOME/sbx)")
    init.add_argument(
        "--external-database",
        action="store_true",
        help="use an existing PostgreSQL: its connection URL is read from stdin, never argv",
    )
    init.add_argument("--host", default="127.0.0.1", help="listen address (default 127.0.0.1)")
    init.add_argument("--port", type=int, default=8800)
    init.add_argument("--public-url", help="base URL for email links (default: the API URL)")
    init.add_argument(
        "--executors", default="local,modal", help="executor backends (default local,modal)"
    )
    server.add_parser(
        "check", parents=[config_flag], help="check configuration, PostgreSQL and executors"
    )
    for name, text in (
        ("start", "start the control plane in the background and wait until ready"),
        ("restart", "stop, then start; durable state is kept in PostgreSQL"),
    ):
        command = server.add_parser(name, parents=[config_flag], help=text)
        command.add_argument("--timeout", type=float, default=90.0)
    stop = server.add_parser(
        "stop", parents=[config_flag], help="stop the control plane (and the managed PostgreSQL)"
    )
    stop.add_argument(
        "--keep-postgres", action="store_true", help="leave the managed PostgreSQL running"
    )
    server.add_parser(
        "status", parents=[config_flag], help="process, health and readiness of this instance"
    )
    logs = server.add_parser("logs", parents=[config_flag], help="last lines of the server log")
    logs.add_argument("--lines", type=int, default=50)
    outbox = server.add_parser(
        "outbox",
        parents=[config_flag],
        help="latest email in the local outbox (verification links without Resend)",
    )
    outbox.add_argument("--to", help="recipient (default: newest message)")
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


class SlotNotReady(Exception):
    """A slot command finished but the slot cannot run Sessions (exit code 2 or 3)."""

    def __init__(self, slot: dict[str, Any], code: int) -> None:
        super().__init__(slot.get("status"))
        self.slot, self.code = slot, code


def _announce_code(login: dict[str, Any]) -> None:
    """Human prompt on stderr; stdout stays machine-readable JSON."""
    print(
        f"Open {login['verification_url']} and enter the code {login['user_code']} "
        f"(expires {login['code_expires_at']}).",
        file=sys.stderr,
        flush=True,
    )


def _ask_login_code(login: dict[str, Any], slot_id: str) -> str | None:
    """Authorization-code providers: prompt for the code the browser shows after sign-in."""
    print(
        f"Open {login['verification_url']} and sign in. The browser then shows a code.",
        file=sys.stderr,
        flush=True,
    )
    if sys.stdin.isatty():
        return getpass.getpass("Paste the code shown in the browser: ").strip() or None
    print(f"Send it with: sbx slots code {slot_id}", file=sys.stderr, flush=True)
    return None


def _slot_wait(client: SBXClient, slot_id: str, timeout: float) -> dict[str, Any]:
    try:
        slot = client.slots.wait(
            slot_id,
            deadline=timeout,
            on_code=_announce_code,
            on_input=lambda login: _ask_login_code(login, slot_id),
        )
    except SBXError as exc:
        if exc.code != "timeout":
            raise
        raise SlotNotReady(client.slots.get(slot_id), 3) from None
    if slot["status"] not in ("ready", "running"):
        raise SlotNotReady(slot, 2)
    return slot


def _slots(args: argparse.Namespace, client: SBXClient) -> Any:
    action = args.action
    if action == "list":
        return client.slots.overview()
    if action == "providers":
        return client.slots.providers()
    if action == "add":
        slot = client.slots.add(
            args.provider,
            label=args.label,
            account_alias=args.account_alias,
            compute_connection_id=args.connection,
            volume_name=args.volume,
        )
        return _slot_wait(client, slot["id"], args.timeout) if args.wait else slot
    if action in ("login", "verify"):
        slot = getattr(client.slots, action)(args.slot_id)
        return _slot_wait(client, slot["id"], args.timeout) if args.wait else slot
    if action == "wait":
        return _slot_wait(client, args.slot_id, args.timeout)
    if action == "code":
        if args.code is not None:
            code = args.code
        elif sys.stdin.isatty():
            code = getpass.getpass("Code shown in the browser: ")
        else:
            code = sys.stdin.readline()
        code = code.strip()
        if not code:
            raise SystemExit("no code was given")
        return client.slots.submit_code(args.slot_id, code)
    if action == "rename":
        changes = {
            k: v
            for k, v in (("label", args.label), ("account_alias", args.account_alias))
            if v is not None
        }
        return client.slots.update(args.slot_id, **changes)
    if action == "delete":
        return client.slots.delete(args.slot_id, confirm=args.confirm)
    return {
        "show": client.slots.get,
        "models": client.slots.models,
        "cancel-login": client.slots.cancel_login,
        "logout": client.slots.logout,
    }[action](args.slot_id)


def _out(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str))


def run(args: argparse.Namespace, client: SBXClient, stdin: Any = sys.stdin) -> Any:
    cmd, action = args.command, getattr(args, "action", None)
    if cmd == "auth" and action == "verify":
        if args.token_file:
            token = Path(args.token_file).read_text().strip()
        elif stdin.isatty():
            token = getpass.getpass("Verification token (the token= value of the link): ").strip()
        else:
            token = stdin.readline().strip()
        if not token:
            raise SystemExit("no verification token was given")
        return client.verify_email(token)
    if cmd == "auth" and action == "register":
        password = (
            stdin.readline().rstrip("\n") if not stdin.isatty() else getpass.getpass("Password: ")
        )
        result = client.register(args.email, password)
        return {
            **(result or {}),
            "next": "open the link in the verification email, or run `sbx server outbox` on a "
            "control plane without Resend; then `sbx auth verify` and `sbx auth login`",
        }
    if cmd == "auth":
        password = (
            stdin.readline().rstrip("\n") if not stdin.isatty() else getpass.getpass("Password: ")
        )
        client.login(args.email, password)
        key = client.create_api_key("sbx-cli", ["*" if args.scope == "full" else args.scope])
        cfg = ClientConfig.load()
        cfg.base_url, cfg.api_key = client.base_url, key["key"]
        return {
            "signed_in": args.email,
            "api_key_prefix": key["prefix"],
            "scopes": key["scopes"],
            "config": str(cfg.save()),
        }
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
    if cmd == "slots":
        return _slots(args, client)
    if cmd == "sessions":
        if action == "create":
            body: dict[str, Any] = {}
            if args.project:
                body["project_id"] = args.project
            if args.backend:
                body["executor"] = {"backend": args.backend}
            harness = args.harness
            if harness is None and args.slot:
                harness = client.slots.get(args.slot)["provider"]
            body["harness"] = {
                "provider_id": harness or "opencode",
                **({"model": args.model} if args.model else {}),
                **({"effort": args.effort} if args.effort else {}),
            }
            if args.slot:
                body["inference"] = {"mode": "subscription", "machine_slot_id": args.slot}
                body.setdefault("executor", {"backend": "modal"})
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
        if action == "patch":
            data = client.changesets.patch(args.changeset_id)
            Path(args.output).write_bytes(data)
            return {"changeset_id": args.changeset_id, "output": args.output, "bytes": len(data)}
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
    if args.command in ("serve", "migrate", "server"):
        from sbx.server_cli import run_operator

        return run_operator(args)
    if args.command == "mcp":
        from sbx.mcp_server import serve

        return serve()
    cfg = ClientConfig.load()
    client = client or SBXClient(
        args.base_url or cfg.base_url, cfg.api_key, workspace_id=cfg.workspace_id
    )
    if args.command == "status":
        from sbx.sdk.diagnostics import exit_code

        report = client.diagnose()
        _out(report)
        return exit_code(report)
    try:
        _out(run(args, client, stdin or sys.stdin))
    except SlotNotReady as exc:
        _out(exc.slot)
        return exc.code
    except SBXError as exc:
        print(
            json.dumps(
                {
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "action": exc.action,
                        "details": exc.details,
                    }
                }
            ),
            file=sys.stderr,
        )
        return 1
    return 0
