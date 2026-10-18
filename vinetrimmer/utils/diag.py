"""Record what a Netflix manifest run actually sends and receives.

Built to compare two runs of the same build on different machines - one that is
served HEVC/DV and one that is not - so the difference can be read off rather
than guessed at. Enable with VT_DIAG=1; a JSON file lands in diag/ and nothing
else about the run changes.

Secrets are fingerprinted, not recorded: cookies, challenges and tokens go in as
a SHA-256 prefix plus a length, which is enough to tell whether two runs used the
same value without the file carrying anything worth leaking.
"""

import atexit
import base64
import hashlib
import json
import os
import platform
import re
import sys
import time

ENABLED = bool(os.environ.get("VT_DIAG"))

_record = {"meta": {}, "env": {}, "requests": []}
_started = time.time()


def fingerprint(value):
    """A value's identity without its content."""
    if value is None:
        return None
    if not isinstance(value, (str, bytes)):
        value = str(value)
    if isinstance(value, str):
        value = value.encode("utf-8", "replace")
    return {"sha256": hashlib.sha256(value).hexdigest()[:16], "len": len(value)}


def _challenge_facts(b64):
    """The parts of a PlayReady challenge that decide what a client is granted."""
    try:
        xml = base64.b64decode(b64).decode("utf-8", "ignore")
    except Exception:
        return None
    facts = {}
    for tag in ("CLIENTVERSION", "IIS_DRM_VERSION", "ClientTime", "LicenseNonce"):
        m = re.search(r"<%s>([^<]*)</%s>" % (tag, tag), xml)
        facts[tag] = m.group(1) if m else None
    facts["revocation_versions"] = [v for _, v in re.findall(
        r"<ListID>([^<]*)</ListID><Version>(\d+)</Version>", xml)]
    m = re.search(r"<PublicKey>([^<]*)</PublicKey>", xml)
    # The leaf key identifies which device the challenge came from, and devices
    # are granted different codec ladders - so it is worth comparing directly.
    facts["leaf_public_key"] = m.group(1)[:44] if m else None
    return facts


def environment():
    """Everything about the host that could plausibly change the answer."""
    if not ENABLED:
        return
    try:
        import ssl
        openssl = ssl.OPENSSL_VERSION
    except Exception:
        openssl = None

    packages = {}
    for name in ("requests", "urllib3", "curl_cffi", "cryptography", "pycryptodomex", "httpx"):
        try:
            mod = __import__(name)
            packages[name] = getattr(mod, "__version__", "?")
        except Exception:
            packages[name] = None

    _record["env"] = {
        "platform": platform.platform(),
        "system": platform.system(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "openssl": openssl,
        "packages": packages,
        "tls_impersonate": os.environ.get("VT_TLS_IMPERSONATE"),
        "local_time": time.strftime("%Y-%m-%d %H:%M:%S%z"),
        "utc_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def service_state(esn, config, cookies, argv=None):
    """The inputs the service was configured with, as identities."""
    if not ENABLED:
        return
    _record["meta"] = {
        "argv": argv if argv is not None else sys.argv[1:],
        "esn": esn,
        "challenge_pr": fingerprint(config.get("payload_challenge_pr")),
        "challenge_pr_facts": _challenge_facts(config.get("payload_challenge_pr", "")),
        "challenge_wv": fingerprint(config.get("payload_challenge")),
        "cookie_names": sorted(cookies or {}),
        "cookie_netflixid": fingerprint((cookies or {}).get("NetflixId")),
    }


def _video_profiles(payload):
    """Which video profiles actually came back, which is the whole question."""
    out = []
    try:
        for stream in payload.get("video_tracks", []) or []:
            for dl in stream.get("streams", []) or []:
                p = dl.get("content_profile")
                if p:
                    out.append((p, "%sx%s" % (dl.get("res_w"), dl.get("res_h"))))
    except Exception:
        pass
    return sorted(set(out))


def manifest_request(endpoint, params, application_data, response=None, error=None):
    """One manifest call: what went out, and what came back."""
    if not ENABLED:
        return
    inner = (application_data or {}).get("params", {}) or {}
    # The challenge is fingerprinted above; carrying it per-request as well only
    # bloats the file.
    scrubbed = {k: v for k, v in inner.items() if k != "challenge"}
    scrubbed["challenge"] = fingerprint(inner.get("challenge"))

    entry = {
        "endpoint": endpoint,
        "query_params": params,
        "profiles_requested": scrubbed.pop("profiles", None),
        "params": scrubbed,
        "app_data_keys": sorted((application_data or {}).keys()),
    }
    if error is not None:
        entry["error"] = str(error)[:600]
    if isinstance(response, dict):
        if "error" in response:
            entry["response_error"] = response["error"]
        entry["profiles_served"] = _video_profiles(response)
    _record["requests"].append(entry)


def save():
    """Write the dump. Safe to call more than once."""
    if not ENABLED:
        return None
    _record["meta"]["duration_s"] = round(time.time() - _started, 2)
    out_dir = os.path.join(os.getcwd(), "diag")
    os.makedirs(out_dir, exist_ok=True)
    name = "nf-diag-%s-%s.json" % (
        platform.system().lower(),
        time.strftime("%Y%m%d-%H%M%S"),
    )
    path = os.path.join(out_dir, name)
    with open(path, "w", encoding="utf-8") as fd:
        json.dump(_record, fd, indent=2, sort_keys=True, default=str)
    sys.stderr.write("\n[diag] wrote %s\n" % path)
    return path


# The run ends in a lot of different ways - a clean finish, log.exit(), an
# unhandled error mid-manifest - and the failing run is the one worth capturing,
# so the dump is written on the way out regardless of which happened.
if ENABLED:
    atexit.register(save)
