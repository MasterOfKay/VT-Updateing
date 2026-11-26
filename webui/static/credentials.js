/* The Credentials tab: service logins and cookies, and the VPN. */

(() => {
  const el = (id) => document.getElementById(id);

  function request(url, options) {
    return fetch(url, options).then(async (r) => {
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.error || "That didn't work. Restart the web UI if it was just updated.");
      return body;
    });
  }

  const postJson = (url, data) => request(url, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
  });

  /* ---------------- tabs (#vpn opens the VPN part) ---------------- */

  function showTab(tab) {
    el("tab-logins").hidden = tab !== "logins";
    el("tab-vpn").hidden = tab !== "vpn";
    document.querySelectorAll("[data-tab]").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  }

  /* ---------------- service logins and cookies ---------------- */

  let service = "";

  function loadOverview() {
    request("/api/credentials").then((overview) => {
      document.querySelectorAll("#cred-list .rail-item").forEach((item) => {
        const have = overview[item.dataset.service] || {};
        const parts = [have.login && "login", have.cookies && "cookies"].filter(Boolean);
        let sub = item.querySelector(".rail-sub");
        if (!sub) {
          sub = document.createElement("span");
          sub.className = "rail-sub";
          item.appendChild(sub);
        }
        sub.textContent = parts.length ? `${parts.join(" and ")} saved` : "";
        item.classList.toggle("has-data", parts.length > 0);
      });
    }).catch(() => {});
  }

  function loadStatus() {
    const asked = service;
    request(`/api/services/${encodeURIComponent(asked)}/info`)
      .then((info) => {
        if (asked === service) el("cred-status").innerHTML = renderServiceCheck(info.check);
      })
      .catch((e) => { el("cred-status").textContent = e.message; });
  }

  function chooseService(name) {
    service = name;
    document.querySelectorAll("#cred-list .rail-item").forEach((item) => {
      const active = item.dataset.service === name;
      item.classList.toggle("active", active);
      item.setAttribute("aria-selected", active);
      if (active) el("cred-title").textContent = item.querySelector(".rail-title").textContent;
    });
    el("cred-empty").hidden = true;
    el("cred-detail").hidden = false;
    ["cred-username", "cred-password", "cookie-file"].forEach((id) => (el(id).value = ""));
    ["cred-message", "cookie-message"].forEach((id) => say(el(id), ""));
    showCookieName();
    el("cred-status").innerHTML = "";
    loadStatus();
  }

  function saveCredential() {
    const msg = el("cred-message");
    postJson(`/api/credentials/${encodeURIComponent(service)}`,
      { username: el("cred-username").value, password: el("cred-password").value })
      .then(() => {
        say(msg, "Login saved.", "ok");
        el("cred-password").value = "";
        loadStatus();
        loadOverview();
      })
      .catch((e) => { say(msg, e.message, "err"); });
  }

  function showCookieName() {
    const file = el("cookie-file").files[0];
    el("cookie-drop").querySelector(".drop-text").textContent =
      file ? file.name : "Drop the cookie file here, or choose one";
  }

  function uploadCookie() {
    const msg = el("cookie-message");
    const input = el("cookie-file");
    if (!input.files.length) {
      say(msg, "Choose a cookie file first.");
      return;
    }
    const form = new FormData();
    form.append("file", input.files[0]);
    request(`/api/cookies/${encodeURIComponent(service)}`, { method: "POST", body: form })
      .then((body) => {
        say(msg, `Saved to ${body.path}`, "ok");
        loadStatus();
        loadOverview();
      })
      .catch((e) => { say(msg, e.message, "err"); });
  }

  function initDrop() {
    const drop = el("cookie-drop");
    ["dragenter", "dragover"].forEach((type) => drop.addEventListener(type, (e) => {
      e.preventDefault();
      drop.classList.add("over");
    }));
    ["dragleave", "drop"].forEach((type) => drop.addEventListener(type, () => drop.classList.remove("over")));
    drop.addEventListener("drop", (e) => {
      e.preventDefault();
      if (!e.dataTransfer.files.length) return;
      el("cookie-file").files = e.dataTransfer.files;
      showCookieName();
    });
    el("cookie-file").addEventListener("change", showCookieName);
  }

  /* ---------------- VPN ---------------- */

  let vpn = null;

  function chosenProvider() {
    const radio = document.querySelector('[name="vpn-provider"]:checked');
    return radio ? radio.value : "";
  }

  function showProvider() {
    const provider = chosenProvider();
    document.querySelectorAll("[data-provider]").forEach((f) => (f.hidden = f.dataset.provider !== provider));
  }

  function serverRow(country = "", number = "") {
    const row = document.createElement("div");
    row.className = "row server-row";
    row.innerHTML = `
      <label>Country code<input type="text" data-f="country" maxlength="2" placeholder="id" value="${escapeHtml(country)}"></label>
      <label>Server number<input type="number" data-f="number" min="1" placeholder="69" value="${escapeHtml(number)}"></label>
      <button type="button" data-remove-row>Remove</button>`;
    return row;
  }

  function proxyRow(p = {}) {
    const row = document.createElement("div");
    row.className = "row proxy-row";
    row.dataset.original = p.country || "";
    row.innerHTML = `
      <label>Country code<input type="text" data-f="country" maxlength="2" placeholder="id" value="${escapeHtml(p.country || "")}"></label>
      <label>Type<select data-f="scheme">
        <option value="https"${p.scheme !== "http" ? " selected" : ""}>HTTPS</option>
        <option value="http"${p.scheme === "http" ? " selected" : ""}>HTTP</option></select></label>
      <label class="wide">Server<input type="text" data-f="host" placeholder="proxy.example.com" value="${escapeHtml(p.host || "")}"></label>
      <label>Port<input type="number" data-f="port" min="1" max="65535" value="${escapeHtml(p.port || "")}"></label>
      <label>Username<input type="text" data-f="username" autocomplete="off" value="${escapeHtml(p.username || "")}"></label>
      <label>Password<input type="password" data-f="password" autocomplete="new-password"
        placeholder="${p.has_login ? "saved" : ""}"></label>
      <button type="button" data-remove-row>Remove</button>`;
    return row;
  }

  const rowValues = (row) => Object.fromEntries(
    [...row.querySelectorAll("[data-f]")].map((input) => [input.dataset.f, input.value.trim()]));

  function fillVpn(state) {
    vpn = state;
    const radio = document.querySelector(`[name="vpn-provider"][value="${state.provider}"]`);
    if (radio) radio.checked = true;
    ["nordvpn", "surfshark"].forEach((name) => {
      const saved = state.logins[name];
      el(`${name}-username`).value = "";
      el(`${name}-password`).value = "";
      el(`${name}-remove`).checked = false;
      el(`${name}-remove-row`).hidden = !saved;
      const note = el(`${name}-state`);
      note.textContent = saved ? "A login is saved. Fill both fields only to replace it." : "No login saved yet.";
      note.classList.remove("warn");
      if (name === "nordvpn" && state.nordvpn_email_login) {
        note.textContent = "The saved username looks like an email address, which NordVPN's proxies refuse. " +
          "Replace it with your service credentials.";
        note.classList.add("warn");
      }
    });
    el("nordvpn-servers").replaceChildren(
      ...Object.entries(state.nordvpn_servers).map(([c, n]) => serverRow(c, n)));
    el("proxy-rows").replaceChildren(...(state.proxies.length ? state.proxies : [{}]).map(proxyRow));
    showProvider();

    const select = el("vpn-test-country");
    const before = select.value;
    select.innerHTML = state.countries.map((c) => `<option value="${c.code}">${escapeHtml(c.name)}</option>`).join("");
    const wanted = [before, vpnMemory().last, "us"];
    select.value = wanted.find((code) => state.countries.some((c) => c.code === code)) || "";
    if (select.selectedIndex < 0) select.selectedIndex = 0;
    el("btn-test-vpn").disabled = !state.ready;
    el("vpn-test-result").textContent = state.ready ? "" : state.problem;
    el("vpn-test-result").className = "result";
    el("vpn-home").textContent = state.home
      ? `Without the VPN, websites see ${countryName(state.home)}.` : "";
  }

  function saveVpn() {
    const msg = el("vpn-message");
    const provider = chosenProvider();
    const data = { provider };
    if (provider === "nordvpn" || provider === "surfshark") {
      data[provider] = {
        username: el(`${provider}-username`).value, password: el(`${provider}-password`).value,
        remove: el(`${provider}-remove`).checked,
      };
    }
    if (provider === "nordvpn") {
      data.nordvpn_servers = Object.fromEntries([...el("nordvpn-servers").children]
        .map(rowValues).filter((r) => r.country || r.number).map((r) => [r.country, r.number]));
    }
    if (provider === "custom") {
      data.proxies = [...el("proxy-rows").children]
        .map((row) => ({ original: row.dataset.original, ...rowValues(row) }))
        .filter((r) => r.country || r.host);
    }
    say(msg, "");
    postJson("/api/vpn", data)
      .then((state) => {
        fillVpn(state);
        const fine = state.ready || !state.provider;
        say(msg, fine ? "VPN settings saved." : `Saved. ${state.problem}`, fine ? "ok" : "");
      })
      .catch((e) => { say(msg, e.message, "err"); });
  }

  function testVpn() {
    const out = el("vpn-test-result");
    const button = el("btn-test-vpn");
    button.disabled = true;
    out.className = "result";
    out.textContent = "Connecting…";
    postJson("/api/vpn/test", { country: el("vpn-test-country").value })
      .then((res) => {
        out.className = `result ${res.ok ? "result-ok" : "result-err"}`;
        out.textContent = res.message;
      })
      .catch((e) => {
        out.className = "result result-err";
        out.textContent = e.message;
      })
      .finally(() => { button.disabled = false; });
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => {
      history.replaceState(null, "", b.dataset.tab === "vpn" ? "#vpn" : window.location.pathname);
      showTab(b.dataset.tab);
    }));
    window.addEventListener("hashchange", () => showTab(location.hash === "#vpn" ? "vpn" : "logins"));
    showTab(location.hash === "#vpn" ? "vpn" : "logins");

    el("cred-list").addEventListener("click", (e) => {
      const item = e.target.closest("[data-service]");
      if (item) chooseService(item.dataset.service);
    });
    el("cred-search").addEventListener("input", () => {
      const wanted = el("cred-search").value.trim().toLowerCase();
      document.querySelectorAll("#cred-list .rail-item").forEach((item) => {
        item.hidden = !item.dataset.label.includes(wanted) && !item.dataset.service.toLowerCase().includes(wanted);
      });
    });
    el("btn-save-credential").addEventListener("click", saveCredential);
    el("btn-upload-cookie").addEventListener("click", uploadCookie);
    initDrop();
    loadOverview();

    document.querySelectorAll('[name="vpn-provider"]').forEach((r) => r.addEventListener("change", showProvider));
    el("btn-add-server").addEventListener("click", () => el("nordvpn-servers").appendChild(serverRow()));
    el("btn-add-proxy").addEventListener("click", () => el("proxy-rows").appendChild(proxyRow()));
    el("tab-vpn").addEventListener("click", (e) => {
      if (e.target.closest("[data-remove-row]")) e.target.closest(".row").remove();
    });
    el("btn-save-vpn").addEventListener("click", saveVpn);
    el("btn-test-vpn").addEventListener("click", testVpn);
    request("/api/vpn").then(fillVpn).catch((e) => { say(el("vpn-message"), e.message, "err"); });
  });
})();
