# VT.PR.WV web UI. Build it from a complete installation folder (see DOCKER.md).
# Linux x86_64 only: several of the media tools have no other builds.

# ---- media tools, pinned and checksummed ----
FROM python:3.12-slim-bookworm AS tools
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl unzip \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /dl
RUN set -eu; \
    get() { curl -fsSL --retry 3 -o "$2" "$1"; echo "$3  $2" | sha256sum -c -; }; \
    gh=https://github.com; \
    get $gh/nilaoda/N_m3u8DL-RE/releases/download/v0.6.0-beta/N_m3u8DL-RE_v0.6.0-beta_linux-x64_20260629.tar.gz re.tar.gz \
        2ebcd450594a08c0de0bcd502a80c5d3f89a702a0fa6604199527bb8d6eee4db; \
    get $gh/shaka-project/shaka-packager/releases/download/v3.9.3/packager-linux-x64 packager \
        7a3cf35ad146fd7810b4ededab363c8a3e6121d1b2c8391f53863126186f9ee6; \
    get $gh/quietvoid/dovi_tool/releases/download/2.3.4/dovi_tool-2.3.4-x86_64-unknown-linux-musl.tar.gz dovi.tar.gz \
        1844258e13c26607b32224bf1fa82b595d3b35949f5467405fda560daad32b3f; \
    get $gh/quietvoid/hdr10plus_tool/releases/download/1.7.2/hdr10plus_tool-1.7.2-x86_64-unknown-linux-musl.tar.gz hdr.tar.gz \
        06385f37a639d61ba21d4be3150c863846933bc3b58110e094d8fc8f1c2249f2; \
    get $gh/denoland/deno/releases/download/v2.9.8/deno-x86_64-unknown-linux-gnu.zip deno.zip \
        e890975bc2549807bbcc0b584ffafda72cc7c93349dd99c80483dd784f6c4609; \
    get $gh/yt-dlp/yt-dlp/releases/download/2026.11.23/yt-dlp_linux yt-dlp \
        249ddb058234276bc449b9cd9492aad9f133bda5923c61a9e39983ae114ef17d; \
    get https://www.bok.net/Bento4/binaries/Bento4-SDK-1-6-0-641.x86_64-unknown-linux.zip bento4.zip \
        d48dc6b164941212e5614237b4d9aeff81d4d111ee8b1508892764078a0870e8; \
    mkdir -p x /out/bin /out/linux-binaries; \
    tar -xzf re.tar.gz -C x; tar -xzf dovi.tar.gz -C x; tar -xzf hdr.tar.gz -C x; \
    unzip -q deno.zip -d x; unzip -q bento4.zip -d x; \
    for name in N_m3u8DL-RE dovi_tool hdr10plus_tool deno mp4decrypt mp4dump; do \
        install -m 755 "$(find x -type f -name "$name" | head -n 1)" "/out/bin/$name"; \
    done; \
    install -m 755 packager /out/bin/packager; \
    # The program puts linux-binaries first on PATH. The standalone yt-dlp comes with all
    # its optional parts (the one among the Python packages doesn't); the Python packages
    # also bring an unrelated mp4dump that must not win.
    install -m 755 yt-dlp /out/linux-binaries/yt-dlp; \
    cp /out/bin/mp4dump /out/linux-binaries/mp4dump

# ---- YouTube PO token provider: its JS packages (canvas is built per platform) ----
FROM node:22-bookworm-slim AS pot
WORKDIR /pot
COPY vinetrimmer/pot_provider/server/package.json vinetrimmer/pot_provider/server/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund

# ---- Python packages ----
FROM python:3.12-slim-bookworm AS deps
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libffi-dev \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "poetry==2.4.1" && python -m venv /opt/venv
WORKDIR /app
COPY pyproject.toml poetry.lock ./
COPY scripts/subby scripts/subby
COPY scripts/pyplayready-0.6.0 scripts/pyplayready-0.6.0
RUN VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH \
    poetry install --only main --no-root --no-interaction

# ---- the app ----
FROM python:3.12-slim-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    aria2 ca-certificates ffmpeg libicu72 libmediainfo0v5 mkvtoolnix tini \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=tools /out/bin/ /usr/local/bin/
COPY --from=tools /out/linux-binaries/ /app/linux-binaries/
COPY --from=deps /opt/venv /opt/venv
COPY scripts/subby scripts/subby
COPY scripts/pyplayready-0.6.0 scripts/pyplayready-0.6.0
COPY vinetrimmer vinetrimmer
COPY webui webui
COPY assets assets
# Node runs the token provider (yt-dlp's own challenge is solved by deno).
COPY --from=pot /usr/local/bin/node /usr/local/bin/node
COPY --from=pot /pot/node_modules vinetrimmer/pot_provider/server/node_modules
COPY run_webui.py VERSION pyproject.toml poetry.lock ./
COPY docker/entrypoint.sh /usr/local/bin/vt-entrypoint
# What this image was built with, for updates from the web UI (webui/docker_update.py).
COPY Dockerfile VERSION pyproject.toml poetry.lock docker/entrypoint.sh /opt/image/

# Everything personal lives in the /data volume; the app finds it through links.
RUN set -eu; \
    sed -i 's/\r$//' /usr/local/bin/vt-entrypoint; chmod 755 /usr/local/bin/vt-entrypoint; \
    mkdir -p vinetrimmer/config; \
    for item in vinetrimmer.yml services devices cookies certs cache logs key_store.db; do \
        rm -rf "vinetrimmer/$item"; ln -s "/data/$item" "vinetrimmer/$item"; \
    done; \
    rm -rf vinetrimmer/config/services; ln -s /data/config/services vinetrimmer/config/services; \
    ln -s /data/downloads downloads; ln -s /data/temp temp; \
    # The standalone yt-dlp can't see Python packages: give it the provider's plugin.
    mkdir -p linux-binaries/yt-dlp-plugins/bgutil; \
    cp -r /opt/venv/lib/python3.12/site-packages/yt_dlp_plugins linux-binaries/yt-dlp-plugins/bgutil/

# The Update tab replaces program files, whichever user the container runs as. The build
# id tells a rebuilt image from a recreated container.
RUN set -eu; \
    cat /proc/sys/kernel/random/uuid > /opt/image/build; \
    find /app \( -path /app/linux-binaries -o -name node_modules \) -prune \
        -o \( -type f -o -type d \) -exec chmod a+rwX {} +

ENV PATH=/opt/venv/bin:$PATH \
    VIRTUAL_ENV=/opt/venv \
    PYTHONUNBUFFERED=1 \
    PYTHONWARNINGS=ignore::SyntaxWarning \
    HOME=/data/home \
    VT_DOCKER=1 \
    VT_WEBUI_DATA_DIR=/data/webui \
    VT_WEBUI_HOST=0.0.0.0 \
    VT_API_HOST=0.0.0.0 \
    VT_API_PORT=8788

VOLUME /data
EXPOSE 8787 8788
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/favicon.ico', timeout=5)"
ENTRYPOINT ["/usr/bin/tini", "--", "vt-entrypoint"]
CMD ["python", "run_webui.py", "--port", "8787"]
