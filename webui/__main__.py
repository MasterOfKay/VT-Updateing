import argparse
import os

from webui.app import create_app


def main():
    parser = argparse.ArgumentParser(description="VT.PR.WV Web UI")
    parser.add_argument("--host", default="127.0.0.1",
                         help="Interface to bind to (default: 127.0.0.1). Use 0.0.0.0 to expose on your network.")
    parser.add_argument("--port", type=int, default=8787, help="Port to listen on (default: 8787).")
    parser.add_argument("--debug", action="store_true", help="Enable Flask's debug mode (verbose error pages).")
    args = parser.parse_args()

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"WARNING: binding to {args.host} exposes this server - including credential management "
            f"and download triggering - to anyone who can reach it on the network. There is no login."
        )

    if os.environ.pop("VT_RESTART_WAIT", None):
        from webui.updater import wait_for_port
        print("Waiting for the previous web UI to close...")
        wait_for_port(args.host, args.port)

    app = create_app()
    print(f"VT.PR.WV Web UI: http://{args.host}:{args.port}")
    # use_reloader is always off: the background job queue worker thread is a
    # process-global singleton, and Flask's reloader runs the whole module in a
    # second child process, which would spin up a second worker/queue.
    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)


if __name__ == "__main__":
    main()
