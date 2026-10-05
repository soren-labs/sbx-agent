"""User-owned Modal SDK adapter. No ambient clients or control credentials in compute."""

from __future__ import annotations

import functools
import uuid
from pathlib import Path

from control.backend import SandboxHandle, SandboxPoll
from control.config import lifecycle_config
from control.hosted_auth import HostedAuthError
from control.modal_tags import modal_tags

ENVIRONMENT = "sbx-compute"
APP = "sbx-compute"
PORT = 8792


def _safe(function):
    @functools.wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except HostedAuthError:
            raise
        except Exception:
            raise HostedAuthError("modal_provider_unavailable", 503) from None

    return call


def _sdk():
    import modal

    return modal


def _close_stdin(process):
    process.stdin.write_eof()
    process.stdin.drain()


class UserModalProcess:
    def __init__(self, process, sandbox, pid_file):
        self._process, self._sandbox, self._pid_file = process, sandbox, pid_file
        self.stdout = self._lines()

    def _lines(self):
        pending = ""
        for chunk in self._process.stdout:
            pending += chunk if isinstance(chunk, str) else chunk.decode("utf-8", "replace")
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                yield line
        if pending:
            yield pending

    def wait(self):
        return int(self._process.wait())

    def stderr_text(self, limit=2000):
        # Provider stderr can echo authentication headers; return a bounded diagnostic.
        return "sandbox command failed" if self._process.returncode else ""

    @_safe
    def kill(self):
        code = (
            "import os,signal,time,pathlib; "
            f"p=pathlib.Path({self._pid_file!r}); "
            "pid=int(p.read_text()) if p.exists() else 0; "
            "\nif pid:\n"
            " try: os.killpg(pid,signal.SIGTERM)\n except ProcessLookupError: pass\n"
            " time.sleep(5)\n"
            " try: os.killpg(pid,signal.SIGKILL)\n except ProcessLookupError: pass\n"
        )
        killer = self._sandbox.exec("python", "-c", code, timeout=15)
        _close_stdin(killer)
        killer.wait()


class RealModalProvider:
    """Implements provisioning and compute seams using manual user tokens."""

    configured = True
    mock = False

    def __init__(self, *, sdk=None, image_factory=None):
        self._injected_sdk, self._image_factory = sdk, image_factory

    @property
    def sdk(self):
        return self._injected_sdk or _sdk()

    def _client(self, context):
        creds = context.credentials
        if not creds.get("token_id") or not creds.get("token_secret"):
            raise HostedAuthError("invalid_modal_credentials", 400)
        return self.sdk.Client.from_credentials(creds["token_id"], creds["token_secret"])

    def _app(self, client):
        return self.sdk.App.lookup(
            APP, environment_name=ENVIRONMENT, create_if_missing=True, client=client
        )

    @_safe
    def verify_workspace(self, context):
        client = self._client(context)
        workspace = self.sdk.Workspace.from_context(client=client)
        workspace.hydrate(client=client)
        if not workspace.name:
            raise HostedAuthError("invalid_modal_credentials", 400)
        return workspace.name

    @_safe
    def ensure_namespace(self, context, workspace):
        client = self._client(context)
        environment = self.sdk.Environment.from_name(
            ENVIRONMENT, create_if_missing=True, client=client
        )
        environment.hydrate(client=client)
        self._app(client)
        return f"{workspace}/{ENVIRONMENT}"

    @_safe
    def publish_runtime(self, context, namespace, version):
        client = self._client(context)
        app = self._app(client)
        # version is the immutable desired build identity already persisted
        # by ModalConnectionService. Publication and reconciliation share it.
        name = f"sbx-runtime-{version[:40]}"
        try:
            image = self.sdk.Image.from_name(name, environment_name=ENVIRONMENT, client=client)
            image.build(app=app)
            return image.object_id
        except self.sdk.exception.NotFoundError:
            pass
        factory = self._image_factory
        if factory is None:
            from runtime.image import sbx_hosted_runtime_image

            factory = sbx_hosted_runtime_image
        image = factory().build(app=app)
        image.publish(name, environment_name=ENVIRONMENT, client=client)
        return image.object_id

    @_safe
    def smoke(self, context, image):
        client = self._client(context)
        sandbox = self.sdk.Sandbox.create(
            "bash",
            "-c",
            "codex --version && opencode --version && python -c 'import runtime.http_service'",
            app=self._app(client),
            image=self.sdk.Image.from_id(image, client=client),
            client=client,
            timeout=60,
            secrets=[],
        )
        try:
            sandbox.wait()
            if sandbox.returncode != 0:
                raise HostedAuthError("modal_runtime_smoke_failed", 503)
        finally:
            sandbox.terminate()

    def authorization_url(self, state):
        raise HostedAuthError("modal_oauth_not_configured", 503)

    def exchange(self, user_id, code):
        raise HostedAuthError("modal_oauth_not_configured", 503)

    def _sandbox(self, context, handle):
        if (
            handle.tags.get("owner") != context.user_id
            or handle.tags.get("modal_connection") != context.connection_id
        ):
            raise HostedAuthError("sandbox_not_found", 404)
        sandbox = self.sdk.Sandbox.from_id(handle.id, client=self._client(context))
        tags = sandbox.get_tags()
        if any(
            tags.get(k) != handle.tags.get(k) for k in ("owner", "modal_connection", "session_id")
        ):
            raise HostedAuthError("sandbox_not_found", 404)
        return sandbox

    @_safe
    def create(self, context, spec, runtime):
        client = self._client(context)
        life = lifecycle_config()
        # User control tokens, long-lived provider state and operator bridges
        # have no place in the immutable image or sandbox's baseline env.
        env = {
            k: v
            for k, v in spec.env.items()
            if not k.startswith(("MODAL_", "AWS_"))
            and k
            not in {
                "CODEX_AUTH_JSON",
                "SBX_ACCOUNT_CREDENTIAL",
                "GH_TOKEN",
                "SBX_GITHUB_APP_PRIVATE_KEY",
                "RESEND_API_KEY",
                "DATABASE_URL",
            }
        }
        env.update({"SBX_WORK": "/work", "HOME": "/work/home", "CODEX_HOME": "/work/home/.codex"})
        sandbox = self.sdk.Sandbox.create(
            "sleep",
            "infinity",
            app=self._app(client),
            image=self.sdk.Image.from_id(runtime["image"], client=client),
            client=client,
            env=env,
            secrets=[],
            workdir="/work",
            tags=modal_tags(spec.tags),
            cpu=spec.cpu or 1,
            memory=spec.memory_mib or 1024,
            timeout=life.sandbox_timeout_s,
            idle_timeout=life.sandbox_idle_timeout_s,
            encrypted_ports=[PORT],
        )
        return SandboxHandle(sandbox.object_id, Path("/work"), dict(spec.tags))

    def restore(self, context, spec, runtime):
        return self.create(context, spec, runtime)

    @_safe
    def snapshot(self, context, handle):
        from control.config import ENV_SNAPSHOT_TIMEOUT_S, ENV_SNAPSHOT_TTL_S

        image = self._sandbox(context, handle).snapshot_filesystem(
            timeout=ENV_SNAPSHOT_TIMEOUT_S,
            ttl=ENV_SNAPSHOT_TTL_S,
        )
        return image.object_id

    @_safe
    def exec(self, context, handle, argv, env):
        sandbox = self._sandbox(context, handle)
        pid_file = f"/tmp/sbx-user-exec-{uuid.uuid4().hex}.pid"
        process = sandbox.exec(
            "setsid",
            "--wait",
            "bash",
            "-c",
            f'echo $$ > {pid_file}; exec "$@"',
            "sbx-exec",
            *argv,
            env=dict(env),
            secrets=[],
            bufsize=1,
        )
        _close_stdin(process)
        return UserModalProcess(process, sandbox, pid_file)

    @_safe
    def poll(self, context, handle):
        try:
            alive = self._sandbox(context, handle).poll() is None
        except self.sdk.exception.NotFoundError:
            alive = False
        return SandboxPoll(alive, 1 if alive else 0)

    @_safe
    def terminate(self, context, handle):
        try:
            self._sandbox(context, handle).terminate()
        except (self.sdk.exception.NotFoundError, self.sdk.exception.ConflictError):
            pass

    @_safe
    def list(self, context, tags):
        client = self._client(context)
        wanted = modal_tags(
            {**tags, "owner": context.user_id, "modal_connection": context.connection_id}
        )
        return [
            SandboxHandle(sb.object_id, Path("/work"), dict(sb.get_tags()))
            for sb in self.sdk.Sandbox.list(
                app_id=self._app(client).app_id, tags=wanted, client=client
            )
            if all(sb.get_tags().get(k) == v for k, v in wanted.items())
        ]

    @_safe
    def endpoint(self, context, handle, key):
        sandbox = self._sandbox(context, handle)
        # Probe first so a reconstructed control process reuses the live listener.
        start = (
            "import urllib.request,subprocess,time\n"
            f"url='http://127.0.0.1:{PORT}/capabilities'\n"
            "try: urllib.request.urlopen(url,timeout=1).close()\n"
            "except Exception:\n"
            " subprocess.Popen(['python','-m','uvicorn','runtime.http_service:from_env',"
            f"'--factory','--host','0.0.0.0','--port','{PORT}','--no-access-log',"
            "'--log-level','critical'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
            "stderr=subprocess.DEVNULL,start_new_session=True)\n"
            "for _ in range(100):\n"
            " try: urllib.request.urlopen(url,timeout=1).close(); break\n"
            " except Exception: time.sleep(.1)\n"
            "else: raise SystemExit(1)\n"
        )
        process = sandbox.exec(
            "python",
            "-c",
            start,
            env={
                "SBX_WORK": "/work",
                "SBX_RUNTIME_CONNECT_KEY": key,
                "SBX_RUNTIME_SANDBOX_ID": handle.id,
                "SBX_RUNTIME_OWNER": context.user_id,
                "SBX_RUNTIME_AGENT_ID": handle.tags["session_id"],
            },
            timeout=20,
        )
        _close_stdin(process)
        if process.wait() != 0:
            raise HostedAuthError("sandbox_runtime_unavailable", 503)
        return sandbox.tunnels()[PORT].url

    def stop(self):
        # Service restart must preserve user sandboxes for durable reconstruction.
        return None
