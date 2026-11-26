# Running in Docker

Runs the web UI (and its downloads) on any system with Docker: Windows, Linux, or macOS
(Apple Silicon runs it emulated, so processing is slower). All tools are in the image.

## What you need

- Docker Desktop, or Docker Engine with Compose.
- A **complete installation folder**: besides what's in the repository it needs
  `scripts/subby`, `scripts/pyplayready-0.6.0` (to build) and `vinetrimmer/services`
  (at the first start).

## Start

In the installation folder:

```bash
docker compose up -d --build
```

Then open <http://127.0.0.1:8787>. The first build takes a few minutes.

On the first start your `vinetrimmer.yml`, services, service configs, devices, cookies,
certs, cache, key vault (`key_store.db`), web UI settings and pinned shows are **copied**
into `docker-data/`. From then on the container uses those copies; the originals are only
read.

## Where things are

| In `docker-data/` | |
|---|---|
| `downloads/`, `temp/` | Finished and unfinished downloads |
| `vinetrimmer.yml` | Your config (also edited on the web UI's Settings and Credentials tabs) |
| `devices/`, `cookies/`, `certs/`, `cache/`, `key_store.db` | CDM devices, logins, keys |
| `services/`, `config/services/` | Services and their configs |
| `webui/` | Queue, settings, pins, API key |

`docker-data/` holds your credentials: don't share or commit it.

Things to check after the first start:

- Download or temp folders given as Windows paths (`E:\...`) don't exist in the container;
  it uses `docker-data/downloads` and `docker-data/temp` instead. To download somewhere
  else, mount that folder in `docker-compose.yml`, e.g. `- E:/Videos:/data/downloads`.
- A key vault configured with a Windows path won't be found. Use `{data_dir}/key_store.db`.

## Updating

On the web UI's **Update** tab press **Install**, then **Restart web UI now**. The release
is kept in `docker-data/update/`, so it stays when the container is recreated, and the files
it replaced are saved in `docker-data/backups/`. If the new version doesn't start, nothing
is changed.

Some releases need a new image: ones that need other Python packages (the tab refuses them
and says so), and, when convenient, ones that change the tools in the `Dockerfile`. For
those, update the installation folder to the release, then:

```bash
docker compose up -d --build
```

A rebuilt image replaces an update installed from the tab, unless that update is newer than
the folder you built from (the `VERSION` file decides). If an update ever leaves the web UI
unusable, delete `docker-data/update/` and run `docker compose up -d --force-recreate`.

Updates don't include services. Their `.py` files are taken from the installation folder on
every start, except the ones you uploaded on the web UI's Services tab. Other files in
`docker-data/` are never overwritten.

If you remove the `.:/import:ro` line after the first start, the container no longer sees
your folder at all, and services are then updated by copying them into
`docker-data/services`.

## VPN

Nothing to add to the container: enter your provider's login on **Credentials** → **VPN**,
then switch the VPN on per download. It uses the provider's HTTP / HTTPS proxy servers
(NordVPN needs its *service credentials*), so only the jobs you choose go through it, and
the web UI stays reachable as before. WireGuard, OpenVPN and SOCKS aren't used this way.

## Adding services in the browser

The **Services** tab shows what each service needs. Uploading `.py` / `.yml` files is off in
Docker, because whoever can upload a service can run code on your server. To switch it on,
add this to the service in `docker-compose.yml` and run `docker compose up -d`:

```yaml
    environment:
      VT_WEBUI_SERVICE_UPLOAD: "1"
```

Uploaded files are kept in `docker-data/services` and `docker-data/config/services`.

## Network and the API

- Both ports are published on **this computer only**. The web UI has no login and manages
  your credentials: don't open port 8787 to a network. If you reach it through a reverse
  proxy, put a password in front of it there (for example an Access List in Nginx Proxy
  Manager) and let the proxy pass the `Host` header on.
- For the external API (see [API.md](API.md)) switch it on under Settings. To reach it
  from another machine, change `"127.0.0.1:8788:8788"` to `"8788:8788"` in
  `docker-compose.yml`. A bot running in Docker on the same computer can join the same
  compose project and use `http://vt:8788`.

## Moving to another computer

Copy the installation folder (with `docker-data/`) and run `docker compose up -d --build`.

## Useful commands

```bash
docker compose logs -f                     # what the web UI is doing
docker compose restart
docker compose down                        # stop; docker-data/ stays
docker compose exec vt python -m vinetrimmer.vinetrimmer dl --help    # command line
```

## Limits

- x86_64 Linux image only.
- YouTube's PO token provider is built into the image (Node and its packages); no `npm ci`
  needed here.
- No `ccextractor` (closed captions embedded in the video are skipped) and none of the
  Windows-only subtitle tools.
- The tool versions are pinned in the `Dockerfile`. When YouTube downloads stop working,
  update the `yt-dlp` link and checksum there and rebuild.
