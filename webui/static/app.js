document.addEventListener("DOMContentLoaded", () => {
  if (document.getElementById("download-form")) initDownloadPage();
  if (document.getElementById("job-table-body")) initQueuePage();
  if (document.getElementById("console-output")) initConsolePage();
  if (document.getElementById("cred-service")) initCredentialsPage();
  if (document.getElementById("storage-paths")) initStoragePaths();
  if (document.getElementById("setting-progress-display")) initPreferences();
  if (document.getElementById("update-list")) initUpdatePage();
  checkUpdateBadge();
});

/* ---------------- Storage paths (downloads / temp folders) ---------------- */

function initStoragePaths() {
  loadStoragePaths();
  document.getElementById("btn-save-storage").addEventListener("click", saveStoragePaths);
  const cleanBtn = document.getElementById("btn-clean-temp");
  if (cleanBtn) {
    loadTempUsage();
    cleanBtn.addEventListener("click", cleanTempFolder);
  }
}

function formatBytes(n) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}

function showTempUsage(info) {
  document.getElementById("temp-usage").textContent =
    `${info.count} item(s), ${formatBytes(info.bytes)} in ${info.path}` + (info.busy ? " (a job is running)" : "");
  document.getElementById("btn-clean-temp").disabled = info.busy || info.count === 0;
}

function loadTempUsage() {
  fetch("/api/temp")
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error("Restart the web UI to use this."))))
    .then(showTempUsage)
    .catch((e) => { document.getElementById("temp-usage").textContent = e.message; });
}

function cleanTempFolder() {
  const msg = document.getElementById("temp-message");
  if (!confirm("Delete EVERYTHING in the temp folder?\n\nDon't do this while vinetrimmer is downloading " +
               "in a terminal outside this web UI. That download would break.")) return;
  fetch("/api/temp/clean", { method: "POST" })
    .then(async (r) => {
      const body = await r.json();
      if (!r.ok) throw new Error(body.error || "Failed to clean");
      return body;
    })
    .then((info) => {
      showTempUsage(info);
      msg.textContent = `Removed ${info.removed} item(s).` +
        (info.failed.length ? ` Could not delete (in use?): ${info.failed.join(", ")}` : "");
    })
    .catch((e) => { msg.textContent = e.message; });
}

function loadStoragePaths() {
  fetch("/api/storage-paths")
    .then((r) => r.json())
    .then((paths) => {
      document.getElementById("storage-downloads").value = paths.downloads || "";
      document.getElementById("storage-downloads-effective").textContent = paths.downloads_effective;
      document.getElementById("storage-temp").value = paths.temp || "";
      document.getElementById("storage-temp-effective").textContent = paths.temp_effective;
    });
}

function saveStoragePaths() {
  const msg = document.getElementById("storage-message");
  const payload = {
    downloads: document.getElementById("storage-downloads").value,
    temp: document.getElementById("storage-temp").value,
  };
  fetch("/api/storage-paths", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
    .then(async (r) => {
      const body = await r.json();
      if (!r.ok) throw new Error(body.error || "Failed to save");
      return body;
    })
    .then((paths) => {
      document.getElementById("storage-downloads").value = paths.downloads || "";
      document.getElementById("storage-downloads-effective").textContent = paths.downloads_effective;
      document.getElementById("storage-temp").value = paths.temp || "";
      document.getElementById("storage-temp-effective").textContent = paths.temp_effective;
      msg.textContent = "Saved. New jobs will use these paths right away.";
    })
    .catch((e) => {
      msg.textContent = e.message;
    });
}

/* ---------------- Preferences (progress display, default alang/slang/vcodec) ---------------- */

function initPreferences() {
  loadPreferences();
  document.getElementById("setting-progress-display").addEventListener("change", (e) => {
    fetch("/api/preferences", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ progress_display: e.target.checked }),
    });
  });
  document.getElementById("btn-save-preferences").addEventListener("click", saveDefaultPreferences);
  const retryBtn = document.getElementById("btn-save-retry");
  if (retryBtn) retryBtn.addEventListener("click", saveRetrySettings);
  initInstantSettings();
}

// Settings saved as soon as they're changed (no Save button).
const INSTANT_SETTINGS = {
  "setting-update-prereleases": "update_prereleases",
  "setting-pin-hours": "pin_check_hours",
  "setting-fetch-air": "fetch_air_dates",
  "setting-console-view": "console_view",
  "setting-experimental-check": "experimental_check",
  "setting-fetch-posters": "fetch_posters",
};

function initInstantSettings() {
  Object.entries(INSTANT_SETTINGS).forEach(([id, key]) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener("change", () => {
      const value = el.type === "checkbox" ? el.checked : el.value;
      saveUiSetting(key, value).then(() => {
        const msg = document.getElementById("experimental-message");
        if (msg && id !== "setting-console-view") msg.textContent = "Saved.";
      });
    });
  });
}

function saveUiSetting(key, value) {
  return fetch("/api/preferences", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ retry: { [key]: value } }),
  }).then((r) => r.json());
}

function fillRetrySettings(prefs) {
  if (!prefs.retry) return;
  Object.entries(INSTANT_SETTINGS).forEach(([id, key]) => {
    const el = document.getElementById(id);
    if (!el || prefs.retry[key] === undefined) return;
    if (el.type === "checkbox") el.checked = prefs.retry[key];
    else el.value = prefs.retry[key];
  });
  if (!document.getElementById("setting-auto-retry")) return;
  document.getElementById("setting-auto-retry").checked = prefs.retry.auto_retry;
  document.getElementById("setting-auto-retry-max").value = prefs.retry.auto_retry_max;
  document.getElementById("setting-cleanup-temp").checked = prefs.retry.cleanup_temp;
}

function saveRetrySettings() {
  const msg = document.getElementById("retry-message");
  const payload = {
    retry: {
      auto_retry: document.getElementById("setting-auto-retry").checked,
      auto_retry_max: document.getElementById("setting-auto-retry-max").value,
      cleanup_temp: document.getElementById("setting-cleanup-temp").checked,
    },
  };
  fetch("/api/preferences", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
    .then((r) => r.json())
    .then((prefs) => {
      fillRetrySettings(prefs);
      msg.textContent = prefs.retry ? "Saved." : "Restart the web UI to use this setting.";
    })
    .catch((e) => {
      msg.textContent = e.message;
    });
}

function loadPreferences() {
  fetch("/api/preferences")
    .then((r) => r.json())
    .then((prefs) => {
      document.getElementById("setting-progress-display").checked = prefs.progress_display;
      document.getElementById("setting-alang").value = prefs.defaults.alang || "";
      document.getElementById("setting-slang").value = prefs.defaults.slang || "";
      document.getElementById("setting-vcodec").value = prefs.defaults.vcodec || "";
      fillRetrySettings(prefs);
    });
}

function saveDefaultPreferences() {
  const msg = document.getElementById("preferences-message");
  const payload = {
    defaults: {
      alang: document.getElementById("setting-alang").value,
      slang: document.getElementById("setting-slang").value,
      vcodec: document.getElementById("setting-vcodec").value,
    },
  };
  fetch("/api/preferences", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
    .then((r) => r.json())
    .then((prefs) => {
      document.getElementById("setting-alang").value = prefs.defaults.alang || "";
      document.getElementById("setting-slang").value = prefs.defaults.slang || "";
      document.getElementById("setting-vcodec").value = prefs.defaults.vcodec || "";
      msg.textContent = "Saved. The Download page will prefill these (reload it if it's already open).";
    })
    .catch((e) => {
      msg.textContent = e.message;
    });
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// Native <select> option lists don't wrap or ellipsis long text (and OS-native
// dropdown rendering ignores CSS text-overflow entirely on some platforms), so a
// full download URL/title as an option label gets visually cut off with no way to
// read the rest. Truncating the text itself, keeping both ends, is the only fix
// that works regardless of browser/OS dropdown rendering.
function truncateMiddle(str, maxLen) {
  str = str || "";
  if (str.length <= maxLen) return str;
  const keep = maxLen - 1;
  const front = Math.ceil(keep * 0.6);
  const back = keep - front;
  return str.slice(0, front) + "…" + str.slice(str.length - back);
}

/* ---------------- Download page ---------------- */

let previewTimer = null;

function initDownloadPage() {
  const form = document.getElementById("download-form");
  document.getElementById("service").addEventListener("change", onServiceChange);
  document.getElementById("opt_vcodec").addEventListener("change", updateRangeGating);
  form.addEventListener("input", schedulePreview);
  form.addEventListener("change", schedulePreview);
  document.getElementById("btn-run").addEventListener("click", () => submitJob("run"));
  document.getElementById("btn-queue").addEventListener("click", () => submitJob("queue"));
  document.getElementById("btn-pin").addEventListener("click", pinFromForm);
  CHECK_FIELDS.forEach((id) => {
    const el = document.getElementById(id);
    if (el) {
      el.addEventListener("input", scheduleRecheck);
      el.addEventListener("change", scheduleRecheck);
    }
  });
  updateRangeGating();
  schedulePreview();
  initAvailability();
  document.getElementById("btn-reset-form").addEventListener("click", () => {
    clearDownloadState();
    window.location.reload();
  });
  form.addEventListener("input", saveDownloadStateSoon);
  form.addEventListener("change", saveDownloadStateSoon);
  window.addEventListener("pagehide", saveDownloadState);
  restoreDownloadState();
}

/* ---------------- Download page state (kept while switching tabs) ---------------- */

// Everything on the Download page - fields, service options, the availability check's
// result and picks, a check still running - is kept in this browser tab's
// sessionStorage, so going to Queue/Settings and back doesn't wipe it. It's tied to
// the web UI's current run (data-boot changes on every start), so a restarted web UI
// starts blank; "Reset form" clears it on purpose.
const DOWNLOAD_STATE_KEY = "vt-download-state";
let saveStateTimer = null;

function storageGet(key) {
  try { return JSON.parse(sessionStorage.getItem(key) || "null"); } catch (e) { return null; }
}

function storageSet(key, value) {
  try { sessionStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* private mode / full */ }
}

function clearDownloadState() {
  try { sessionStorage.removeItem(DOWNLOAD_STATE_KEY); } catch (e) { /* ignore */ }
  window.removeEventListener("pagehide", saveDownloadState);
}

function fieldKey(el) {
  if (el.dataset.combo) return `c:${el.dataset.combo}`;
  if (el.name) return `n:${el.name}`;
  return el.id ? `i:${el.id}` : null;
}

function downloadFields() {
  // The form plus the check's own controls (episode, quality, combinations, mode).
  return [...document.querySelectorAll("#download-form input, #download-form select")]
    .filter((el) => el.type !== "radio" && el.type !== "file" && !el.closest("#probe-result") && fieldKey(el));
}

function saveDownloadStateSoon() {
  clearTimeout(saveStateTimer);
  saveStateTimer = setTimeout(saveDownloadState, 300);
}

function saveDownloadState() {
  const fields = {};
  downloadFields().forEach((el) => {
    fields[fieldKey(el)] = el.type === "checkbox" ? el.checked : el.value;
  });
  storageSet(DOWNLOAD_STATE_KEY, {
    boot: document.body.dataset.boot,
    fields,
    probe: typeof probeResult === "undefined" ? null : {
      probeId, probeResult, probeResultId, probeSel, probeTitle, probeEpisode,
      keysState, keysProbeId, preferredLangs,
      steps: document.getElementById("probe-steps") ? document.getElementById("probe-steps").innerHTML : "",
    },
  });
}

function applyFields(fields, onlySvc) {
  downloadFields().forEach((el) => {
    const key = fieldKey(el);
    if (!(key in fields) || key === "n:service") return;
    if (onlySvc !== key.startsWith("n:svc__")) return;
    if (el.type === "checkbox") el.checked = !!fields[key];
    else el.value = fields[key];
  });
}

function restoreDownloadState() {
  const state = storageGet(DOWNLOAD_STATE_KEY);
  if (!state || state.boot !== document.body.dataset.boot) {
    clearDownloadState();
    window.addEventListener("pagehide", saveDownloadState);
    return;
  }
  const fields = state.fields || {};
  const serviceEl = document.getElementById("service");
  serviceEl.value = fields["n:service"] || "";
  applyFields(fields, false);
  if (typeof updateProbeModes === "function" && document.getElementById("availability")) updateProbeModes();
  // Service options only exist once the service's info has loaded.
  onServiceChange().then(() => {
    applyFields(fields, true);
    updateRangeGating();
    const vEl = document.getElementById("opt_range_");
    if (vEl && "n:range_" in fields) vEl.value = fields["n:range_"];
    restoreProbeState(state.probe);
    schedulePreview();
  });
}

function restoreProbeState(saved) {
  if (!saved || !document.getElementById("availability")) return;
  ({ probeId, probeResult, probeResultId, probeSel, probeTitle, probeEpisode, keysState, keysProbeId } = saved);
  if (saved.preferredLangs) preferredLangs = saved.preferredLangs;
  document.getElementById("probe-steps").innerHTML = saved.steps || "";
  if (probeResult && probeSel) renderProbeResult();
  // A check (or key check) that was still running: pick its progress back up.
  if (probeId && probeId !== probeResultId) {
    clearInterval(probeTimer);
    probeTimer = setInterval(pollProbe, 1500);
    pollProbe();
  }
  if (keysState && keysState.status === "running" && keysProbeId) {
    clearInterval(keysTimer);
    keysTimer = setInterval(pollKeys, 1500);
  }
}

function collectFormData() {
  const form = document.getElementById("download-form");
  const data = {};
  form.querySelectorAll("input, select").forEach((el) => {
    if (!el.name) return;
    if (el.type === "checkbox") {
      data[el.name] = el.checked;
    } else if (el.value !== "") {
      data[el.name] = el.value;
    }
  });
  const meta = typeof probeMeta === "function" ? probeMeta() : null;
  if (meta) data.meta = meta;
  return data;
}

function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(updatePreview, 300);
}

function updatePreview() {
  const data = collectFormData();
  if (!data.service) {
    document.getElementById("command-preview").textContent = "python -m vinetrimmer.vinetrimmer dl … (select a service)";
    return;
  }
  fetch("/api/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  })
    .then((r) => r.json())
    .then((d) => {
      document.getElementById("command-preview").textContent = d.command || d.error || "";
    });
}

function updateRangeGating() {
  const vcodecEl = document.getElementById("opt_vcodec");
  const rangeEl = document.getElementById("opt_range_");
  if (!vcodecEl || !rangeEl) return;
  const vcodec = vcodecEl.value || "h264";
  const service = document.getElementById("service").value;
  const hdrCapable = ["h265", "av1", "vp9"].includes(vcodec);

  [...rangeEl.options].forEach((o) => {
    if (o.value === "" || o.value === "sdr") {
      o.disabled = false;
      return;
    }
    if (o.value === "hybridplus") {
      o.disabled = !(service === "Netflix" && vcodec === "av1");
      return;
    }
    o.disabled = !hdrCapable;
  });

  const selected = rangeEl.options[rangeEl.selectedIndex];
  if (selected && selected.disabled) rangeEl.value = "sdr";
}

function syncServiceOptionsWrap() {
  const wrap = document.getElementById("service-options-wrap");
  if (wrap) wrap.hidden = document.getElementById("service-options").hidden;
}

function onServiceChange() {
  const service = document.getElementById("service").value;
  const svcFieldset = document.getElementById("service-options");
  const svcBody = document.getElementById("service-options-body");
  const authFieldset = document.getElementById("auth-status");
  const authBody = document.getElementById("auth-status-body");

  svcBody.innerHTML = "";
  authBody.innerHTML = "";

  if (!service) {
    svcFieldset.hidden = true;
    syncServiceOptionsWrap();
    authFieldset.hidden = true;
    document.getElementById("title-hint").textContent = "";
    updateRangeGating();
    schedulePreview();
    return Promise.resolve();
  }

  return fetch(serviceInfoUrl(service))
    .then((r) => r.json())
    .then((info) => {
      document.getElementById("title-hint").textContent = info.title_hint || "";

      (info.options || []).forEach((opt) => svcBody.appendChild(renderServiceOption(opt)));
      svcFieldset.hidden = (info.options || []).length === 0;
      syncServiceOptionsWrap();

      lastCheck = info.check;
      authBody.innerHTML = renderServiceCheck(info.check);
      authFieldset.hidden = false;

      updateRangeGating();
      schedulePreview();
    });
}

// The requirement check depends on these Download-form fields, since they change
// which cookie file/credential (-p), CDM (--cdm) and proxy the CLI will use.
const CHECK_FIELDS = ["opt_profile", "opt_cdm", "opt_proxy", "opt_no_proxy"];
let lastCheck = null;
let recheckTimer = null;

function serviceInfoUrl(service) {
  const params = new URLSearchParams();
  const el = (id) => document.getElementById(id);
  if (el("opt_profile") && el("opt_profile").value) params.set("profile", el("opt_profile").value);
  if (el("opt_cdm") && el("opt_cdm").value) params.set("cdm", el("opt_cdm").value);
  if (el("opt_proxy") && el("opt_proxy").value) params.set("proxy", el("opt_proxy").value);
  if (el("opt_no_proxy") && el("opt_no_proxy").checked) params.set("no_proxy", "1");
  const qs = params.toString();
  return `/api/services/${encodeURIComponent(service)}/info${qs ? "?" + qs : ""}`;
}

function scheduleRecheck() {
  clearTimeout(recheckTimer);
  recheckTimer = setTimeout(() => {
    const service = document.getElementById("service").value;
    if (!service) return;
    fetch(serviceInfoUrl(service))
      .then((r) => r.json())
      .then((info) => {
        lastCheck = info.check;
        document.getElementById("auth-status-body").innerHTML = renderServiceCheck(info.check);
      });
  }, 500);
}

const CHECK_ICONS = { ok: "&#10003;", info: "i", warn: "!", error: "&#10007;" };

function renderServiceCheck(check) {
  if (!check) return "";
  const parts = [];
  const auth = check.authorization
    ? `<strong>Authorization:</strong> ${escapeHtml(check.authorization)}`
    : "<strong>Authorization:</strong> not stated by the service script";
  const profile = check.profile ? ` &nbsp;&middot;&nbsp; profile <code>${escapeHtml(check.profile)}</code>` : "";
  parts.push(`<p class="check-auth">${auth}${profile}</p>`);

  const verdict = {
    ok: `<p class="ok">Everything this service needs is in place.</p>`,
    warn: `<p class="warn">Should work, but check the warnings below.</p>`,
    error: `<p class="err">This will most likely fail. Fix the items marked &#10007; first.</p>`,
  }[check.level] || "";
  parts.push(verdict);

  parts.push(`<ul class="check-list">${check.items.map((i) => `
    <li class="check-${i.status}">
      <span class="check-icon">${CHECK_ICONS[i.status] || ""}</span>
      <span><strong>${escapeHtml(i.label)}:</strong> ${escapeHtml(i.detail)}</span>
    </li>`).join("")}</ul>`);

  // Notes are written in webui/service_checks.py (trusted, may contain markup).
  if (check.notes && check.notes.length) {
    parts.push(`<ul class="check-notes">${check.notes.map((n) => `<li>${n}</li>`).join("")}</ul>`);
  }
  return parts.join("");
}

function renderServiceOption(opt) {
  const wrap = document.createElement("label");
  wrap.className = "field" + (opt.kind === "flag" ? " field-flag" : "");
  const fieldName = `svc__${opt.name}`;
  const label = `${escapeHtml(opt.opts.join("/"))}: ${escapeHtml(opt.help)}`;

  if (opt.kind === "flag") {
    wrap.innerHTML = `<input type="checkbox" name="${fieldName}"><span>${label}</span>`;
  } else if (opt.kind === "choice") {
    const defaultLabel = opt.default ? `(default: ${escapeHtml(opt.default)})` : "(default)";
    const options = [`<option value="">${defaultLabel}</option>`]
      .concat(
        (opt.choices || [])
          .filter((c) => c !== opt.default)
          .map((c) => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`)
      )
      .join("");
    wrap.innerHTML = `<span>${label}</span><select name="${fieldName}">${options}</select>`;
  } else {
    wrap.innerHTML = `<span>${label}</span><input type="text" name="${fieldName}">`;
  }
  return wrap;
}

function submitJob(mode) {
  const data = collectFormData();
  data.mode = mode;
  const msg = document.getElementById("form-message");
  msg.classList.remove("warn");
  if (!data.service) {
    msg.textContent = "Select a service first.";
    return;
  }
  if (lastCheck && lastCheck.level === "error" &&
      !confirm("The Requirements check found problems that will most likely make this job fail. Start it anyway?")) {
    return;
  }
  fetch("/api/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  })
    .then(async (r) => {
      const body = await r.json();
      if (!r.ok) throw new Error(body.error || "Failed to create job");
      return body;
    })
    .then((job) => {
      if (mode === "run") {
        window.location.href = `/console?job=${job.id}`;
      } else {
        msg.textContent = `Added to queue (job ${job.id}). Go to the Queue tab to start it.`;
      }
    })
    .catch((e) => {
      msg.textContent = e.message;
    });
}

/* ---------------- Availability check (experimental) ---------------- */

// What the check found, and what's picked from it. The picks are written straight
// into the normal form fields (-v, -r, -q, -al, -sl, --audio-pick, -drm), so the
// command preview always shows exactly what will run.
let probeId = null;
let probeTimer = null;
let probeResult = null;
let probeTitle = null; // the Title field value the result belongs to
let probeSel = null;
let probeEpisode = ""; // the episode the shown result was checked on ("" = first)
let probeResultId = null; // the check the shown result came from (extensions add to it)
let keysState = null; // Netflix key check: {status, message, label}
let keysProbeId = null;
let keysTimer = null;

const RANGE_ORDER = ["SDR", "HDR10", "HDR10+", "HLG", "DV", "DV+HDR"];
const RANGE_ARG = { "SDR": "sdr", "HDR10": "hdr10", "HDR10+": "hdr10", "HLG": "hlg", "DV": "dv", "DV+HDR": "dvhdr" };
const CODEC_ARG = { "H.264": "h264", "H.265": "h265", "AV1": "av1", "VP9": "vp9" };

// The -al / -sl the page opened with (the Settings defaults): languages from these that
// a checked episode doesn't offer at all stay in the command - another season may have
// them (a new episode often doesn't have every dub yet), and vinetrimmer just skips
// languages a title doesn't have.
let preferredLangs = { alang: "", slang: "" };

function primaryLang(tag) {
  try {
    return new Intl.Locale(tag).language;  // "spa" / "es-ES" / "es-419" -> "es"
  } catch (e) {
    return String(tag).toLowerCase();
  }
}

function keepUnoffered(preferred, offered) {
  const have = new Set(offered.map(primaryLang));
  return (preferred || "").split(",").map((x) => x.trim())
    .filter((x) => x && x !== "all" && x !== "orig" && !have.has(primaryLang(x)));
}

function initAvailability() {
  if (!document.getElementById("availability")) return;
  preferredLangs = {
    alang: (document.getElementById("opt_alang") || {}).value || "",
    slang: (document.getElementById("opt_slang") || {}).value || "",
  };
  document.getElementById("btn-check").addEventListener("click", startProbe);
  document.getElementById("btn-check-cancel").addEventListener("click", () => {
    if (probeId) fetch(`/api/probe/${probeId}/cancel`, { method: "POST" });
  });
  document.getElementById("service").addEventListener("change", updateProbeModes);
  document.getElementById("btn-combos-all").addEventListener("click", () => {
    // Everything: every manifest, at 4K - a 4K manifest also lists 1080p.
    document.querySelectorAll("#netflix-check [data-combo]").forEach((cb) => (cb.checked = true));
    document.getElementById("check-quality").value = "2160";
  });
  document.getElementById("btn-combos-none").addEventListener("click", () => {
    document.querySelectorAll("#netflix-check [data-combo]").forEach((cb) => (cb.checked = false));
  });
  updateProbeModes();
}

// Services where H.264 tops out lower than H.265 - worth checking both.
const PROBE_MODE_WARNINGS = {
  HBOMax: "HBO Max often has 1080p only on H.265, while H.264 can stop at 720p. Check H.265 or Both before you choose H.264.",
  Max: "HBO Max often has 1080p only on H.265, while H.264 can stop at 720p. Check H.265 or Both before you choose H.264.",
};

// Netflix gets its own controls (combinations + max quality); everything else the
// H.265 / H.264 / both choice.
function updateProbeModes() {
  const service = document.getElementById("service").value;
  document.getElementById("netflix-check").hidden = service !== "Netflix";
  document.getElementById("generic-check").hidden = service === "Netflix";
  const warn = document.getElementById("probe-mode-warning");
  warn.textContent = PROBE_MODE_WARNINGS[service] || "";
  warn.hidden = !PROBE_MODE_WARNINGS[service];
}

// extra: {extend, combos} to check more variants into the shown result, or
// {probe_mode: "keys"} for the key check (see startKeysCheck).
function startProbe(extra) {
  extra = extra && !(extra instanceof Event) ? extra : {};
  const msg = document.getElementById("probe-message");
  const data = collectFormData();
  delete data.meta;
  if (data.service === "Netflix") {
    data.probe_mode = "combos";
    data.combos = [...document.querySelectorAll("#netflix-check [data-combo]:checked")].map((cb) => cb.dataset.combo);
    data.check_quality = document.getElementById("check-quality").value;
  } else {
    data.probe_mode = document.getElementById("probe-mode").value;
  }
  // Checking a single variant afterwards stays on the episode the result is for.
  data.check_episode = extra.extend ? probeEpisode
    : (document.getElementById("check-episode").value || "").trim().toUpperCase();
  Object.assign(data, extra);
  msg.textContent = "";
  if (!data.service || !data.title) {
    msg.textContent = "Select a service and enter a link / ID first.";
    return;
  }
  if (data.service === "Netflix" && !data.combos.length) {
    msg.textContent = "Tick at least one codec / range combination to check.";
    return;
  }
  fetch("/api/jobs").then((r) => r.json()).then((jobs) => {
    if (jobs.some((j) => j.status === "running") &&
        !confirm("A download is running. Checking now means a second login/manifest request at the same time, " +
                 "which puts the account at higher risk.\n\nCheck anyway?")) return;
    fetch("/api/probe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    })
      .then(async (r) => {
        const body = await r.json();
        if (!r.ok) throw new Error(body.error || "Could not start the check");
        return body;
      })
      .then((p) => {
        probeId = p.id;
        if (!extra.extend) {
          probeTitle = data.title;
          probeEpisode = data.check_episode || "";
          probeResult = null;
          probeResultId = null;
          probeSel = null;
          keysState = null;
          document.getElementById("probe-result").innerHTML = "";
        }
        renderProbe(p);
        clearInterval(probeTimer);
        probeTimer = setInterval(pollProbe, 1500);
      })
      .catch((e) => { msg.textContent = e.message; });
  });
}

function pollProbe() {
  if (!probeId) return;
  fetch(`/api/probe/${probeId}`).then((r) => r.json()).then((p) => {
    renderProbe(p);
    if (p.status !== "running") clearInterval(probeTimer);
  });
}

const STEP_ICONS = { pending: "&middot;", running: "&#8987;", ok: "&#10003;", unavailable: "&#10007;", error: "!", cancelled: "&times;" };

function renderProbe(p) {
  const running = p.status === "running";
  document.getElementById("btn-check").disabled = running;
  document.getElementById("btn-check-cancel").hidden = !running;
  const warn = document.getElementById("probe-warning");
  warn.hidden = !(running && p.queue_busy);
  warn.textContent = "A download is running at the same time as this check. That's riskier for the account.";
  document.getElementById("probe-steps").innerHTML = p.steps.map((s) => `
    <span class="probe-step probe-${s.status}" title="${escapeHtml(s.message || s.command || "")}">
      <span class="probe-icon">${STEP_ICONS[s.status] || ""}</span>${escapeHtml(s.label)}
      ${s.status === "unavailable" || s.status === "error" ? `<small>${escapeHtml(truncateMiddle(s.message || "", 70))}</small>` : ""}
    </span>`).join("") + (running ? `<span class="probe-checking">checking&hellip;</span>` : "");
  // Why a run came back empty, in full - the chips only fit a short snippet.
  const failed = p.steps.filter((s) => (s.status === "unavailable" || s.status === "error") && s.message);
  document.getElementById("probe-steps").insertAdjacentHTML("beforeend", failed.length && !running
    ? `<ul class="probe-reasons">${failed.map((s) => `<li><strong>${escapeHtml(s.label)}:</strong> ${escapeHtml(s.message)}</li>`).join("")}</ul>`
    : "");
  const msg = document.getElementById("probe-message");
  if (p.status === "failed") msg.textContent = p.error || "The check failed.";
  else if (p.status === "cancelled") msg.textContent = "Check cancelled.";
  else msg.textContent = "";
  if (p.status === "done" && p.result && p.id !== probeResultId) {
    // An extension (single variant checked afterwards) keeps what's already picked.
    const keep = probeSel && probeResult;
    probeResult = p.result;
    probeResultId = p.id;
    if (!keep) probeSel = initialSelection(p.result);
    rerenderProbe();
  }
}

// Netflix 4K: are the keys for what's picked already in the key vaults? Runs
// --keys --cache - vault lookups only, never a license request.
function startKeysCheck() {
  const data = collectFormData();
  delete data.meta;
  data.probe_mode = "keys";
  const label = [data.vcodec, data.range_, data.quality ? data.quality + "p" : "best"].filter(Boolean).join(" ");
  keysState = { status: "running", message: "Looking in the key vaults…", label };
  rerenderProbe();
  fetch("/api/probe", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  })
    .then(async (r) => {
      const body = await r.json();
      if (!r.ok) throw new Error(body.error || "Could not start the key check");
      return body;
    })
    .then((p) => {
      keysProbeId = p.id;
      clearInterval(keysTimer);
      keysTimer = setInterval(pollKeys, 1500);
    })
    .catch((e) => {
      keysState = { status: "error", message: e.message, label };
      rerenderProbe();
    });
}

function pollKeys() {
  fetch(`/api/probe/${keysProbeId}`).then((r) => r.json()).then((p) => {
    if (p.status === "running") return;
    clearInterval(keysTimer);
    const label = keysState ? keysState.label : "";
    const keys = p.result && p.result.keys;
    if (p.status === "done" && keys && keys.complete) {
      keysState = { status: "ok", message: "All keys are in your vaults, so this downloads in 4K.", label };
    } else if (p.status === "done") {
      keysState = { status: "missing", label,
        message: (p.result && p.result.message) || "Not every key is in your vaults." };
    } else {
      keysState = { status: "error", label, message: p.error || "The key check failed." };
    }
    rerenderProbe();
  });
}

function langLabel(track) {
  return track.language + (track.name ? ` · ${track.name.replace(/\s*\[?Original\]?\s*/, "")}` : "");
}

function initialSelection(res) {
  const sel = { vcodec: null, range: null, quality: null, audio: {}, subs: {}, drm: null };
  const defaults = res.default_selected || { audio: [], subtitles: [], video: [] };
  // Audio: one slot per language; vinetrimmer's own default choice (made with the
  // -al from the form) decides which are on, and "best" = leave it to vinetrimmer.
  res.audio.filter((a) => !a.descriptive).forEach((a) => {
    if (!sel.audio[a.language]) sel.audio[a.language] = { on: false, pick: null };
    if (defaults.audio.includes(a.id)) sel.audio[a.language].on = true;
  });
  res.subtitles.forEach((s) => {
    if (!sel.subs[s.language]) sel.subs[s.language] = { on: false };
    if (defaults.subtitles.includes(s.id)) sel.subs[s.language].on = true;
  });
  // Video: preselect what the form already asks for, if it exists.
  const vEl = document.getElementById("opt_vcodec");
  const rEl = document.getElementById("opt_range_");
  sel.vcodec = vEl && vEl.value ? vEl.value : null;
  sel.range = rEl && rEl.value ? rEl.value : null;
  if (res.quality) sel.drm = res.quality === "1080" ? "widevine" : "";
  return sel;
}

function videoHeights(codec, ranges) {
  const hs = new Set(probeResult.video.filter((v) => v.codec === codec && ranges.includes(v.range)).map((v) => v.height));
  return [...hs].sort((a, b) => b - a);
}

function formatKbps(kb) {
  return kb >= 1000 ? `${(kb / 1000).toFixed(1)} Mb/s` : `${kb} kb/s`;
}

function formatSize(bytes) {
  return bytes ? formatBytes(bytes) : "";
}

// Netflix: which ranges each codec can have, and how to show one that has no tracks
// in the result: inferred SDR, checked-but-not-available, or not checked yet.
const NETFLIX_VALID = { "H.264": ["SDR"], "H.265": ["SDR", "HDR10", "DV"], "AV1": ["SDR", "HDR10+"], "VP9": ["SDR", "HDR10"] };

function netflixCell(res, codec, range) {
  if (!NETFLIX_VALID[codec] || !NETFLIX_VALID[codec].includes(range)) return `<td class="avail-na">&middot;</td>`;
  const vc = CODEC_ARG[codec];
  const rg = RANGE_ARG[range];
  const combo = `${vc}:${rg}`;
  const checkLink = `<span class="link-button" role="button" data-check-combo="${combo}">check</span>`;
  if ((res.assumed || []).some((a) => a.codec === codec && a.range === range)) {
    const active = probeSel.vcodec === vc && probeSel.range === rg ? " active" : "";
    return `<td><button type="button" class="avail-cell assumed${active}" data-vcodec="${vc}" data-range="${rg}"
      title="HDR or DV was found, so SDR exists too. Its resolution and bitrate weren't checked.">
      available<small>not checked &middot; ${checkLink}</small></button></td>`;
  }
  const run = (res.checked || []).find((c) => c.vcodec === vc && c.range === rg);
  if (run && (run.status === "unavailable" || run.status === "error")) {
    return `<td class="avail-no">&#10007; not available</td>`;
  }
  return `<td class="avail-unchecked">not checked<br>${checkLink}</td>`;
}

function renderProbeResult() {
  const res = probeResult;
  const t = res.title;
  const service = document.getElementById("service").value;
  const isShow = t.type === "TV" || t.season;
  const seasonNums = Object.keys(res.seasons || {}).sort((a, b) => a - b);
  const pad = (n) => String(n).padStart(2, "0");
  const seasons = seasonNums.length
    ? `${seasonNums.length} season${seasonNums.length === 1 ? "" : "s"} · ${res.titles_total} episodes`
    : "";
  const perSeason = seasonNums.map((n) => `<span class="season-count">S${pad(n)}: ${res.seasons[n]}</span>`).join("");
  const episode = isShow && t.season ? `S${pad(t.season)}E${pad(t.episode)}` : "";
  const recheck = isShow ? `
        <div class="recheck">
          <span class="hint">Check another episode (dubs/subs can differ per season, new episodes may not have them all yet):</span>
          <input type="text" id="recheck-episode" placeholder="e.g. S02E01" value="${escapeHtml(probeEpisode)}" autocomplete="off">
          <button type="button" class="chip" data-recheck="1">Check</button>
        </div>` : "";

  const head = `
    <div class="avail-head">
      ${res.poster ? `<img class="avail-poster" src="${escapeHtml(res.poster)}" alt="">` : ""}
      <div>
        <div class="avail-title">${escapeHtml(t.name || "")}${t.year ? ` <span class="job-year">(${t.year})</span>` : ""}${
          episode ? ` <span class="avail-episode">&middot; ${episode}${t.episode_name ? `: ${escapeHtml(t.episode_name)}` : ""}</span>` : ""}</div>
        <div class="hint">${escapeHtml([isShow ? "Show" : "Movie", seasons].filter(Boolean).join(" · "))}</div>
        ${perSeason ? `<div class="season-counts">${perSeason}</div>` : ""}
        ${recheck}
        ${forcedOptionFlags().length ? `<div class="hint">Checked with ${forcedOptionFlags().map((f) => `<code>${escapeHtml(f)}</code>`).join(" ")}. These are ticked below for the download too.</div>` : ""}
      </div>
    </div>`;

  // ---- video matrix ----
  const netflix = service === "Netflix" && res.checked;
  const codecOrder = (a, b) => Object.keys(CODEC_ARG).indexOf(a) - Object.keys(CODEC_ARG).indexOf(b);
  const codecs = netflix ? Object.keys(NETFLIX_VALID)
    : Object.keys(res.matrix).sort(codecOrder);
  const ranges = RANGE_ORDER.filter((r) => codecs.some((c) => (res.matrix[c] || {})[r]) ||
    (netflix && codecs.some((c) => NETFLIX_VALID[c].includes(r))));
  const cell = (codec, range) => {
    const c = (res.matrix[codec] || {})[range];
    if (!c) return netflix ? netflixCell(res, codec, range) : `<td class="avail-na">&middot;</td>`;
    const vc = CODEC_ARG[codec] || codec.toLowerCase();
    const rg = RANGE_ARG[range];
    const active = probeSel.vcodec === vc && probeSel.range === rg ? " active" : "";
    return `<td><button type="button" class="avail-cell${active}" data-vcodec="${vc}" data-range="${rg}">
      ${c.max_height}p<small>${formatKbps(c.max_bitrate)}</small></button></td>`;
  };
  const combos = [];
  codecs.forEach((codec) => {
    const m = res.matrix[codec] || {};
    const vc = CODEC_ARG[codec] || codec.toLowerCase();
    if (m["HDR10"] && (m["DV"] || m["DV+HDR"])) combos.push({ vc, rg: "hybrid", label: `${codec} Hybrid (HDR10 + DV)` });
  });
  if (service === "Netflix" && res.matrix["AV1"] && res.matrix["AV1"]["HDR10+"] &&
      res.matrix["H.265"] && res.matrix["H.265"]["DV"]) {
    combos.push({ vc: "av1", rg: "hybridplus", label: "AV1 Hybrid+ (HDR10+ with DV from H.265)" });
  }
  const comboHtml = combos.length ? `<div class="avail-chips"><span class="hint">Combined:</span>${combos.map((c) =>
    `<button type="button" class="chip${probeSel.vcodec === c.vc && probeSel.range === c.rg ? " active" : ""}" data-vcodec="${c.vc}" data-range="${c.rg}">${escapeHtml(c.label)}</button>`).join("")}</div>` : "";

  let av1Question = "";
  if (service === "Netflix" && probeSel.vcodec === "av1" && ["hdr10", "hybridplus"].includes(probeSel.range) &&
      combos.some((c) => c.rg === "hybridplus")) {
    av1Question = `<div class="avail-question">AV1 HDR: plain <strong>HDR10+</strong>, or <strong>Hybrid+</strong> (adds the DV layer from the H.265 stream)?
      <button type="button" class="chip${probeSel.range === "hdr10" ? " active" : ""}" data-vcodec="av1" data-range="hdr10">HDR10+</button>
      <button type="button" class="chip${probeSel.range === "hybridplus" ? " active" : ""}" data-vcodec="av1" data-range="hybridplus">Hybrid+</button></div>`;
  }

  let resRow = "";
  if (probeSel.vcodec && probeSel.range) {
    const codec = Object.keys(CODEC_ARG).find((k) => CODEC_ARG[k] === probeSel.vcodec);
    const rangesFor = probeSel.range.startsWith("hybrid") ? ["HDR10", "HDR10+"]
      : RANGE_ORDER.filter((r) => RANGE_ARG[r] === probeSel.range);
    let heights = codec ? videoHeights(codec, rangesFor) : [];
    if (service === "Netflix" && probeSel.drm === "widevine") heights = heights.filter((h) => h <= 1080);
    if (heights.length) {
      resRow = `<div class="avail-chips"><span class="hint">Resolution:</span>
        <button type="button" class="chip${!probeSel.quality ? " active" : ""}" data-quality="">Best</button>
        ${heights.map((h) => `<button type="button" class="chip${String(probeSel.quality) === String(h) ? " active" : ""}" data-quality="${h}">${h}p</button>`).join("")}</div>`;
    }
  }

  const hasUHD = probeResult.video.some((v) => v.height >= 2160);
  let drm = "";
  if (service === "Netflix") {
    const drmEl = document.querySelector('[name="svc__drm_system"]');
    const current = probeSel.drm !== null ? probeSel.drm : drmEl ? drmEl.value : "";
    const ks = keysState;
    const keysLine = ks ? `<span class="keys-status keys-${ks.status}">${ks.status === "ok" ? "&#10003; " : ks.status === "running" ? "&#8987; " : "&#10007; "}${escapeHtml(ks.message)}${ks.label ? ` <small>(${escapeHtml(ks.label)})</small>` : ""}</span>` : "";
    drm = `<h4>DRM</h4>
      <div class="avail-chips">
        <button type="button" class="chip${current !== "widevine" ? " active" : ""}" data-drm="">PlayReady (default), up to 4K</button>
        <button type="button" class="chip${current === "widevine" ? " active" : ""}" data-drm="widevine">Widevine (-drm widevine), up to 1080p</button>
      </div>
      ${current === "widevine"
        ? `<p class="hint ok">Widevine gets its keys from the CDM, so this always works, up to 1080p.</p>`
        : `<p class="hint warn">4K is still a work in progress. The tool can't get 4K keys by itself, so a PlayReady download
            only works if the keys are already in your key vaults. For now, ask the owner of the program for the keys,
            or use Widevine for 1080p.</p>
          <div class="avail-chips">
            <button type="button" class="chip" data-check-keys="1"${ks && ks.status === "running" ? " disabled" : ""}>Check keys for this selection</button>
            ${keysLine}
          </div>
          <p class="hint">Runs <code>--keys --cache</code> for the codec / range / resolution picked above: it only looks in
            your key vaults and never sends a license request.</p>`}`;
  } else if (hasUHD) {
    drm = "";
  }

  const videoHtml = codecs.length ? `
    <h4>Video</h4>
    <table class="avail-matrix">
      <thead><tr><th>Codec</th>${ranges.map((r) => `<th>${r}</th>`).join("")}</tr></thead>
      <tbody>${codecs.map((c) => `<tr><th>${escapeHtml(c)}</th>${ranges.map((r) => cell(c, r)).join("")}</tr>`).join("")}</tbody>
    </table>
    <p class="hint">Highest resolution and bitrate per codec and range. Click one to use it.</p>
    ${comboHtml}${av1Question}${resRow}` : `<h4>Video</h4><p class="hint">No video tracks reported.</p>`;

  // ---- audio slots ----
  const audioByLang = {};
  res.audio.filter((a) => !a.descriptive).forEach((a) => (audioByLang[a.language] = audioByLang[a.language] || []).push(a));
  const audioLangs = Object.keys(audioByLang);
  const audioOn = audioLangs.filter((l) => probeSel.audio[l] && probeSel.audio[l].on).length;
  const audioHtml = `
    <h4>Audio <span class="hint">(${audioLangs.length} language${audioLangs.length === 1 ? "" : "s"}, ${audioOn} selected)</span></h4>
    <div class="slots">${audioLangs.map((lang) => {
      const st = probeSel.audio[lang];
      const variants = audioByLang[lang].slice().sort((a, b) => b.bitrate - a.bitrate);
      const picked = variants.find((v) => v.id === st.pick);
      const first = variants[0];
      const summary = picked ? `${picked.codec} ${picked.atmos ? "Atmos" : picked.channels || ""} · ${picked.bitrate} kb/s`
        : `Best (${first.codec} ${first.atmos ? "Atmos" : first.channels || ""})`;
      return `
      <details class="slot${st.on ? " on" : ""}" data-lang="${escapeHtml(lang)}">
        <summary>
          <input type="checkbox" class="slot-toggle" data-audio-lang="${escapeHtml(lang)}"${st.on ? " checked" : ""}>
          <span class="slot-lang">${escapeHtml(langLabel(first))}${first.original ? ' <span class="badge">Original</span>' : ""}</span>
          <span class="slot-pick">${escapeHtml(summary)}</span>
        </summary>
        <div class="slot-variants">
          <label><input type="radio" name="pick-${escapeHtml(lang)}" data-audio-pick="${escapeHtml(lang)}" value=""${!st.pick ? " checked" : ""}> Best (vinetrimmer's normal choice)</label>
          ${variants.map((v) => `<label><input type="radio" name="pick-${escapeHtml(lang)}" data-audio-pick="${escapeHtml(lang)}" value="${escapeHtml(v.id)}"${st.pick === v.id ? " checked" : ""}>
            ${escapeHtml(v.codec)} · ${escapeHtml(v.atmos ? "Atmos " + (v.channels || "") : v.channels || "?")} · ${v.bitrate} kb/s${v.size ? ` · ${formatSize(v.size)}` : ""}</label>`).join("")}
        </div>
      </details>`;
    }).join("")}</div>`;

  // ---- subtitles ----
  const subsByLang = {};
  res.subtitles.forEach((s) => (subsByLang[s.language] = subsByLang[s.language] || []).push(s));
  const subLangs = Object.keys(subsByLang);
  const subsOn = subLangs.filter((l) => probeSel.subs[l].on).length;
  const subsHtml = `
    <h4>Subtitles <span class="hint">(${subLangs.length} language${subLangs.length === 1 ? "" : "s"}, ${subsOn} selected)</span>
      <button type="button" class="link-button" data-subs-all="1">all</button>
      <button type="button" class="link-button" data-subs-all="0">none</button></h4>
    <div class="avail-chips">${subLangs.map((lang) => {
      const kinds = subsByLang[lang].map((s) => s.forced ? "Forced" : s.sdh ? "SDH" : s.cc ? "CC" : "Full");
      return `<button type="button" class="chip${probeSel.subs[lang].on ? " active" : ""}" data-sub-lang="${escapeHtml(lang)}"
        title="${escapeHtml(subsByLang[lang].map((s) => s.name || s.codec).join(", "))}">${escapeHtml(lang)} <small>${escapeHtml([...new Set(kinds)].join(", "))}</small></button>`;
    }).join("")}</div>`;

  const box = document.getElementById("probe-result");
  box.innerHTML = head + videoHtml + drm + audioHtml + subsHtml;
  bindProbeResult(box);
}

function bindProbeResult(box) {
  box.querySelectorAll("[data-vcodec]").forEach((b) => b.addEventListener("click", () => {
    probeSel.vcodec = b.dataset.vcodec;
    probeSel.range = b.dataset.range;
    probeSel.quality = null;
    rerenderProbe();
  }));
  box.querySelectorAll("[data-quality]").forEach((b) => b.addEventListener("click", () => {
    probeSel.quality = b.dataset.quality || null;
    rerenderProbe();
  }));
  box.querySelectorAll("[data-drm]").forEach((b) => b.addEventListener("click", () => {
    probeSel.drm = b.dataset.drm;
    rerenderProbe();
  }));
  box.querySelectorAll("[data-check-combo]").forEach((b) => b.addEventListener("click", (e) => {
    e.stopPropagation();
    startProbe({ extend: probeResultId, combos: [b.dataset.checkCombo] });
  }));
  box.querySelectorAll("[data-check-keys]").forEach((b) => b.addEventListener("click", startKeysCheck));
  const recheckInput = box.querySelector("#recheck-episode");
  const recheck = () => {
    document.getElementById("check-episode").value = recheckInput.value.trim().toUpperCase();
    startProbe();
  };
  box.querySelectorAll("[data-recheck]").forEach((b) => b.addEventListener("click", recheck));
  if (recheckInput) recheckInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      recheck();
    }
  });
  box.querySelectorAll("[data-audio-lang]").forEach((cb) => {
    cb.addEventListener("click", (e) => e.stopPropagation());
    cb.addEventListener("change", () => {
      probeSel.audio[cb.dataset.audioLang].on = cb.checked;
      rerenderProbe(true);
    });
  });
  box.querySelectorAll("[data-audio-pick]").forEach((r) => r.addEventListener("change", () => {
    const st = probeSel.audio[r.dataset.audioPick];
    st.pick = r.value || null;
    if (st.pick) st.on = true;
    rerenderProbe(true);
  }));
  box.querySelectorAll("[data-sub-lang]").forEach((b) => b.addEventListener("click", () => {
    const st = probeSel.subs[b.dataset.subLang];
    st.on = !st.on;
    rerenderProbe();
  }));
  box.querySelectorAll("[data-subs-all]").forEach((b) => b.addEventListener("click", () => {
    Object.values(probeSel.subs).forEach((st) => (st.on = b.dataset.subsAll === "1"));
    rerenderProbe();
  }));
}

// Re-render, keeping open audio slots open.
function rerenderProbe() {
  const open = [...document.querySelectorAll("#probe-result details.slot[open]")].map((d) => d.dataset.lang);
  renderProbeResult();
  open.forEach((lang) => {
    const d = document.querySelector(`#probe-result details.slot[data-lang="${CSS.escape(lang)}"]`);
    if (d) d.open = true;
  });
  applyProbeSelection();
}

// "--tier-unlimits", "--imax", ... for the forced service options, from the labels the
// service options are rendered with ("-tu/--tier-unlimits — ...").
function forcedOptionFlags() {
  return Object.keys((probeResult && probeResult.forced_options) || {}).map((name) => {
    const el = document.querySelector(`#download-form [name="${CSS.escape(name)}"]`);
    const label = el && el.closest("label") ? el.closest("label").textContent : "";
    const m = label.match(/--[\w-]+/);
    return m ? m[0] : name.replace(/^svc__/, "--").replace(/_/g, "-");
  });
}

function setField(id, value) {
  const el = document.getElementById(id);
  if (el) el.value = value ?? "";
}

function applyProbeSelection() {
  if (!probeResult || !probeSel) return;
  saveDownloadStateSoon();
  if (probeSel.vcodec) {
    setField("opt_vcodec", probeSel.vcodec);
    updateRangeGating();
    setField("opt_range_", probeSel.range || "");
  }
  setField("opt_quality", probeSel.quality || "");

  const langs = Object.keys(probeSel.audio).filter((l) => probeSel.audio[l].on);
  const keptAudio = keepUnoffered(preferredLangs.alang, Object.keys(probeSel.audio));
  if (langs.length || keptAudio.length) setField("opt_alang", [...langs, ...keptAudio].join(","));
  const picks = langs.filter((l) => probeSel.audio[l].pick).map((l) => {
    const v = probeResult.audio.find((a) => a.id === probeSel.audio[l].pick);
    if (!v) return null;
    // "16/JOC" would clash with the "/" separator - Atmos is picked by name instead.
    const ch = v.atmos ? "atmos" : v.channels || "";
    return `${l}=${v.codec}/${ch}/${v.bitrate}`;
  }).filter(Boolean);
  setField("opt_audio_pick", picks.join(","));

  const subLangs = Object.keys(probeSel.subs);
  const subsOn = subLangs.filter((l) => probeSel.subs[l].on);
  const noSubs = document.getElementById("opt_no_subs");
  // Only when there were subtitles to choose from and all were unticked.
  if (noSubs && subLangs.length) noSubs.checked = subsOn.length === 0;
  const keptSubs = keepUnoffered(preferredLangs.slang, subLangs);
  if (subsOn.length && subsOn.length === subLangs.length && !keptSubs.length) setField("opt_slang", "all");
  else if (subsOn.length || keptSubs.length) setField("opt_slang", [...subsOn, ...keptSubs].join(","));

  // Service options the check always uses (e.g. Disney+ --tier-unlimits --imax), so
  // the download gets the same manifest that was checked.
  Object.entries(probeResult.forced_options || {}).forEach(([name, value]) => {
    const el = document.querySelector(`#download-form [name="${CSS.escape(name)}"]`);
    if (el && el.type === "checkbox") el.checked = !!value;
  });

  if (probeSel.drm !== null) {
    const drmEl = document.querySelector('[name="svc__drm_system"]');
    if (drmEl) drmEl.value = probeSel.drm;
  }
  schedulePreview();
}

// The check's name/year/poster go along with the job (queue + console), as long as
// the Title field still holds what was checked.
function probeMeta() {
  if (!probeResult) return null;
  if ((document.getElementById("title").value || "").trim() !== (probeTitle || "").trim()) return null;
  return { name: probeResult.title.name, year: probeResult.title.year, poster: probeResult.poster || null };
}

function pinFromForm() {
  const data = collectFormData();
  const msg = document.getElementById("form-message");
  if (!data.service || !data.title) {
    msg.textContent = "Select a service and enter a link / ID first.";
    return;
  }
  fetch("/api/pins", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) })
    .then(async (r) => {
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.error || "Couldn't pin (restart the web UI if it was just updated).");
      msg.textContent = "Pinned. The first check is in the queue, and you find all pinned shows on the Queue tab.";
    })
    .catch((e) => { msg.textContent = e.message; });
}

/* ---------------- Queue page ---------------- */

function initQueuePage() {
  refreshJobs();
  setInterval(refreshJobs, 2000);
  refreshPins();
  setInterval(refreshPins, 5000);
  document.getElementById("pin-list").addEventListener("click", onPinAction);
  document.getElementById("pin-list").addEventListener("change", onPinAction);
  document.getElementById("btn-start-queue").addEventListener("click", () => {
    fetch("/api/jobs/start_queue", { method: "POST" }).then(refreshJobs);
  });
  const pauseBtn = document.getElementById("btn-pause-queue");
  if (pauseBtn) {
    pauseBtn.addEventListener("click", () => {
      fetch("/api/jobs/pause_queue", { method: "POST" })
        .then((r) => {
          if (!r.ok) throw new Error();
          return r.json();
        })
        .then(showQueueState)
        .catch(() => alert("Restart the web UI to use Pause (it was just added)."));
    });
  }
  document.getElementById("btn-clear-finished").addEventListener("click", () => {
    fetch("/api/jobs/clear_finished", { method: "POST" }).then(refreshJobs);
  });
}

const QUEUE_STATE_TEXT = {
  running: (n) => `Running${n ? `, ${n} more in the queue` : ""}.`,
  pausing: (n) => `Pausing: the current download finishes, then the queue stops${n ? ` (${n} waiting)` : ""}. Press <strong>Start Queue</strong> to keep going instead.`,
  paused: (n) => `Paused, ${n} waiting. Press <strong>Start Queue</strong> (or Download Now on the Download page) to continue.`,
  idle: () => "",
};

function showQueueState(s) {
  const el = document.getElementById("queue-state");
  const pauseBtn = document.getElementById("btn-pause-queue");
  if (!el || !s || !s.state) return;
  const text = (QUEUE_STATE_TEXT[s.state] || (() => ""))(s.pending);
  el.innerHTML = text;
  el.className = `queue-state queue-state-${s.state}`;
  el.hidden = !text;
  if (pauseBtn) pauseBtn.disabled = s.state !== "running";
}

function refreshQueueState() {
  fetch("/api/queue/state")
    .then((r) => (r.ok ? r.json() : null))
    .then(showQueueState)
    .catch(() => {});
}

function pinJob(jobId) {
  return fetch("/api/pins", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ job_id: jobId }) })
    .then(async (r) => {
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.error || "Couldn't pin (restart the web UI if it was just updated).");
      return body;
    })
    .catch((e) => alert(e.message));
}

function whenText(ts) {
  if (!ts) return "";
  const diff = ts * 1000 - Date.now();
  const d = new Date(ts * 1000);
  const day = d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
  const time = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  if (diff <= 0) return "due now";
  const h = Math.round(diff / 3600000);
  return `${day} ${time}${h < 48 ? ` (in ${h < 1 ? "<1" : h} h)` : ""}`;
}

const PIN_INTERVALS = [[6, "6 h"], [12, "12 h"], [24, "day"], [48, "2 days"], [168, "week"]];

function refreshPins() {
  fetch("/api/pins")
    .then((r) => (r.ok ? r.json() : []))
    .then((pins) => {
      const section = document.getElementById("pins");
      section.hidden = !pins.length;
      document.getElementById("pin-list").innerHTML = pins.map((p) => {
        const air = p.air && p.air.next
          ? `On TV: S${String(p.air.next.season).padStart(2, "0")}E${String(p.air.next.number).padStart(2, "0")}${p.air.next.name ? ` &ldquo;${escapeHtml(p.air.next.name)}&rdquo;` : ""} &middot; ${whenText(p.air.next.airstamp_ts)}`
          : p.air && p.air.status === "Ended" ? "TVmaze: the show has ended" : "";
        const busy = p.active_job ? `<span class="status status-${p.active_job.status}">${p.active_job.status}</span>` : "";
        const intervals = PIN_INTERVALS.map(([h, label]) =>
          `<option value="${h}"${h === p.interval_hours ? " selected" : ""}>${label}</option>`).join("");
        return `
          <div class="pin-card">
            ${p.poster ? `<img class="job-thumb" src="${escapeHtml(p.poster)}" alt="">` : `<div class="job-thumb job-thumb-empty"></div>`}
            <div class="pin-main">
              <div class="job-title-name">${escapeHtml(p.name || p.title)}${p.year ? ` <span class="job-year">(${p.year})</span>` : ""} ${busy}</div>
              <div class="hint">${escapeHtml(p.service)} &middot; next: <strong>${escapeHtml(p.next_episode || "from the start")}</strong>${p.last_downloaded ? ` &middot; last downloaded ${escapeHtml(p.last_downloaded)}` : ""}</div>
              <div class="hint">${escapeHtml(p.last_result || "")}${p.next_check_at && !p.active_job ? ` &middot; next check ${whenText(p.next_check_at)}` : ""}</div>
              ${air ? `<div class="hint pin-air">${air}</div>` : ""}
            </div>
            <div class="pin-actions">
              <button type="button" data-pin-check="${p.id}"${p.active_job ? " disabled" : ""}>Check now</button>
              <label class="hint">every <select data-pin-interval="${p.id}">${intervals}</select></label>
              <button type="button" data-pin-remove="${p.id}">Unpin</button>
            </div>
          </div>`;
      }).join("");
    })
    .catch(() => {});
}

function onPinAction(e) {
  const t = e.target;
  if (e.type === "click" && t.dataset.pinCheck) {
    t.disabled = true;
    fetch(`/api/pins/${t.dataset.pinCheck}/check`, { method: "POST" }).then(() => { refreshPins(); refreshJobs(); });
  } else if (e.type === "click" && t.dataset.pinRemove) {
    if (!confirm("Unpin this show? Already queued checks stay in the queue as normal jobs.")) return;
    fetch(`/api/pins/${t.dataset.pinRemove}`, { method: "DELETE" }).then(() => { refreshPins(); refreshJobs(); });
  } else if (e.type === "change" && t.dataset.pinInterval) {
    fetch(`/api/pins/${t.dataset.pinInterval}/interval`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ hours: Number(t.value) }),
    }).then(refreshPins);
  }
}

// The table follows the run order: what's running, then what's queued in the order
// it will run, then finished/failed/cancelled jobs newest first. Attempts that were
// retried are left out - their retry is listed in their place.
function orderJobsForTable(jobs) {
  const ids = new Set(jobs.map((j) => j.id));
  const visible = jobs.filter((j) => !(j.retried_by && ids.has(j.retried_by)));
  const running = visible.filter((j) => j.status === "running");
  const queued = visible
    .map((j, i) => [j, i])
    .filter(([j]) => j.status === "queued")
    .sort(([a, ai], [b, bi]) => (a.position ?? Infinity) - (b.position ?? Infinity) || ai - bi)
    .map(([j]) => j);
  const finished = visible.filter((j) => j.status !== "running" && j.status !== "queued").reverse();
  return [...running, ...queued, ...finished];
}

// Poster + real name (from the availability check, or picked up once the job ran),
// falling back to the URL/ID that was entered.
function renderJobTitle(job) {
  const poster = job.poster
    ? `<img class="job-thumb" src="${escapeHtml(job.poster)}" alt="" loading="lazy">`
    : `<div class="job-thumb job-thumb-empty"></div>`;
  const name = job.display_name
    ? `<div class="job-title-name">${escapeHtml(job.display_name)}${job.display_year ? ` <span class="job-year">(${job.display_year})</span>` : ""}</div>
       <div class="job-title-url">${escapeHtml(job.title || "")}</div>`
    : `<div class="job-title-name job-title-plain">${escapeHtml(job.title || "")}</div>`;
  return `<div class="job-title">${poster}<div class="job-title-text">${name}</div></div>`;
}

function refreshJobs() {
  refreshQueueState();
  fetch("/api/jobs")
    .then((r) => r.json())
    .then((jobs) => {
      const body = document.getElementById("job-table-body");
      body.innerHTML = "";
      orderJobsForTable(jobs).forEach((job) => {
        const tr = document.createElement("tr");
        const cancellable = job.status === "queued" || job.status === "running";
        const retryable = ["failed", "cancelled", "interrupted"].includes(job.status) && !job.retried_by;
        const continuable = job.status === "stopped" && !job.retried_by;
        // A retry stands in for the attempt it replaces (which is hidden), with a
        // link to that attempt's log.
        const retryInfo = job.attempt > 1
          ? `<div class="hint">${job.continuation ? "continued" : `retry #${job.attempt - 1}`}${job.resume_from ? ` &middot; from ${escapeHtml(job.resume_from)}` : ""}` +
            (job.retry_of ? ` &middot; <a href="/console?job=${job.retry_of}">previous log</a>` : "") + `</div>`
          : "";
        const statusInfo = job.status === "queued" && job.position
          ? `<div class="hint">#${job.position} in line</div>`
          : job.status === "interrupted" && job.current_episode
            ? `<div class="hint">stopped at ${escapeHtml(job.current_episode)}</div>`
            : job.status === "stopped" && job.current_episode
              ? `<div class="hint">before ${escapeHtml(job.current_episode)}</div>`
              : job.status === "running" && job.stop_requested
                ? `<div class="hint">stops after this episode</div>` : "";
        tr.innerHTML = `
          <td>${escapeHtml(job.service || "")}</td>
          <td>${renderJobTitle(job)}${retryInfo}</td>
          <td><span class="status status-${job.status}">${job.status}</span>${statusInfo}</td>
          <td class="cmd"><code>${escapeHtml(job.command)}</code></td>
          <td>
            <a href="/console?job=${job.id}">Console</a>
            ${cancellable ? `<button data-cancel="${job.id}">Cancel</button>` : ""}
            ${retryable ? `<button data-retry="${job.id}">Retry</button>` : ""}
            ${continuable ? `<button data-continue="${job.id}" class="primary">Continue</button>` : ""}
            ${job.pin_id ? `<span class="pin-badge" title="Part of a pinned show">pinned</span>`
              : `<button data-pin="${job.id}" title="Pin this show: check for new episodes from now on">Pin</button>`}
          </td>`;
        body.appendChild(tr);
      });
      body.querySelectorAll("[data-cancel]").forEach((btn) => {
        btn.addEventListener("click", () => {
          fetch(`/api/jobs/${btn.dataset.cancel}/cancel`, { method: "POST" }).then(refreshJobs);
        });
      });
      body.querySelectorAll("[data-pin]").forEach((btn) => {
        btn.addEventListener("click", () => {
          btn.disabled = true;
          pinJob(btn.dataset.pin).then(() => { refreshJobs(); refreshPins(); });
        });
      });
      body.querySelectorAll("[data-continue]").forEach((btn) => {
        btn.addEventListener("click", () => {
          btn.disabled = true;
          jobAction(btn.dataset.continue, "continue").catch((err) => alert(err.message)).then(refreshJobs);
        });
      });
      body.querySelectorAll("[data-retry]").forEach((btn) => {
        btn.addEventListener("click", () => {
          btn.disabled = true;
          fetch(`/api/jobs/${btn.dataset.retry}/retry`, { method: "POST" })
            .then(async (r) => {
              const res = await r.json().catch(() => ({}));
              if (!r.ok) alert(res.error || "Retry failed (restart the web UI if it was just updated).");
            })
            .then(refreshJobs);
        });
      });
    });
}

/* ---------------- Console page ---------------- */

let currentSource = null;

function initConsolePage() {
  document.getElementById("console-input-form").addEventListener("submit", (e) => {
    e.preventDefault();
    sendConsoleInput();
  });
  const select = document.getElementById("job-select");
  const initial = document.getElementById("console-output").dataset.initialJob;
  // Opened without a specific job (?job=...): show whatever is running, and keep
  // following the queue to the next running job - until the user picks one.
  followRunning = !initial;
  refreshJobSelect(initial);
  setInterval(() => refreshJobSelect(), 3000);
  select.addEventListener("change", () => {
    followRunning = false;
    connectToJob(select.value);
  });
  initConsoleView();
  initJobActions();
}

// Download progress / CLI / both - remembered as a setting (Settings page too).
// Shows only (a job that has reached an episode): finish the current episode, then
// stop - and pause the queue with it; Continue picks up at the next episode.
function renderJobActions(job) {
  const box = document.getElementById("job-actions");
  if (!box) return;
  let html = "";
  if (job.status === "running" && job.stop_requested) {
    html = `<span class="job-action-note">Stopping after this episode. The queue is paused too.</span>
      <button type="button" data-job-action="cancel_stop">Keep going</button>`;
  } else if (job.status === "running" && job.current_episode) {
    html = `<button type="button" data-job-action="stop_after_episode"
      title="${escapeHtml(job.current_episode)} is finished (downloaded, muxed, moved), then the download stops and the queue pauses">
      Stop after this episode</button>`;
  } else if (job.status === "stopped" && !job.retried_by) {
    html = `<span class="job-action-note">Stopped${job.current_episode ? ` before ${escapeHtml(job.current_episode)}` : ""}.</span>
      <button type="button" class="primary" data-job-action="continue">Continue${job.current_episode ? ` from ${escapeHtml(job.current_episode)}` : ""}</button>`;
  }
  html += job.pin_id ? `<span class="pin-badge">pinned show</span>`
    : (job.current_episode || job.status === "queued") ? `<button type="button" data-job-action="pin">Pin show</button>` : "";
  if (box.dataset.html !== html) {  // don't rebuild (and lose a hover/click) every poll
    box.dataset.html = html;
    box.innerHTML = html;
    box.dataset.job = job.id;
  }
}

function jobAction(jobId, action) {
  return fetch(`/api/jobs/${jobId}/${action}`, { method: "POST" })
    .then(async (r) => {
      const res = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(res.error || "Didn't work (restart the web UI if it was just updated).");
      return res;
    });
}

function initJobActions() {
  const box = document.getElementById("job-actions");
  if (!box) return;
  box.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-job-action]");
    if (!btn) return;
    btn.disabled = true;
    const action = btn.dataset.jobAction;
    if (action === "pin") {
      pinJob(box.dataset.job).then(() => refreshJobSelect());
      return;
    }
    jobAction(box.dataset.job, action)
      .then((res) => {
        // Continue starts a new job: follow it.
        if (action === "continue" && res.id) {
          followRunning = true;
          connectToJob(res.id);
        }
        refreshJobSelect();
      })
      .catch((err) => alert(err.message));
  });
}

function initConsoleView() {
  const page = document.getElementById("console-page");
  if (!page) return;
  const setView = (view) => {
    page.classList.remove("view-bars", "view-cli", "view-both");
    page.classList.add(`view-${view}`);
    page.dataset.view = view;
    page.querySelectorAll(".view-switch button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
    const output = document.getElementById("console-output");
    if (view !== "bars") output.scrollTop = output.scrollHeight;
  };
  setView(page.dataset.view || "bars");
  page.querySelectorAll(".view-switch button").forEach((btn) => {
    btn.addEventListener("click", () => {
      setView(btn.dataset.view);
      saveUiSetting("console_view", btn.dataset.view).catch(() => {});
    });
  });
}

// Header card: real name + poster when known, the URL/ID otherwise.
function updateJobHeader(job) {
  const header = document.getElementById("job-header");
  if (!header || !job) return;
  header.hidden = false;
  const name = job.display_name
    ? job.display_name + (job.display_year ? ` (${job.display_year})` : "")
    : job.title || "";
  document.getElementById("job-name").textContent = name;
  const status = document.getElementById("job-status");
  status.textContent = job.status;
  status.className = `status status-${job.status}`;
  const sub = [job.service, job.current_episode, job.display_name ? job.title : null].filter(Boolean);
  document.getElementById("job-sub").textContent = sub.join(" · ");
  const poster = document.getElementById("job-poster");
  if (job.poster) {
    if (poster.getAttribute("src") !== job.poster) poster.src = job.poster;
    poster.hidden = false;
  } else {
    poster.hidden = true;
  }
  if (job.command) document.getElementById("job-command").textContent = job.command;
  renderJobActions(job);
}

let followRunning = false;

function refreshJobSelect(preferred) {
  fetch("/api/jobs")
    .then((r) => r.json())
    .then((jobs) => {
      const select = document.getElementById("job-select");
      const current = select.value;
      select.innerHTML = jobs
        .slice()
        .reverse()
        .map((j) => `<option value="${j.id}">${escapeHtml(j.service || "?")}: ${escapeHtml(truncateMiddle(j.display_name || j.title, 45))} (${j.status})</option>`)
        .join("");

      const running = jobs.find((j) => j.status === "running");
      // While following: switch to the running job; once nothing runs, stay on the
      // job we were showing (its finished log) instead of jumping somewhere else.
      const followed = followRunning ? (running ? running.id : current) : "";
      const want = preferred || followed || current ||
        (running ? running.id : jobs.length ? jobs[jobs.length - 1].id : "");
      const wanted = jobs.find((j) => j.id === want);
      if (wanted) {
        updateConsoleInputState(wanted.status);
        updateJobHeader(wanted);
      }
      if (want) {
        select.value = want;
        // A finished job's stream ends and closes on purpose - reconnecting to it
        // on every poll rebuilt the whole log each time (the flicker, and the jump
        // back to the bottom). Only reconnect for a different job, or when a still
        // running job's stream was lost.
        const lostRunning = !currentSource && wanted && wanted.status === "running" && !streamEnded;
        if (want !== connectedJobId || lostRunning) connectToJob(want);
      }
    });
}

// Answers interactive prompts (Disney+ Web Token / OTP / PIN, SonyLIV accessToken,
// ...) by writing a line to the running job's stdin.
function sendConsoleInput() {
  const input = document.getElementById("console-input");
  const msg = document.getElementById("console-input-message");
  const jobId = document.getElementById("job-select").value;
  if (!jobId) return;
  fetch(`/api/jobs/${jobId}/input`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: input.value }),
  })
    .then(async (r) => {
      const body = await r.json();
      if (!r.ok) throw new Error(body.error || "Failed to send");
      input.value = "";
      msg.textContent = "";
    })
    .catch((e) => {
      msg.textContent = e.message;
    });
}

function updateConsoleInputState(status) {
  const running = status === "running";
  document.getElementById("console-input").disabled = !running;
  document.getElementById("console-input-send").disabled = !running;
}

// Console rendering is append-only (new text nodes, never a full textContent
// replacement) so an in-progress text selection survives new output arriving -
// replacing textContent on every message, as this used to, tears down and
// rebuilds every child node, which silently kills any selection/copy in
// progress. Incoming lines are also batched and flushed on a short timer
// instead of one DOM write per SSE message, since a chatty job can otherwise
// push many messages a second, forcing a browser reflow with each one.
let pendingLines = [];
let flushTimer = null;
let lastLineNode = null; // the newest line on screen, target of in-place redraws
let connectedJobId = null;
let streamEnded = false;

function isNearBottom(el, threshold = 60) {
  return el.scrollHeight - el.scrollTop - el.clientHeight < threshold;
}

function flushConsoleOutput() {
  flushTimer = null;
  if (!pendingLines.length) return;
  const output = document.getElementById("console-output");
  const last = pendingLines.pop();
  const chunk = pendingLines.length ? pendingLines.join("\n") + "\n" : "";
  pendingLines = [];
  const stick = isNearBottom(output);
  if (chunk) output.appendChild(document.createTextNode(chunk));
  // The newest line gets its own node so a redraw can replace just that line.
  lastLineNode = document.createTextNode(last + "\n");
  output.appendChild(lastLineNode);
  setLastLine(last);
  // Only auto-follow the tail if the user was already at (or near) the bottom -
  // otherwise this forces their view back down mid-scroll, which is what made
  // scrolling up to read earlier output ("the start of the CLI") unusable.
  if (stick) output.scrollTop = output.scrollHeight;
}

// The newest log line, shown under the header - in "Download progress" view it's the
// only log output visible, so e.g. an "Enter OTP:" prompt still gets noticed.
function setLastLine(line) {
  const el = document.getElementById("console-last-line");
  if (!el) return;
  // Strip the "2026-10-04 23:05:41 [I] DSNP : " prefix.
  el.textContent = (line || "").replace(/^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \[\w\] [^:]+ : /, "").trim()
    .replace(/^Progress: (\d+%)/, "Muxing: $1");
}

function scheduleConsoleFlush() {
  if (flushTimer !== null) return;
  flushTimer = setTimeout(flushConsoleOutput, 120);
}

function connectToJob(jobId) {
  if (!jobId) return;
  if (currentSource) {
    currentSource.close();
    currentSource = null;
  }
  clearTimeout(flushTimer);
  flushTimer = null;
  pendingLines = [];
  lastLineNode = null;
  connectedJobId = jobId;
  streamEnded = false;
  document.getElementById("console-output").textContent = "";
  resetProgressPanel();

  fetch(`/api/jobs/${jobId}`)
    .then((r) => r.json())
    .then((job) => {
      document.getElementById("job-command").textContent = job.command || "";
      updateConsoleInputState(job.status);
      updateJobHeader(job);
    });

  currentSource = new EventSource(`/api/jobs/${jobId}/stream`);
  currentSource.onmessage = (e) => {
    pendingLines.push(e.data);
    scheduleConsoleFlush();
  };
  currentSource.addEventListener("progress", (e) => {
    pendingProgress = e.data;
    if (progressFrame === null) progressFrame = requestAnimationFrame(renderProgressPanel);
  });
  currentSource.addEventListener("replace", (e) => {
    // In-place redraw (e.g. mkvmerge's "Progress: 42%"): update the last line.
    if (pendingLines.length) {
      pendingLines[pendingLines.length - 1] = e.data;
    } else if (lastLineNode) {
      lastLineNode.textContent = e.data + "\n";
      setLastLine(e.data);
    } else {
      pendingLines.push(e.data);
      scheduleConsoleFlush();
    }
  });
  currentSource.addEventListener("end", () => {
    streamEnded = true;
    flushConsoleOutput();
    if (currentSource) {
      currentSource.close();
      currentSource = null;
    }
  });
  currentSource.onerror = () => {
    flushConsoleOutput();
    if (currentSource) {
      currentSource.close();
      currentSource = null;
    }
  };
}

// Live per-track download progress. vinetrimmer (run with VT_WEBUI_PROGRESS=1 by
// webui/queue_manager.py) prints periodic JSON snapshots of every track's state in
// place of Rich's live tree, which can't redraw over a pipe; the server strips them
// out of the log and forwards them as SSE "progress" events. Only the latest
// snapshot matters, so renders are coalesced to one per animation frame.
let pendingProgress = null;
let progressFrame = null;

function resetProgressPanel() {
  if (progressFrame !== null) cancelAnimationFrame(progressFrame);
  progressFrame = null;
  pendingProgress = null;
  const panel = document.getElementById("progress-panel");
  panel.innerHTML = "";
  panel.hidden = true;
  const empty = document.getElementById("progress-empty");
  if (empty) empty.hidden = false;
  const last = document.getElementById("console-last-line");
  if (last) last.textContent = "";
}

// 75 -> "1:15", 3725 -> "1:02:05" (same style as Rich's compact time column).
function formatDuration(sec) {
  if (sec === undefined || sec === null || !isFinite(sec)) return "";
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = String(sec % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
}

function trackPct(t) {
  return Math.max(0, Math.min(100, t.total ? (t.completed / t.total) * 100 : 0));
}

function trackState(t) {
  if (/fail/i.test(t.status || "")) return "failed";
  if (t.status === "Downloaded") return "done";
  if (t.elapsed === undefined) return "queued";
  return "active";
}

function renderTrackRow(t, big) {
  const pct = trackPct(t);
  const state = trackState(t);
  let time = "";
  if (state === "done") time = `took ${formatDuration(t.elapsed)}`;
  else if (state === "active") {
    time = t.eta !== undefined ? `ETA ${formatDuration(t.eta)}` : "ETA &hellip;";
    time += ` &middot; ${formatDuration(t.elapsed)} elapsed`;
  }
  return `
    <div class="progress-track ${state}${big ? " big" : ""}">
      <div class="progress-name" title="${escapeHtml(t.name)}">${escapeHtml(t.name)}</div>
      <div class="progress-row">
        <div class="progress-bar"><div style="width:${pct.toFixed(1)}%"></div></div>
        <span class="progress-pct">${pct.toFixed(0)}%</span>
        <span class="progress-status">${escapeHtml(state === "queued" ? "Queued" : t.status || "")}</span>
        <span class="progress-time">${time}</span>
      </div>
    </div>`;
}

// Remembered across re-renders (the panel's HTML is rebuilt on every snapshot).
let subtitlesExpanded = false;

function renderProgressPanel() {
  progressFrame = null;
  if (pendingProgress === null) return;
  let snapshot;
  try {
    snapshot = JSON.parse(pendingProgress);
  } catch (e) {
    return;
  }
  pendingProgress = null;
  const tracks = snapshot.tracks || [];
  const panel = document.getElementById("progress-panel");
  panel.hidden = tracks.length === 0;
  const empty = document.getElementById("progress-empty");
  if (empty) empty.hidden = tracks.length > 0;
  if (!tracks.length) return;

  const byType = (type) => tracks.filter((t) => t.type === type);
  const main = tracks.filter((t) => t.type !== "Subtitle");
  const subs = byType("Subtitle");
  const done = tracks.filter((t) => trackState(t) === "done").length;

  // Overall ETA: tracks download in parallel, so the job's remaining time is
  // roughly the slowest active video/audio track's.
  const etas = main.filter((t) => trackState(t) === "active" && t.eta !== undefined).map((t) => t.eta);
  const overall = etas.length ? ` &middot; about ${formatDuration(Math.max(...etas))} remaining` : "";
  const html = [`<div class="progress-summary">${done} / ${tracks.length} tracks downloaded${overall}</div>`];

  ["Video", "Audio"].forEach((type) => {
    const list = byType(type);
    if (!list.length) return;
    html.push(`<div class="progress-group">${list.length} ${type}${list.length === 1 ? "" : "s"}</div>`);
    list.forEach((t) => html.push(renderTrackRow(t, type === "Video")));
  });

  if (subs.length) {
    const subsDone = subs.filter((t) => trackState(t) === "done").length;
    const subsFailed = subs.some((t) => trackState(t) === "failed");
    const subsPct = (subsDone / subs.length) * 100;
    html.push(`
      <details class="progress-subs"${subtitlesExpanded ? " open" : ""}>
        <summary class="progress-group">${subs.length} Subtitle${subs.length === 1 ? "" : "s"} (${subsDone} / ${subs.length} done)</summary>
        <div class="progress-track ${subsFailed ? "failed" : subsDone === subs.length ? "done" : "active"}">
          <div class="progress-row">
            <div class="progress-bar"><div style="width:${subsPct.toFixed(1)}%"></div></div>
            <span class="progress-pct">${subsPct.toFixed(0)}%</span>
          </div>
        </div>
        ${subs.map((t) => renderTrackRow(t, false)).join("")}
      </details>`);
  }

  const scroll = panel.scrollTop;
  panel.innerHTML = html.join("");
  panel.scrollTop = scroll;
  const details = panel.querySelector(".progress-subs");
  if (details) details.addEventListener("toggle", () => { subtitlesExpanded = details.open; });
}

/* ---------------- Credentials page ---------------- */

function initCredentialsPage() {
  document.getElementById("cred-service").addEventListener("change", loadCredStatus);
  document.getElementById("btn-save-credential").addEventListener("click", saveCredential);
  document.getElementById("btn-upload-cookie").addEventListener("click", uploadCookie);
}

function loadCredStatus() {
  const service = document.getElementById("cred-service").value;
  const statusEl = document.getElementById("cred-status");
  if (!service) {
    statusEl.innerHTML = "";
    return;
  }
  fetch(`/api/services/${encodeURIComponent(service)}/info`)
    .then((r) => r.json())
    .then((info) => {
      statusEl.innerHTML = renderServiceCheck(info.check);
    });
}

function saveCredential() {
  const service = document.getElementById("cred-service").value;
  const msg = document.getElementById("cred-message");
  if (!service) {
    msg.textContent = "Select a service first.";
    return;
  }
  const payload = {
    username: document.getElementById("cred-username").value,
    password: document.getElementById("cred-password").value,
  };
  fetch(`/api/credentials/${encodeURIComponent(service)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
    .then(async (r) => {
      const body = await r.json();
      if (!r.ok) throw new Error(body.error || "Failed to save");
      return body;
    })
    .then(() => {
      msg.textContent = "Saved.";
      document.getElementById("cred-password").value = "";
      loadCredStatus();
    })
    .catch((e) => {
      msg.textContent = e.message;
    });
}

function uploadCookie() {
  const service = document.getElementById("cred-service").value;
  const msg = document.getElementById("cookie-message");
  const fileInput = document.getElementById("cookie-file");
  if (!service) {
    msg.textContent = "Select a service first.";
    return;
  }
  if (!fileInput.files.length) {
    msg.textContent = "Choose a cookie file first.";
    return;
  }
  const form = new FormData();
  form.append("file", fileInput.files[0]);
  fetch(`/api/cookies/${encodeURIComponent(service)}`, { method: "POST", body: form })
    .then(async (r) => {
      const body = await r.json();
      if (!r.ok) throw new Error(body.error || "Failed to upload");
      return body;
    })
    .then((body) => {
      msg.textContent = `Saved to ${body.path}`;
      loadCredStatus();
    })
    .catch((e) => {
      msg.textContent = e.message;
    });
}

/* ---------------- Update tab ---------------- */

// Small dot on the "Update" tab when a newer version is out (uses the server's cached
// GitHub answer - at most one request every few hours).
function checkUpdateBadge() {
  const dot = document.querySelector("#nav-update .nav-dot");
  if (!dot) return;
  fetch("/api/update")
    .then((r) => (r.ok ? r.json() : null))
    .then((info) => { if (info) dot.hidden = !info.update_available; })
    .catch(() => {});
}

function initUpdatePage() {
  initInstantSettings();
  fetch("/api/preferences").then((r) => r.json()).then(fillRetrySettings);
  document.getElementById("setting-update-prereleases").addEventListener("change", () => setTimeout(() => loadUpdates(false), 300));
  document.getElementById("btn-update-check").addEventListener("click", () => loadUpdates(true));
  document.getElementById("btn-update-restart").addEventListener("click", restartWebUi);
  document.getElementById("update-list").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-install]");
    if (btn) installUpdate(btn.dataset.install, btn.dataset.state);
  });
  loadUpdates(false);
}

function loadUpdates(force) {
  const status = document.getElementById("update-status");
  status.textContent = force ? "Asking GitHub…" : "";
  fetch(`/api/update${force ? "?force=1" : ""}`)
    .then((r) => r.json())
    .then((info) => {
      document.getElementById("update-repo").textContent = info.repo;
      const when = info.checked_at ? new Date(info.checked_at * 1000).toLocaleString() : "never";
      status.textContent = info.error ? info.error
        : info.update_available ? `Version ${info.latest.version} is available. (Checked ${when})`
        : `You're on the newest version. (Checked ${when})`;
      document.querySelector("#nav-update .nav-dot").hidden = !info.update_available;
      document.getElementById("update-list").innerHTML = info.releases.length ? info.releases.map((r) => {
        const date = r.published_at ? new Date(r.published_at).toLocaleDateString() : "";
        const label = r.state === "newer" ? "Install" : r.state === "current" ? "Reinstall" : "Go back to this version";
        return `
          <div class="release release-${r.state}">
            <div class="release-head">
              <strong>${escapeHtml(r.name || r.tag)}</strong>
              <code>${escapeHtml(r.tag)}</code>
              ${r.state === "current" ? '<span class="badge badge-ok">installed</span>' : ""}
              ${r.state === "newer" ? '<span class="badge badge-new">new</span>' : ""}
              ${r.prerelease ? '<span class="badge">pre-release</span>' : ""}
              <span class="hint">${escapeHtml(date)}</span>
              <span class="release-actions">
                ${r.url ? `<a href="${escapeHtml(r.url)}" target="_blank" rel="noopener">GitHub</a>` : ""}
                <button type="button" data-install="${escapeHtml(r.tag)}" data-state="${r.state}"${r.state === "newer" ? ' class="primary"' : ""}${info.busy ? ` disabled title="${escapeHtml(info.busy)}"` : ""}>${label}</button>
              </span>
            </div>
            ${r.notes ? `<div class="release-notes">${escapeHtml(r.notes)}</div>` : ""}
          </div>`;
      }).join("") : `<p class="hint">No releases published yet.</p>`;
      if (info.busy) document.getElementById("update-message").textContent = info.busy;
    })
    .catch((e) => { status.textContent = `Couldn't load updates: ${e.message}`; });
}

function installUpdate(tag, state) {
  const msg = document.getElementById("update-message");
  const what = state === "older" ? `go back to ${tag}` : state === "current" ? `reinstall ${tag}` : `install ${tag}`;
  if (!confirm(`Download and ${what}?\n\nReplaced files are backed up to backups/ first. Your configs, credentials, ` +
               "services, devices, cookies, key vault, downloads and queue aren't touched.")) return;
  msg.textContent = `Downloading and installing ${tag}…`;
  document.querySelectorAll("[data-install]").forEach((b) => (b.disabled = true));
  fetch("/api/update/install", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ tag }) })
    .then(async (r) => {
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.error || "The update failed.");
      return body;
    })
    .then((res) => {
      msg.textContent = "";
      const deps = res.dependencies_changed && res.dependencies_changed.length
        ? ` The dependencies changed (${res.dependencies_changed.join(", ")}), so run install.bat before restarting.` : "";
      document.getElementById("update-restart-text").textContent =
        `${res.installed} installed (${res.changed} file(s) changed, ${res.added} new)${res.backup ? ", backup saved in backups/" : ""}.${deps} Restart the web UI to use it.`;
      document.getElementById("update-restart").hidden = false;
    })
    .catch((e) => {
      msg.textContent = e.message;
      loadUpdates(false);
    });
}

function restartWebUi() {
  const text = document.getElementById("update-restart-text");
  fetch("/api/update/restart", { method: "POST" })
    .then(async (r) => {
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.error || "Couldn't restart.");
      text.textContent = "Restarting… this page reloads once the web UI is back (it opens in a new window).";
      const oldBoot = document.body.dataset.boot;
      const poll = setInterval(() => {
        fetch("/update", { cache: "no-store" })
          .then((r) => r.text())
          .then((html) => {
            const m = html.match(/data-boot="([^"]+)"/);
            if (m && m[1] !== oldBoot) {
              clearInterval(poll);
              window.location.reload();
            }
          })
          .catch(() => {});
      }, 1500);
    })
    .catch((e) => { text.textContent = e.message; });
}
