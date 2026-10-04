"""Isolated real-gate infrastructure; no cloud credentials are sent to PostgreSQL."""

import os
import secrets
import subprocess
import time
from contextlib import contextmanager

import psycopg


def docker(*args):
    result = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=120,
        env={k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ},
    )
    if result.returncode:
        raise RuntimeError("isolated_postgres_container_failed")
    return result.stdout.strip()


@contextmanager
def isolated_postgres():
    name = "sbx-real-gate-" + secrets.token_hex(8)
    image = "postgres@sha256:1a6ab3f5345eb6dbe04a1349529caabdb0ab09293a09590fad07b2246bfa4b54"
    docker(
        "run",
        "--rm",
        "-d",
        "--name",
        name,
        "-e",
        "POSTGRES_HOST_AUTH_METHOD=trust",
        "-p",
        "127.0.0.1::5432",
        image,
    )
    try:
        port = docker("port", name, "5432/tcp").rsplit(":", 1)[1]
        url = f"postgresql://postgres@127.0.0.1:{port}/postgres"
        for _ in range(100):
            try:
                with psycopg.connect(url, connect_timeout=1):
                    break
            except psycopg.Error:
                time.sleep(0.2)
        else:
            raise RuntimeError("isolated_postgres_not_ready")
        yield url
    finally:
        docker("stop", "--time", "5", name)
