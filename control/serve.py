import argparse

import uvicorn

from control.api.app import create_app
from control.bootstrap import build


def main():
    parser = argparse.ArgumentParser(
        description="Unified SBX control plane (explicit disposable/operator state)"
    )
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--allow-local", action="store_true")
    parser.add_argument("--insecure-local-cookie", action="store_true")
    parser.add_argument("--console-dir")
    args = parser.parse_args()
    app = create_app(
        build(args.dsn, args.state_dir, allow_local=args.allow_local),
        run_worker=True,
        secure_cookies=not args.insecure_local_cookie,
        console_dir=args.console_dir,
    )
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
