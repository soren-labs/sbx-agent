import argparse
import threading
from urllib.parse import urlparse

import uvicorn

from control.api.app import create_app
from control.api.preview import create_preview_app
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
    parser.add_argument("--public-origin")
    parser.add_argument("--email-command", nargs="+")
    parser.add_argument("--preview-origin")
    parser.add_argument("--preview-port", type=int, default=8788)
    args = parser.parse_args()
    if args.preview_origin:
        if (
            not args.public_origin
            or urlparse(args.preview_origin).scheme != "https"
            or urlparse(args.preview_origin).hostname == urlparse(args.public_origin).hostname
        ):
            parser.error("Preview requires HTTPS on a different hostname from --public-origin")
    resources = build(
        args.dsn,
        args.state_dir,
        allow_local=args.allow_local,
        control_origin=args.public_origin,
        preview_origin=args.preview_origin,
        email_command=args.email_command,
    )
    if args.preview_origin:
        threading.Thread(
            target=lambda: uvicorn.run(
                create_preview_app(resources),
                host=args.host,
                port=args.preview_port,
                access_log=False,
            ),
            daemon=True,
        ).start()
    app = create_app(
        resources,
        run_worker=True,
        secure_cookies=not args.insecure_local_cookie,
        console_dir=args.console_dir,
    )
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
