"""Explicit operator rollout to the prepared VPS; secrets travel only on SSH stdin."""

import hashlib
import io
import json
import os
import shlex
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def ssh(command, *, payload=None, timeout=120):
    result = subprocess.run(
        ["ssh", os.environ["SBX_PROD_SSH_ALIAS"], command],
        input=payload,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError("vps_rollout_command_failed")
    return result.stdout


def production_env():
    values = {
        "SBX_HOSTED": "1",
        "SBX_STATE_BACKEND": "postgres",
        "SBX_BACKEND": "local",
        "DATABASE_URL": "postgresql:///sbx?host=/var/run/postgresql",
        "SBX_BROWSER_ORIGINS": "https://sbx-agent.com",
        "SBX_CONNECTIONS_MODE": "production",
        "SBX_AUTH_EMAIL_MODE": "production",
        "SBX_OPERATOR_AUTH_ENABLED": "0",
        "SBX_HOSTED_ADAPTER_FACTORY": "control.production_adapters:create_adapters",
        "SBX_DEFAULT_MODEL": "gpt-6.1-sol",
        "SBX_CODEX_CONTROL_BIN": "/usr/local/bin/codex",
        "SBX_CODEX_BROKER_TMP": "/run/sbx-hosted/codex",
        "SBX_CODEX_IMPORT_ROOT": "/var/lib/sbx-hosted/import",
        "SBX_GITHUB_APP_PRIVATE_KEY_PATH": "/etc/sbx/github-app.pem",
    }
    for name in (
        "SBX_CONNECTION_ENCRYPTION_KEY",
        "RESEND_API_KEY",
        "SBX_AUTH_EMAIL_FROM",
        "SBX_GITHUB_APP_ID",
        "SBX_GITHUB_APP_SLUG",
    ):
        values[name] = os.environ[name]
        if not values[name] or any(char in values[name] for char in "\r\n\x00"):
            raise ValueError("production_environment_incomplete")
    for name in ("SBX_AUTH_EMAIL_DOMAIN", "SBX_AUTH_EMAIL_REPLY_TO"):
        if os.environ.get(name):
            values[name] = os.environ[name]
    return values


def source_archive():
    files = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT, text=True
    ).splitlines()
    digest = hashlib.sha256()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name in sorted(files):
            if name not in {"pyproject.toml", "uv.lock", "README.md"} and not name.startswith(
                ("control/", "runtime/", "broker/", "src/", "deploy/hosted/", "docs/", "web/")
            ):
                continue
            path = ROOT / name
            if not path.is_file() or path.is_symlink():
                continue
            if path.name in {"auth.json", ".env", ".modal.toml"} or "__pycache__" in path.parts:
                raise ValueError("credential_file_in_source_archive")
            digest.update(name.encode())
            digest.update(path.read_bytes())
            archive.add(path, arcname=name, recursive=False)
    return stream.getvalue(), digest.hexdigest()


def main():
    from control.connections import SecretVault

    assert SecretVault.from_env() is not None
    key = Path(os.environ["SBX_GITHUB_APP_PRIVATE_KEY_PATH"]).resolve()
    if key.is_relative_to(ROOT) or key.stat().st_mode & 0o077:
        raise ValueError("app_key_must_be_private_outside_checkout")
    # No user Modal credentials, AWS credentials or development-agent auth are copied.
    payload = json.dumps({"env": production_env(), "app_key": key.read_text()}).encode()
    remote = """import json,sys,pathlib,os,pwd,shlex
v=json.load(sys.stdin); env=pathlib.Path('/etc/sbx-hosted.env'); uid=pwd.getpwnam('sbx').pw_uid
if env.exists():
 old=dict(line.split('=',1) for line in env.read_text().splitlines()
          if '=' in line and not line.startswith('#'))
 previous=shlex.split(old.get('SBX_CONNECTION_ENCRYPTION_KEY',''))
 if previous and previous[0] != v['env']['SBX_CONNECTION_ENCRYPTION_KEY']: raise SystemExit(1)
def write(p,data):
 fd=os.open(p,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
 with os.fdopen(fd,'w') as f: f.write(data)
 os.chmod(p,0o600); os.chown(p,uid,pwd.getpwnam('sbx').pw_gid)
write(env,''.join(k+'='+json.dumps(val)+'\\n' for k,val in v['env'].items()))
root=pathlib.Path('/etc/sbx'); root.mkdir(mode=0o750,exist_ok=True)
os.chown(root,uid,pwd.getpwnam('sbx').pw_gid)
write(root/'github-app.pem',v['app_key'])
"""
    ssh("sudo python3 -c " + shlex.quote(remote), payload=payload)
    archive, digest = source_archive()
    ssh("sudo -u sbx tar -xzf - -C /opt/sbx-browser", payload=archive)
    print("Production source uploaded; artifact SHA256 " + digest, flush=True)
    ssh("sudo bash /opt/sbx-browser/deploy/hosted/install_vps.sh", timeout=1200)
    print("Production service and PostgreSQL health PASS", flush=True)
    # Record a public source manifest; verify every uploaded file before claiming
    # that the deployed artifact corresponds to the implementation commit.
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as uploaded:
        files = {
            member.name: hashlib.sha256(uploaded.extractfile(member).read()).hexdigest()
            for member in uploaded.getmembers()
        }
    release = {
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "source_dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)),
        "artifact_sha256": digest,
        "files": files,
    }
    verify = """import hashlib,json,pathlib,sys,os
v=json.load(sys.stdin); root=pathlib.Path('/opt/sbx-browser')
for name,digest in v['files'].items():
 p=root/name
 assert p.resolve().is_relative_to(root) and not p.is_symlink()
 assert hashlib.sha256(p.read_bytes()).hexdigest()==digest
p=pathlib.Path('/var/lib/sbx-hosted/release.json')
fd=os.open(p,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
with os.fdopen(fd,'w') as f: json.dump(v,f)
"""
    ssh("sudo -u sbx python3 -c " + shlex.quote(verify), payload=json.dumps(release).encode())
    print("Production uploaded source manifest verified PASS", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("Production rollout failed: " + type(error).__name__, flush=True)
        raise SystemExit(1) from None
