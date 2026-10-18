import re
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

from . import cleanup, command_builder, config_io, probe, retry, service_checks, services_info
from . import pins as pins_module
from . import updater
from .queue_manager import job_queue

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app():
    app = Flask(__name__, template_folder=str(TEMPLATE_DIR), static_folder=str(STATIC_DIR))

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
        )

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
        return render_template("update.html")

    @app.get("/settings")
    def settings_page():
        vcodec_opt = next(o for o in command_builder.GLOBAL_OPTIONS if o["key"] == "vcodec")
        return render_template("settings.html", vcodec_choices=vcodec_opt["choices"])

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
            return jsonify({"error": "A check is already running. Wait for it to finish or cancel it."}), 409
        return jsonify({**p.to_dict(), "queue_busy": job_queue.is_busy()})

    @app.get("/api/probe/<probe_id>")
    def get_probe(probe_id):
        p = probe.get(probe_id)
        if not p:
            return jsonify({"error": "not found"}), 404
        return jsonify({**p.to_dict(), "queue_busy": job_queue.is_busy()})

    @app.post("/api/probe/<probe_id>/cancel")
    def cancel_probe(probe_id):
        p = probe.get(probe_id)
        if not p:
            return jsonify({"error": "not found"}), 404
        p.cancel()
        return jsonify({"ok": True})

    # ---- api: credentials / cookies ----

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

    # ---- api: storage paths (downloads / temp directories) ----

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
        updater.restart_web_ui(sys.argv[1:])
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
            result = config_io.set_storage_paths(downloads=data.get("downloads"), temp=data.get("temp"))
        except OSError as e:
            return jsonify({"error": f"Could not create directory: {e}"}), 400
        return jsonify(result)

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
