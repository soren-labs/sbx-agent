"""sbx-runtime process entry point; all env inputs are explicit lease boot inputs."""

import os

import uvicorn

from runtime.daemon.app import Runtime, create_runtime_app


def main():
    runtime = Runtime(
        os.environ["SBX_RUNTIME_ROOT"],
        os.environ["SBX_SESSION_ID"],
        os.environ["SBX_LEASE_ID"],
        int(os.environ["SBX_LEASE_GENERATION"]),
        os.environ["SBX_RUNTIME_TOKEN"],
    )
    uvicorn.run(create_runtime_app(runtime), host="0.0.0.0", port=8792, access_log=False)


if __name__ == "__main__":
    main()
