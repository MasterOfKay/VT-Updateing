"""
Two protections for a web UI that has no login of its own.

- Another website open in the same browser must not be able to send commands here
  (cross-site request forgery): requests that change something have to come from
  this page itself.
- Uploading a service means uploading code that then runs on this computer. That is
  only on while the web UI is reachable from this computer alone, or after it was
  switched on outside the browser with VT_WEBUI_SERVICE_UPLOAD=1.
"""
import os
from urllib.parse import urlsplit

from flask import jsonify, request

_SAFE = ("GET", "HEAD", "OPTIONS")
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def _split(netloc):
    parts = urlsplit(f"//{netloc}")
    try:
        return (parts.hostname or "").lower(), parts.port
    except ValueError:
        return "", None


def same_origin(headers, host):
    """Whether a request may have come from this web UI's own page. Programs other than
    browsers send neither header."""
    if headers.get("Sec-Fetch-Site") in ("same-origin", "none"):
        return True
    origin = headers.get("Origin")
    if origin is None:
        return headers.get("Sec-Fetch-Site") != "cross-site"
    sent = urlsplit(origin)
    if not sent.netloc:
        return False  # "null": a sandboxed or local page
    for candidate in (host, headers.get("X-Forwarded-Host")):
        if not candidate:
            continue
        name, port = _split(candidate)
        sent_name, sent_port = _split(sent.netloc)
        # a reverse proxy may pass the host name on without its port
        if sent_name == name and (port is None or sent_port == port):
            return True
    return False


def install(app):
    @app.before_request
    def _check_origin():
        if request.method not in _SAFE and not same_origin(request.headers, request.host):
            return jsonify({"error": "This request came from another website, so it was refused. If you reach the "
                                     "web UI through a reverse proxy, let it pass the Host header on."}), 403
        return None


def upload_allowed(bound_host):
    setting = (os.environ.get("VT_WEBUI_SERVICE_UPLOAD") or "").strip().lower()
    if setting in ("1", "true", "yes", "on"):
        return True
    if setting:
        return False
    return (bound_host or "127.0.0.1") in _LOCAL_HOSTS
