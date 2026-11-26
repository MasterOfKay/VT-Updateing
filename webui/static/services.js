/* The Services tab: what each service needs, and replacing or adding its files. */

(() => {
  const el = (id) => document.getElementById(id);
  let services = [];
  let selected = null; // a service name, "" while adding a new one, null before a choice
  let chosen = []; // the files picked for upload
  let canUpload = false;

  function request(url, options) {
    return fetch(url, options).then(async (r) => {
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.error || "That didn't work. Restart the web UI if it was just updated.");
      return body;
    });
  }

  const date = (seconds) => new Date(seconds * 1000).toLocaleDateString(undefined,
    { day: "numeric", month: "short", year: "numeric" });

  function renderList() {
    const wanted = el("service-search").value.trim().toLowerCase();
    el("service-list").innerHTML = services
      .filter((s) => !wanted || [s.name, ...s.aliases].some((a) => a.toLowerCase().includes(wanted)))
      .map((s) => {
        const notes = [
          s.added ? "added here" : s.uploaded ? "replaced here" : "",
          s.config ? "" : "no .yml",
        ].filter(Boolean).join(", ");
        return `<button type="button" class="rail-item${s.name === selected ? " active" : ""}" role="option"
            aria-selected="${s.name === selected}" data-service="${escapeHtml(s.name)}">
          <span class="rail-title">${escapeHtml(s.name)}</span>
          ${notes ? `<span class="rail-sub">${escapeHtml(notes)}</span>` : ""}
        </button>`;
      }).join("") || `<p class="hint">No service by that name.</p>`;
  }

  function loadList() {
    return request("/api/service-files").then((list) => {
      services = list;
      renderList();
    });
  }

  function showFiles(files) {
    chosen = [...files];
    el("chosen-files").innerHTML = chosen.map((f) => `<li><code>${escapeHtml(f.name)}</code></li>`).join("");
    el("btn-upload-service").disabled = !canUpload || !chosen.length;
  }

  function resetUpload() {
    el("service-files").value = "";
    say(el("upload-message"), "");
    showFiles([]);
  }

  function showReport(report) {
    const { service } = report;
    const files = [
      service.script ? `<li><code>services/${escapeHtml(service.script.name)}</code> <span class="hint">changed ${date(service.script.modified)}</span></li>`
        : "<li>No script</li>",
      service.config ? `<li><code>config/services/${escapeHtml(service.config.name)}</code> <span class="hint">changed ${date(service.config.modified)}</span></li>`
        : "<li>No .yml in config/services</li>",
      service.override ? `<li class="hint">Your own settings in services/${escapeHtml(service.name.toLowerCase())}.yml are applied on top.</li>` : "",
    ];
    el("service-file-list").innerHTML = files.join("");

    const limits = [...report.limits];
    el("limits-box").hidden = !limits.length;
    el("service-limits").innerHTML = limits.map((l) => `<li>${escapeHtml(l)}</li>`).join("");

    const missing = report.missing_packages.length
      ? `<p class="err">Python packages it imports that aren't installed: <code>${report.missing_packages.map(escapeHtml).join("</code> <code>")}</code></p>`
      : "";
    el("service-requirements").innerHTML = missing +
      (report.check ? renderServiceCheck(report.check) : `<p class="hint">The requirements can't be worked out.</p>`);

    const restore = el("btn-restore-service");
    restore.hidden = !(canUpload && service.can_restore);
    restore.textContent = service.added ? "Remove this service" : "Restore the earlier files";
  }

  function result(text, kind) {
    const box = el("service-result");
    box.className = `result${kind ? ` result-${kind}` : ""}`;
    box.textContent = text;
  }

  function open(name) {
    selected = name;
    renderList();
    resetUpload();
    result("", "");
    el("service-empty").hidden = true;
    el("service-detail").hidden = false;
    const adding = name === "";
    el("report-column").hidden = adding;
    el("files-box").hidden = adding;
    el("btn-restore-service").hidden = true;
    const service = services.find((s) => s.name === name);
    el("service-title").textContent = adding ? "Add a new service" : name;
    el("service-aliases").textContent = service && service.aliases.length ? `Also known as ${service.aliases.join(", ")}` : "";
    el("upload-legend").textContent = adding ? "Its files" : "Replace files";
    el("upload-hint").innerHTML = adding
      ? `The script (<code>.py</code>) is required; add its <code>.yml</code> if it has one. The service's name comes
         from the class in the script. It's checked before it's kept: if vinetrimmer can't start with it, nothing changes.`
      : `Drop a new <code>.py</code>, a new <code>.yml</code>, or both. The <code>.py</code> goes to
         <code>vinetrimmer/services</code>, the <code>.yml</code> to <code>vinetrimmer/config/services</code>.
         What was there before is kept, so you can go back.`;
    if (adding) return;
    el("service-requirements").innerHTML = `<p class="hint">Looking…</p>`;
    el("limits-box").hidden = true;
    el("service-file-list").innerHTML = "";
    request(`/api/service-files/${encodeURIComponent(name)}`)
      .then((report) => {
        if (selected !== name) return;
        showReport(report);
        if (report.error) result(report.error, "err");
      })
      .catch((e) => result(e.message, "err"));
  }

  function upload() {
    const form = new FormData();
    chosen.forEach((file) => form.append("files", file, file.name));
    if (selected) form.append("service", selected);
    const button = el("btn-upload-service");
    button.disabled = true;
    say(el("upload-message"), "Uploading and checking…");
    request("/api/service-files", { method: "POST", body: form })
      .then((report) => loadList().then(() => {
        const adding = selected === "";
        open(report.service.name);
        showReport(report);
        result(report.error || `${adding ? "Added" : "Replaced"}: ${report.saved.join(" and ")}. ` +
          "It's in the service list on the Download tab.", report.error ? "err" : "ok");
      }))
      .catch((e) => {
        say(el("upload-message"), e.message, "err");
        button.disabled = false;
      });
  }

  function restore() {
    const service = services.find((s) => s.name === selected);
    const question = service && service.added
      ? `Remove ${selected}? Its files are deleted.`
      : `Put back the files ${selected} had before the latest upload?`;
    if (!confirm(question)) return;
    request(`/api/service-files/${encodeURIComponent(selected)}/restore`, { method: "POST" })
      .then((res) => loadList().then(() => {
        if (res.removed) {
          selected = null;
          renderList();
          el("service-detail").hidden = true;
          el("service-empty").hidden = false;
          el("service-empty").textContent = `${res.service} was removed.`;
        } else {
          open(res.service);
          result("The earlier files are back.", "ok");
        }
      }))
      .catch((e) => result(e.message, "err"));
  }

  document.addEventListener("DOMContentLoaded", () => {
    canUpload = !!el("services-page").dataset.canUpload;
    el("service-search").addEventListener("input", renderList);
    el("service-list").addEventListener("click", (e) => {
      const item = e.target.closest("[data-service]");
      if (item) open(item.dataset.service);
    });
    el("btn-new-service").addEventListener("click", () => open(""));
    el("service-files").addEventListener("change", () => showFiles(el("service-files").files));
    const drop = el("service-drop");
    ["dragenter", "dragover"].forEach((type) => drop.addEventListener(type, (e) => {
      e.preventDefault();
      if (canUpload) drop.classList.add("over");
    }));
    ["dragleave", "drop"].forEach((type) => drop.addEventListener(type, () => drop.classList.remove("over")));
    drop.addEventListener("drop", (e) => {
      e.preventDefault();
      if (canUpload && e.dataTransfer.files.length) showFiles(e.dataTransfer.files);
    });
    el("btn-upload-service").addEventListener("click", upload);
    el("btn-restore-service").addEventListener("click", restore);
    loadList().catch((e) => {
      el("service-empty").textContent = e.message;
    });
  });
})();
