import re
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_from_directory, stream_with_context

from . import cleanup, command_builder, config_io, paths, probe, retry, service_checks, services_info
from . import api_server, api_settings, discover, discover_links
from . import config_editor, folders, guard, service_manager, vpn
from . import pins as pins_module
from . import updater
from .queue_manager import job_queue

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(ui_port=None, host=None):
    app = Flask(__name__, template_folder=str(TEMPLATE_DIR), static_folder=str(STATIC_DIR))
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # cookie files and service scripts are small
    guard.install(app)
    can_upload = guard.upload_allowed(host)

    # Browsers cache /static files heuristically (Flask sends no max-age), which kept
    # serving an outdated app.js after updates. Versioning the URL by the file's
    # modification time makes every change load immediately.
    # Changes every time the web UI starts: pages keep their unsaved state (the Download
    # form, a check result) in the browser tab only for as long as this run lasts.
    boot_id = uuid.uuid4().hex

    @app.context_processor
    def _boot_id():
        return {"boot_id": boot_id, "app_version": updater.current_version()}

    pins = pins_module.init(job_queue)
    external_api = api_server.init(job_queue, ui_port)

    @app.context_processor
    def _asset_version():
        def asset_version(filename):
            try:
                return int((STATIC_DIR / filename).stat().st_mtime)
            except OSError:
                return 0
        return {"asset_version": asset_version}

    def _build_argv_from_request(data):
        service_name = data.get("service")
        info = services_info.get_service_options(service_name) if service_name else None
        argv = command_builder.build_argv(data, service_name, info["options"] if info else [])
        return argv, info

    # ---- pages ----

    @app.get("/")
    def index():
        return download()

    @app.get("/download")
    def download():
        # alang/slang/vcodec defaults saved on the Settings page (vinetrimmer.yml's
        # arguments: section) are prefilled as real field values, not just shown as
        # greyed placeholders - so they're visibly part of the command preview and
        # passed explicitly on the command line, and can still be edited per download.
        configured_defaults = config_io.get_argument_defaults()
        options = []
        for opt in command_builder.GLOBAL_OPTIONS:
            opt = dict(opt)
            configured = configured_defaults.get(opt["key"])
            if configured:
                opt["value"] = configured
            options.append(opt)

        return render_template(
            "download.html",
            services=services_info.list_services(),
            primary_options=[o for o in options if o["section"] == "primary"],
            advanced_options=[o for o in options if o["section"] == "advanced"],
            settings=retry.get_settings(),
            vpn=vpn.status(),
        )

    @app.get("/favicon.ico")
    def favicon():
        return send_from_directory(STATIC_DIR, "favicon.ico", max_age=86400)

    @app.get("/discover")
    def discover_page():
        return render_template("discover.html", countries=discover.COUNTRIES, languages=discover.LANGUAGES,
                               settings=retry.get_settings())

    def _discover(call):
        """Run a Discover lookup for the ?country= / ?language= asked for."""
        settings = retry.get_settings()
        country = request.args.get("country") or settings["discover_country"]
        language = request.args.get("language") or settings["discover_language"]
        if country not in dict(discover.COUNTRIES) or language not in dict(discover.LANGUAGES):
            return jsonify({"error": "Unknown country or language."}), 400
        try:
            return jsonify(call(country, language))
        except discover.DiscoverError as e:
            return jsonify({"error": str(e)}), 502

    @app.get("/api/discover/providers")
    def discover_providers():
        return _discover(lambda country, _: discover.providers(country))

    @app.get("/api/discover/titles")
    def discover_titles():
        args = request.args
        services = [s for s in (args.get("services") or "").split(",") if s]
        kind = args.get("kind") if args.get("kind") in ("movie", "show") else None
        query = (args.get("q") or "").strip()[:100] or None
        access = args.get("access") if args.get("access") in discover.ACCESS_FILTERS else None
        if args.get("mode") == "new" and not query:
            return _discover(lambda c, l: discover.new(c, l, services, kind, args.get("date"), args.get("cursor"), access))
        offset = max(0, args.get("offset", 0, type=int))
        if args.get("mode") == "all" and not query:
            return _discover(lambda c, l: discover.browse(c, l, services, kind, args.get("sort"), offset, access))
        return _discover(lambda c, l: discover.popular(c, l, services, kind, query, min(offset, 2000), access))

    @app.get("/api/discover/title/<title_id>")
    def discover_title(title_id):
        return _discover(lambda c, l: discover.details(title_id, c, l))

    @app.get("/api/discover/season/<season_id>")
    def discover_season(season_id):
        return _discover(lambda c, l: discover.episodes(season_id, c, l))

    @app.post("/api/discover/resolve")
    def discover_resolve():
        data = request.get_json(force=True, silent=True) or {}
        try:
            return jsonify(discover_links.resolve(str(data.get("url") or "")))
        except ValueError as e:
            return jsonify({"error": str(e)}), 502

    @app.get("/queue")
    def queue_page():
        return render_template("queue.html")

    @app.get("/console")
    def console_page():
        return render_template("console.html", job_id=request.args.get("job", ""),
                               console_view=retry.get_settings()["console_view"])

    @app.get("/credentials")
    def credentials_page():
        return render_template("credentials.html", services=services_info.list_services())

    @app.get("/update")
    def update_page():
        return render_template("update.html", in_docker=paths.in_docker())

    @app.get("/settings")
    def settings_page():
        return render_template("settings.html")

    @app.get("/services")
    def services_page():
        return render_template("services.html", can_upload=can_upload, in_docker=paths.in_docker())

    # ---- api: services ----

    @app.get("/api/services/<service_name>/info")
    def service_info(service_name):
        info = services_info.get_service_options(service_name)
        if not info:
            return jsonify({"error": "unknown service"}), 404
        info = dict(info)
        info["check"] = service_checks.check_service(
            info["name"],
            profile=(request.args.get("profile") or "").strip() or None,
            cdm_override=(request.args.get("cdm") or "").strip() or None,
            proxy=(request.args.get("proxy") or "").strip() or None,
            no_proxy=request.args.get("no_proxy") in ("1", "true"),
            vpn_country=(request.args.get("vpn") or "").strip().lower()[:2] or None,
        )
        return jsonify(info)

    # ---- api: command preview / jobs ----

    @app.post("/api/preview")
    def preview():
        data = request.get_json(force=True, silent=True) or {}
        try:
            argv, _ = _build_argv_from_request(data)
        except command_builder.CommandError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"argv": argv, "command": command_builder.format_command(argv)})

    @app.post("/api/jobs")
    def create_job():
        data = request.get_json(force=True, silent=True) or {}
        mode = data.get("mode", "queue")
        try:
            argv, _ = _build_argv_from_request(data)
        except command_builder.CommandError as e:
            return jsonify({"error": str(e)}), 400
        command = command_builder.format_command(argv)
        job = job_queue.add(
            service=data.get("service"),
            title=data.get("title", ""),
            argv=argv,
            command=command,
            auto_start=(mode == "run"),
            form={k: v for k, v in data.items() if k != "mode"},
        )
        return jsonify(job.to_dict())

    @app.get("/api/jobs")
    def list_jobs():
        return jsonify(job_queue.list_jobs())

    @app.get("/api/jobs/<job_id>")
    def get_job(job_id):
        job = job_queue.get(job_id)
        if not job:
            return jsonify({"error": "not found"}), 404
        return jsonify(job.to_dict())

    @app.post("/api/jobs/<job_id>/cancel")
    def cancel_job(job_id):
        return jsonify({"ok": job_queue.cancel(job_id)})

    @app.post("/api/jobs/<job_id>/retry")
    def retry_job(job_id):
        try:
            new_job = job_queue.retry(job_id)
        except command_builder.CommandError as e:
            return jsonify({"error": str(e)}), 400
        if not new_job:
            return jsonify({"error": "Only failed or cancelled jobs that weren't retried yet can be retried."}), 400
        return jsonify(new_job.to_dict())

    @app.post("/api/jobs/<job_id>/stop_after_episode")
    def stop_after_episode(job_id):
        if not job_queue.request_stop(job_id):
            return jsonify({"error": "Only a running download can be stopped like this."}), 400
        return jsonify({"ok": True})

    @app.post("/api/jobs/<job_id>/cancel_stop")
    def cancel_stop(job_id):
        return jsonify({"ok": job_queue.cancel_stop(job_id)})

    @app.post("/api/jobs/<job_id>/continue")
    def continue_job(job_id):
        new_job = job_queue.continue_job(job_id)
        if not new_job:
            return jsonify({"error": "Only a stopped download that wasn't continued yet can be continued."}), 400
        return jsonify(new_job.to_dict())

    @app.post("/api/jobs/<job_id>/input")
    def job_input(job_id):
        job = job_queue.get(job_id)
        if not job:
            return jsonify({"error": "not found"}), 404
        data = request.get_json(force=True, silent=True) or {}
        if not job.send_input(str(data.get("text", ""))):
            return jsonify({"error": "This job isn't running (or no longer accepts input)."}), 400
        return jsonify({"ok": True})

    @app.post("/api/jobs/start_queue")
    def start_queue():
        job_queue.start()
        return jsonify({"ok": True})

    @app.post("/api/jobs/pause_queue")
    def pause_queue():
        job_queue.pause()
        return jsonify(job_queue.state())

    @app.get("/api/queue/state")
    def queue_state():
        return jsonify(job_queue.state())

    @app.post("/api/jobs/clear_finished")
    def clear_finished():
        job_queue.clear_finished()
        return jsonify({"ok": True})

    @app.get("/api/jobs/<job_id>/stream")
    def stream_job(job_id):
        job = job_queue.get(job_id)
        if not job:
            return jsonify({"error": "not found"}), 404

        def gen():
            sub = job.subscribe()
            try:
                while True:
                    line = sub.get()
                    if line is None:
                        yield "event: end\ndata: end\n\n"
                        break
                    if isinstance(line, tuple):
                        yield f"event: {line[0]}\ndata: {line[1]}\n\n"
                        continue
                    yield f"data: {line.rstrip(chr(10))}\n\n"
            finally:
                job.unsubscribe(sub)

        return Response(stream_with_context(gen()), mimetype="text/event-stream")

    # ---- api: availability check (experimental, see probe.py) ----

    @app.post("/api/probe")
    def start_probe():
        if not retry.get_settings()["experimental_check"]:
            return jsonify({"error": "Turn on the experimental availability check in Settings first."}), 400
        data = request.get_json(force=True, silent=True) or {}
        if not data.get("service") or not (data.get("title") or "").strip():
            return jsonify({"error": "Select a service and enter a link / ID first."}), 400
        mode = data.get("probe_mode") if data.get("probe_mode") in probe.MODES else None
        try:
            _build_argv_from_request(data)  # same validation as a real job
        except command_builder.CommandError as e:
            return jsonify({"error": str(e)}), 400
        episode = (data.get("check_episode") or "").strip()
        if episode and not re.match(r"^S\d+(E\d+)?$", episode, re.IGNORECASE):
            return jsonify({"error": "Episode to check: use e.g. S02E01 (or S02 for a season's first episode)."}), 400
        combos = data.get("combos") if isinstance(data.get("combos"), list) else None
        p = probe.start(data, mode, combos=combos, quality=str(data.get("check_quality") or "") or None,
                        extend=data.get("extend") or None)
        if p == "nothing":
            return jsonify({"error": "Tick at least one codec / range combination to check."}), 400
        if not p:
            return jsonify({"error": "A check is already running (possibly one asked for over the API). "
                                     "Wait for it to finish."}), 409
        return jsonify({**p.to_dict(), "queue_busy": _busy_warning(p.service)})

    def _busy_warning(service):
        return retry.get_settings()["warn_check_busy"] and job_queue.is_busy(service)

    @app.get("/api/probe/<probe_id>")
    def get_probe(probe_id):
        p = probe.get(probe_id)
        if not p:
            return jsonify({"error": "not found"}), 404
        return jsonify({**p.to_dict(), "queue_busy": _busy_warning(p.service)})

    @app.get("/api/check-busy")
    def check_busy():
        """Asked right before a check starts: should the page warn first?"""
        return jsonify({"warn": _busy_warning(request.args.get("service", ""))})

    @app.post("/api/probe/<probe_id>/cancel")
    def cancel_probe(probe_id):
        p = probe.get(probe_id)
        if not p:
            return jsonify({"error": "not found"}), 404
        p.cancel()
        return jsonify({"ok": True})

    # ---- api: credentials / cookies ----

    @app.get("/api/credentials")
    def credential_overview():
        return jsonify(config_io.credential_overview([s["name"] for s in services_info.list_services()]))

    @app.post("/api/credentials/<service_name>")
    def set_credential(service_name):
        data = request.get_json(force=True, silent=True) or {}
        try:
            config_io.set_credential(
                service_name,
                data.get("username", ""),
                data.get("password", ""),
                profile=(data.get("profile") or "").strip() or None,
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": True})

    @app.post("/api/cookies/<service_name>")
    def upload_cookie(service_name):
        file = request.files.get("file")
        if not file or not file.filename:
            return jsonify({"error": "no file provided"}), 400
        profile = (request.form.get("profile") or "default").strip() or "default"
        path = config_io.save_cookie(service_name, profile, file)
        return jsonify({"ok": True, "path": path})

    @app.get("/api/external-api")
    def external_api_status():
        return jsonify(external_api.status())

    @app.post("/api/external-api")
    def external_api_save():
        # Only real JSON requests: another website can't send those here unasked.
        if not request.is_json:
            return jsonify({"error": "Send JSON."}), 415
        api_settings.save(request.get_json(silent=True) or {})
        external_api.apply()
        return jsonify(external_api.status())

    @app.post("/api/external-api/key")
    def external_api_new_key():
        if not request.is_json:
            return jsonify({"error": "Send JSON."}), 415
        api_settings.regenerate_key()
        return jsonify(external_api.status())

    # ---- api: updates (Update tab, see updater.py) ----

    def _busy_reason():
        if job_queue.is_busy():
            return "A download is running. Wait until it's done (or pause the queue after it)."
        if probe.active():
            return "An availability check is running. Wait for it to finish."
        return None

    @app.get("/api/update")
    def update_info():
        force = request.args.get("force") in ("1", "true")
        info = updater.releases(force=force, include_prerelease=retry.get_settings()["update_prereleases"])
        return jsonify({**info, "prereleases": retry.get_settings()["update_prereleases"],
                        "busy": _busy_reason()})

    @app.post("/api/update/install")
    def update_install():
        busy = _busy_reason()
        if busy:
            return jsonify({"error": busy}), 409
        tag = (request.get_json(force=True, silent=True) or {}).get("tag")
        try:
            result = updater.install(tag, include_prerelease=True)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": f"Update failed: {type(e).__name__}: {e}"}), 500
        return jsonify(result)

    @app.post("/api/update/restart")
    def update_restart():
        busy = _busy_reason()
        if busy:
            return jsonify({"error": busy}), 409
        import sys
        restart = updater.restart_in_place if paths.in_docker() else updater.restart_web_ui
        restart(sys.argv[1:])
        return jsonify({"ok": True})

    # ---- api: pinned shows (see pins.py) ----

    @app.get("/api/pins")
    def list_pins():
        return jsonify(pins.list())

    @app.post("/api/pins")
    def create_pin():
        data = request.get_json(force=True, silent=True) or {}
        try:
            if data.get("job_id"):
                pin = pins.pin_job(data["job_id"])
            else:
                _build_argv_from_request(data)  # same validation as a real job
                pin = pins.pin_form(data)
        except (ValueError, command_builder.CommandError) as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"id": pin["id"]})

    @app.post("/api/pins/<pin_id>/check")
    def check_pin(pin_id):
        job = pins.check_now(pin_id)
        if not job:
            return jsonify({"error": "not found"}), 404
        return jsonify({"job_id": job.id})

    @app.post("/api/pins/<pin_id>/interval")
    def pin_interval(pin_id):
        hours = (request.get_json(force=True, silent=True) or {}).get("hours")
        try:
            pin = pins.set_interval(pin_id, hours)
        except (TypeError, ValueError):
            return jsonify({"error": "hours must be a number"}), 400
        if not pin:
            return jsonify({"error": "not found"}), 404
        return jsonify({"ok": True})

    @app.post("/api/pins/<pin_id>/vpn")
    def pin_vpn(pin_id):
        country = (request.get_json(force=True, silent=True) or {}).get("country")
        try:
            pin = pins.set_vpn(pin_id, str(country or ""))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        if not pin:
            return jsonify({"error": "not found"}), 404
        return jsonify({"ok": True})

    @app.delete("/api/pins/<pin_id>")
    def delete_pin(pin_id):
        return jsonify({"ok": pins.unpin(pin_id)})

    @app.get("/api/storage-paths")
    def get_storage_paths():
        return jsonify(config_io.get_storage_paths())

    @app.post("/api/storage-paths")
    def set_storage_paths():
        data = request.get_json(force=True, silent=True) or {}
        try:
            result = config_io.set_storage_paths(downloads=data.get("downloads"), temp=data.get("temp"),
                                                 fonts=data.get("fonts"))
        except OSError as e:
            return jsonify({"error": f"Could not create directory: {e}"}), 400
        return jsonify(result)

    # ---- api: folder picker (see folders.py) ----

    @app.get("/api/folders")
    def list_folders():
        return jsonify(folders.listing(request.args.get("path")))

    @app.post("/api/folders")
    def create_folder():
        data = request.get_json(force=True, silent=True) or {}
        try:
            return jsonify(folders.create(str(data.get("parent") or ""), str(data.get("name") or "")))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    # ---- api: vinetrimmer.yml (see config_editor.py) ----

    @app.get("/api/config")
    def get_config():
        return jsonify(config_editor.read())

    @app.post("/api/config/<section>")
    def save_config(section):
        try:
            return jsonify(config_editor.save(section, request.get_json(force=True, silent=True)))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except OSError as e:
            return jsonify({"error": f"vinetrimmer.yml couldn't be written: {e}"}), 500

    @app.post("/api/vaults/check")
    def check_vaults():
        return jsonify(config_editor.check_vaults())

    @app.get("/api/service-options/<service_name>")
    def service_option_list(service_name):
        info = services_info.get_service_options(service_name)
        if not info:
            return jsonify({"error": "unknown service"}), 404
        return jsonify(info["options"])

    # ---- api: VPN (see vpn.py) ----

    @app.get("/api/vpn")
    def vpn_status():
        return jsonify({**vpn.status(), "home": service_checks.current_country()[0]})

    @app.post("/api/vpn")
    def vpn_save():
        try:
            vpn.save(request.get_json(force=True, silent=True) or {})
        except (ValueError, TypeError, AttributeError) as e:
            return jsonify({"error": str(e)}), 400
        return vpn_status()

    @app.post("/api/vpn/test")
    def vpn_check():
        data = request.get_json(force=True, silent=True) or {}
        try:
            return jsonify(vpn.test(str(data.get("country") or "")))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    # ---- api: service files (Services tab, see service_manager.py) ----

    def _upload_refused():
        if not can_upload:
            return jsonify({"error": "Changing service files is switched off. See the note on this page."}), 403
        if job_queue.is_busy() or probe.active():
            return jsonify({"error": "A download or check is running. Wait until it's done."}), 409
        return None

    @app.get("/api/service-files")
    def service_files():
        return jsonify(service_manager.listing())

    @app.get("/api/service-files/<service_name>")
    def service_report(service_name):
        if service_name not in {s["name"] for s in services_info.list_services()}:
            return jsonify({"error": "unknown service"}), 404
        return jsonify(service_manager.report(service_name))

    @app.post("/api/service-files")
    def service_upload():
        refused = _upload_refused()
        if refused:
            return refused
        files = [(f.filename, f.read()) for f in request.files.getlist("files") if f and f.filename]
        try:
            return jsonify(service_manager.install(files, request.form.get("service") or None))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except OSError as e:
            return jsonify({"error": f"The files couldn't be written: {e}"}), 500

    @app.post("/api/service-files/<service_name>/restore")
    def service_restore(service_name):
        refused = _upload_refused()
        if refused:
            return refused
        if service_name not in {s["name"] for s in services_info.list_services()}:
            return jsonify({"error": "unknown service"}), 404
        try:
            return jsonify(service_manager.restore(service_name))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except OSError as e:
            return jsonify({"error": f"The files couldn't be restored: {e}"}), 500

    # ---- api: temp folder (manual full cleanup) ----

    def _temp_info():
        path = config_io.get_storage_paths()["temp_effective"]
        count, size = cleanup.usage(path)
        return {"path": path, "count": count, "bytes": size, "busy": job_queue.is_busy()}

    @app.get("/api/temp")
    def get_temp():
        return jsonify(_temp_info())

    @app.post("/api/temp/clean")
    def clean_temp():
        # Wiping temp under a running download would destroy it.
        if job_queue.is_busy():
            return jsonify({"error": "A download is running. Wait until it's done."}), 409
        removed, failed = cleanup.wipe(_temp_info()["path"])
        return jsonify({**_temp_info(), "removed": len(removed), "failed": failed})

    # ---- api: preferences (progress display, default alang/slang/vcodec) ----

    def _preferences():
        return {
            "progress_display": config_io.get_progress_display(),
            "defaults": config_io.get_argument_defaults(),
            "retry": retry.get_settings(),
        }

    @app.get("/api/preferences")
    def get_preferences():
        return jsonify(_preferences())

    @app.post("/api/preferences")
    def set_preferences():
        data = request.get_json(force=True, silent=True) or {}
        if "progress_display" in data:
            config_io.set_progress_display(bool(data["progress_display"]))
        if "defaults" in data:
            config_io.set_argument_defaults(data.get("defaults") or {})
        if "retry" in data:
            retry.set_settings(data.get("retry") or {})
        return jsonify(_preferences())

    return app
