"""
A local plain-HTTP proxy in front of an https:// proxy (NordVPN, SurfShark, ...).

aria2c can't talk to a proxy over TLS at all, and not every other downloader can. They
get http://127.0.0.1:<port> instead; everything sent there is forwarded over TLS to the
real proxy, with its login added. The login never appears on their command lines.
"""
import base64
import select
import socket
import ssl
import threading
from urllib.parse import unquote, urlsplit

_IDLE_SECONDS = 300
_MAX_HEAD = 65536
_bridges = {}
_lock = threading.Lock()


def local_proxy(proxy):
    """The local address standing in for an https:// proxy; any other value as it is."""
    if not proxy or not str(proxy).lower().startswith("https://"):
        return proxy
    with _lock:
        if proxy not in _bridges:
            _bridges[proxy] = _start(proxy)
        return _bridges[proxy]


def _start(proxy, context=None):
    parts = urlsplit(proxy)
    login = None
    if parts.username is not None:
        pair = f"{unquote(parts.username)}:{unquote(parts.password or '')}"
        login = b"Proxy-Authorization: Basic " + base64.b64encode(pair.encode())
    upstream = (parts.hostname, parts.port or 443)
    context = context or ssl.create_default_context()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(128)

    def accept():
        while True:
            try:
                client, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=_serve, args=(client, upstream, login, context), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    return f"http://127.0.0.1:{server.getsockname()[1]}"


def _request_head(client):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = client.recv(_MAX_HEAD)
        if not chunk or len(data) > _MAX_HEAD:
            return None
        data += chunk
    return data


def _serve(client, address, login, context):
    upstream = None
    try:
        data = _request_head(client)
        if data is None:
            return
        head, rest = data.split(b"\r\n\r\n", 1)
        lines = head.split(b"\r\n")
        tunnel = lines[0].upper().startswith(b"CONNECT ")
        replaced = (b"proxy-authorization:",) if tunnel else (b"proxy-authorization:", b"proxy-connection:",
                                                              b"connection:")
        lines = [line for line in lines if not line.lower().startswith(replaced)]
        if login:
            lines.append(login)
        if not tunnel:
            # one request per connection, so that every request carries the login
            lines.append(b"Connection: close")
        upstream = context.wrap_socket(socket.create_connection(address, timeout=30), server_hostname=address[0])
        upstream.sendall(b"\r\n".join(lines) + b"\r\n\r\n" + rest)
        upstream.settimeout(None)
        _pipe(client, upstream)
    except OSError:  # includes ssl.SSLError
        try:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
        except OSError:
            pass
    finally:
        for sock in (client, upstream):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


def _pipe(client, upstream):
    """Copy both directions until either side closes. One thread per connection: an SSL
    socket must not be read and written from two threads at once."""
    while True:
        if upstream.pending():  # already decrypted, select() wouldn't see it
            ready = [upstream]
        else:
            ready = select.select([client, upstream], [], [], _IDLE_SECONDS)[0]
        if not ready:
            return
        for source in ready:
            data = source.recv(65536)
            if not data:
                return
            (upstream if source is client else client).sendall(data)
