"""
It listens on its own port, separate from the web UI, and only serves /api/v1. So it can
be opened to the network while the web UI - which has no login and manages credentials -
stays on this machine. Every call needs the API key. It's off until switched on under
Settings.
"""
import hmac
import json
import logging
import socket
import threading
import time
from collections import defaultdict, deque

from flask import Flask, Response, jsonify, request
from werkzeug.exceptions import HTTPException
from werkzeug.serving import make_server

from . import api_progress, api_requests, api_settings, updater

MAX_WAIT = 600
MIN_INTERVAL, MAX_INTERVAL = 2, 300
MAX_STREAMS = 5
FAILURE_LIMIT = 10
FAILURE_WINDOW = 300

log = logging.getLogger("webui.api")


def create_api_app(requests_):
    app = Flask("vt_external_api", static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    failures = defaultdict(deque)  # address -> times of wrong keys
    # Open progress streams -> until when each counts as alive. Not freed by a close
    # callback: werkzeug skips it when the caller just disappears.
    streams = {}
    streams_lock = threading.Lock()

    @app.before_request
    def authorize():
        address = request.remote_addr or "?"
        recent = failures[address]
        now = time.time()
        while recent and now - recent[0] > FAILURE_WINDOW:
            recent.popleft()
        header = request.headers.get("Authorization", "")
        sent = header[7:].strip() if header.lower().startswith("bearer ") else request.headers.get("X-API-Key", "")
        key = api_settings.get()["key"]
        if key and sent and hmac.compare_digest(sent.encode(), key.encode()):
            return None
        if len(recent) >= FAILURE_LIMIT:
            return jsonify({"error": "Too many wrong keys. Wait a few minutes."}), 429
        recent.append(now)
        return jsonify({"error": "Missing or wrong API key. Send it as 'Authorization: Bearer <key>'."}), 401

    @app.errorhandler(api_requests.RequestError)
    def bad_request(e):
        return jsonify({"error": str(e)}), e.status

    @app.errorhandler(Exception)
    def failed(e):
        if isinstance(e, HTTPException):
            return jsonify({"error": e.description}), e.code
        if isinstance(e, ValueError):  # command_builder.CommandError
            return jsonify({"error": str(e)}), 400
        log.exception("API request failed")
        return jsonify({"error": "Internal error."}), 500

    def body():
        return request.get_json(force=True, silent=True) or {}

    def wait_for(found, seconds):
        """Hold the answer until the check is through (or the time is up)."""
        try:
            seconds = max(0.0, min(MAX_WAIT, float(seconds or 0)))
        except (TypeError, ValueError):
            seconds = 0
        if seconds:
            found.done.wait(seconds)
        return found

    def find(request_id):
        found = requests_.get(request_id)
        if not found:
            raise api_requests.RequestError("Unknown request.", 404)
        return found

    @app.get("/api/v1/ping")
    def ping():
        return jsonify({"ok": True, "version": updater.current_version()})

    @app.get("/api/v1/services")
    def services():
        from . import services_info  # lazy: see queue_manager.retry()
        return jsonify([{"name": s["name"], "aliases": s["aliases"]} for s in services_info.list_services()])

    @app.post("/api/v1/requests")
    def create():
        data = body()
        created = requests_.create(data)
        wait_for(created, data.get("wait"))
        return jsonify(created.to_dict()), 202 if created.status in api_requests.OPEN else 201

    @app.get("/api/v1/requests")
    def index():
        return jsonify([r.to_dict() for r in requests_.list()])

    @app.get("/api/v1/requests/<request_id>")
    def show(request_id):
        return jsonify(wait_for(find(request_id), request.args.get("wait")).to_dict())

    @app.post("/api/v1/requests/<request_id>/confirm")
    def confirm(request_id):
        return jsonify(requests_.confirm(request_id, body()).to_dict())

    @app.delete("/api/v1/requests/<request_id>")
    @app.post("/api/v1/requests/<request_id>/decline")
    def decline(request_id):
        return jsonify(requests_.decline(request_id).to_dict())

    @app.get("/api/v1/requests/<request_id>/job")
    def job(request_id):
        find(request_id)
        found = requests_.job(request_id)
        if not found:
            raise api_requests.RequestError("This request has no download (yet), or it was cleared "
                                            "from the queue.", 404)
        data = found.to_dict()
        # Only progress - not the command line, which can hold things like a proxy login.
        keep = ("id", "status", "service", "display_name", "display_year", "poster", "current_episode",
                "episodes", "created_at", "started_at", "finished_at", "attempt")
        return jsonify({k: data[k] for k in keep})

    @app.get("/api/v1/progress")
    def progress():
        return jsonify(api_progress.snapshot(requests_.queue, requests_))

    @app.get("/api/v1/progress/stream")
    def progress_stream():
        """One line of JSON every ?interval= seconds, until the caller hangs up."""
        interval = request.args.get("interval", 5, type=float)
        interval = max(MIN_INTERVAL, min(MAX_INTERVAL, interval if interval == interval else 5))
        key = api_settings.get()["key"]
        token = object()
        alive_for = interval + 15
        with streams_lock:
            now = time.time()
            for stale in [t for t, until in streams.items() if until < now]:
                del streams[stale]
            if len(streams) >= MAX_STREAMS:
                return jsonify({"error": f"Already {MAX_STREAMS} progress streams open."}), 429
            streams[token] = now + alive_for

        def lines():
            try:
                while True:
                    with streams_lock:
                        streams[token] = time.time() + alive_for
                    yield json.dumps(api_progress.snapshot(requests_.queue, requests_)) + "\n"
                    until = time.time() + interval
                    while time.time() < until:
                        time.sleep(min(1.0, interval))
                        settings = api_settings.get()
                        # Switched off, or a new key: this caller is no longer welcome.
                        if not settings["enabled"] or settings["key"] != key:
                            return
            finally:  # also runs when an abandoned stream is thrown away
                with streams_lock:
                    streams.pop(token, None)

        return Response(lines(), mimetype="application/x-ndjson",
                        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    return app


class ApiServer:
    def __init__(self, job_queue):
        self.requests = api_requests.Requests(job_queue)
        self._app = create_api_app(self.requests)
        self._lock = threading.Lock()
        self._server = None
        self._address = None
        self.error = None
        self.ui_port = None

    def apply(self):
        """Start, stop or move the server to match the settings."""
        settings = api_settings.get()
        address = (settings["host"], settings["port"]) if settings["enabled"] else None
        with self._lock:
            if self._server and address == self._address:
                return
            self._stop()
            self.error = None
            if not address:
                return
            if address[1] == self.ui_port:
                self.error = "The API can't use the web UI's own port. Choose another one."
                return
            try:
                # Check first: werkzeug ends the program if the port is taken, and Windows
                # would let two servers share it.
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_socket:
                    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                        probe_socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                    else:
                        probe_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    probe_socket.bind(address)
                self._server = make_server(address[0], address[1], self._app, threaded=True)
            except (OSError, SystemExit) as e:
                self.error = f"Couldn't listen on {address[0]}:{address[1]}: {e}"
                return
            self._address = address
            threading.Thread(target=self._server.serve_forever, daemon=True).start()
            print(f"VT.PR.WV external API: http://{address[0]}:{address[1]}/api/v1")

    def _stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        self._server = self._address = None

    def status(self):
        return {**api_settings.get(), "running": self._server is not None, "error": self.error}


server = None


def init(job_queue, ui_port=None):
    global server
    if server is None:
        server = ApiServer(job_queue)
        server.ui_port = ui_port
        server.apply()
    return server
