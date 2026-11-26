/* ---------------- Discover tab: browse JustWatch, send a title to the downloader ----------------
   Uses escapeHtml() from app.js, which is loaded first. */
(() => {
  const root = document.getElementById("discover");
  if (!root) return;

  const STORE = "vt-discover";
  const $ = (id) => document.getElementById(id);
  const results = $("discover-results");
  const message = $("discover-message");
  const moreButton = $("discover-more");
  const dialog = $("title-dialog");
  const dialogBody = $("title-dialog-body");

  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(STORE) || "{}"); } catch (e) { /* ignore */ }
  const state = {
    mode: ["popular", "all"].includes(saved.mode) ? saved.mode : "new",
    sort: ["az", "za"].includes(saved.sort) ? saved.sort : "", // only for "all"
    access: ["included", "paid"].includes(saved.access) ? saved.access : "",
    kind: ["movie", "show"].includes(saved.kind) ? saved.kind : "",
    services: new Set(Array.isArray(saved.services) ? saved.services : []),
    query: "",
  };
  let providers = [];
  let next = null; // how to load more: {cursor, date} / {offset} / {date}
  let listRequest = 0; // answers of older requests are ignored
  let current = null; // the title shown in the dialog

  const remember = () => {
    try {
      localStorage.setItem(STORE, JSON.stringify({
        mode: state.mode, sort: state.sort, access: state.access, kind: state.kind, services: [...state.services],
      }));
    } catch (e) { /* private mode */ }
  };

  function api(path, params) {
    const query = new URLSearchParams({
      country: $("discover-country").value, language: $("discover-language").value, ...params,
    });
    return fetch(`${path}?${query}`).then(async (r) => {
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.error || "The request failed. Restart the web UI if you just updated it.");
      return body;
    });
  }

  /* ---- providers ---- */

  function loadProviders() {
    return api("/api/discover/providers", {}).then((list) => {
      providers = list;
      // Services picked earlier that this country doesn't have would filter everything away.
      state.services = new Set([...state.services].filter((s) => list.some((p) => p.service === s)));
      renderProviders();
    });
  }

  function renderProviders() {
    const all = `<button type="button" class="provider provider-all${state.services.size ? "" : " active"}" data-service="">All services</button>`;
    $("discover-providers").innerHTML = all + providers.map((p) => `
      <button type="button" class="provider${state.services.has(p.service) ? " active" : ""}" data-service="${escapeHtml(p.service)}"
        title="${escapeHtml(p.name)} (${escapeHtml(p.service)})">
        ${p.icon ? `<img src="${escapeHtml(p.icon)}" alt="">` : ""}<span>${escapeHtml(p.name)}</span></button>`).join("");
  }

  const providerOf = (service) => providers.find((p) => p.service === service);

  /* ---- listing ---- */

  function dayLabel(iso) {
    const day = new Date(`${iso}T12:00:00`);
    const diff = Math.round((new Date().setHours(12, 0, 0, 0) - day) / 86400000);
    const date = day.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
    return diff === 0 ? `Today · ${date}` : diff === 1 ? `Yesterday · ${date}` : date;
  }

  function cardHtml(item) {
    const badge = item.kind === "movie" ? "Movie"
      : item.season ? `Season ${item.season}${item.new_episodes ? ` · +${item.new_episodes}` : ""}` : "Show";
    const icons = item.services.slice(0, 5).map((s) => {
      const p = providerOf(s);
      return p && p.icon ? `<img src="${escapeHtml(p.icon)}" alt="${escapeHtml(s)}" title="${escapeHtml(s)}">` : `<span>${escapeHtml(s)}</span>`;
    }).join("");
    return `
      <button type="button" class="title-card" data-id="${escapeHtml(item.id)}" data-season="${item.season || ""}">
        <span class="title-art">
          ${item.poster ? `<img src="${escapeHtml(item.poster)}" alt="" loading="lazy">` : `<span class="title-art-empty">${escapeHtml(item.title || "")}</span>`}
          <span class="title-badge">${escapeHtml(badge)}</span>
        </span>
        <span class="title-name">${escapeHtml(item.title || "")}</span>
        <span class="title-meta">${item.year || ""}<span class="title-icons">${icons}</span></span>
      </button>`;
  }

  // The grid a day's (or the search's) cards go into, created when first needed.
  function grid(key, heading) {
    let el = results.querySelector(`[data-grid="${key}"]`);
    if (!el) {
      results.insertAdjacentHTML("beforeend",
        `${heading ? `<h2 class="discover-day">${escapeHtml(heading)}</h2>` : ""}<div class="title-grid" data-grid="${key}"></div>`);
      el = results.querySelector(`[data-grid="${key}"]`);
    }
    return el;
  }

  function load(from, skipped = 0) {
    const request = ++listRequest;
    if (!from) {
      results.innerHTML = "";
      next = null;
    }
    moreButton.hidden = true;
    const isNew = state.mode === "new" && !state.query;
    const isAll = state.mode === "all" && !state.query;
    if (!from) $("discover-count").hidden = true;
    $("discover-limit").hidden = true;
    message.textContent = isAll && state.sort && !from
      ? "Collecting the whole catalogue to sort it. For a large one this can take up to a minute…"
      : "Loading…";
    const params = {
      mode: isNew ? "new" : isAll ? "all" : "popular",
      services: [...state.services].join(","), kind: state.kind,
    };
    if (isAll && state.sort) params.sort = state.sort;
    if (state.access) params.access = state.access;
    if (state.query) params.q = state.query;
    if (from) Object.assign(params, from);

    api("/api/discover/titles", params).then((data) => {
      if (request !== listRequest) return;
      message.textContent = "";
      if (isNew) {
        if (data.items.length) {
          const el = grid(data.date, dayLabel(data.date));
          // A later page of a day can repeat a title from an earlier one.
          const seen = new Set([...el.children].map((c) => `${c.dataset.id}:${c.dataset.season}`));
          el.insertAdjacentHTML("beforeend", data.items
            .filter((i) => !seen.has(`${i.id}:${i.season || ""}`)).map(cardHtml).join(""));
        }
        next = data.next ? { date: data.date, cursor: data.next } : { date: data.earlier };
        moreButton.textContent = data.next ? "Load more" : "Show the day before";
        // Nothing (yet) on this day: go back a few days by itself.
        if (!data.items.length && !data.next && skipped < 3) {
          load(next, skipped + 1);
          return;
        }
        if (!results.children.length) message.textContent = "Nothing new found in these days.";
      } else {
        if (data.items.length) grid("list", "").insertAdjacentHTML("beforeend", data.items.map(cardHtml).join(""));
        next = data.next ? { offset: data.next } : null;
        moreButton.textContent = "Load more";
        if (!results.children.length) message.textContent = "Nothing found.";
        $("discover-limit").hidden = !data.limited || !!state.query;
        if (isAll && data.total) {
          $("discover-count").textContent = `${data.total.toLocaleString()} titles`;
          $("discover-count").hidden = false;
        }
      }
      moreButton.hidden = !next;
    }).catch((e) => {
      if (request !== listRequest) return;
      message.textContent = e.message;
      if (from) {
        next = from;
        moreButton.textContent = "Try again";
        moreButton.hidden = false;
      }
    });
  }

  function syncSwitches() {
    root.querySelectorAll("[data-mode]").forEach((b) => b.classList.toggle("active", !state.query && b.dataset.mode === state.mode));
    root.querySelectorAll("[data-kind]").forEach((b) => b.classList.toggle("active", b.dataset.kind === state.kind));
    $("discover-access").value = state.access;
    $("discover-sort").value = state.sort;
    $("discover-sort").hidden = state.mode !== "all" || !!state.query;
  }

  function reload() {
    remember();
    syncSwitches();
    load();
  }

  /* ---- one title ---- */

  const pad = (n) => String(n).padStart(2, "0");

  function openTitle(id, season) {
    current = { id, season: null, episode: null, data: null };
    dialogBody.innerHTML = `<p class="hint">Loading…</p>`;
    if (!dialog.open) dialog.showModal();
    api(`/api/discover/title/${encodeURIComponent(id)}`, {}).then((data) => {
      if (!current || current.id !== id) return;
      current.data = data;
      if (season && data.seasons.some((s) => s.number === Number(season))) current.season = Number(season);
      renderTitle();
      if (current.season) loadEpisodes();
    }).catch((e) => {
      dialogBody.innerHTML = `<p class="message">${escapeHtml(e.message)}</p>`;
    });
  }

  function renderTitle() {
    const t = current.data;
    const seasons = t.kind === "show" && t.seasons.length ? `
      <div class="dialog-pick">
        <label>Season
          <select id="title-season">
            <option value="">All seasons</option>
            ${t.seasons.map((s) => `<option value="${s.number}"${s.number === current.season ? " selected" : ""}>Season ${s.number}${s.episodes ? ` (${s.episodes} eps)` : ""}</option>`).join("")}
          </select></label>
        <label>Episode
          <select id="title-episode" disabled><option value="">${current.season ? "Loading…" : "(pick a season first)"}</option></select></label>
      </div>` : "";
    dialogBody.innerHTML = `
      <div class="dialog-head">
        ${t.poster ? `<img class="dialog-poster" src="${escapeHtml(t.poster)}" alt="">` : ""}
        <div class="dialog-info">
          <h2>${escapeHtml(t.title || "")} ${t.year ? `<span class="job-year">(${t.year})</span>` : ""}</h2>
          <div class="hint">${t.kind === "movie" ? "Movie" : "Show"}</div>
          <p>${escapeHtml(t.description || "")}</p>
          ${seasons}
        </div>
      </div>
      <h4>Where to get it</h4>
      <div id="title-offers"></div>
      <p id="title-message" class="message" aria-live="polite"></p>`;
    renderOffers();
  }

  // How you get a title: badge colour and text for each kind of access.
  const ACCESS = {
    included: ["included", "\u2713 Included in subscription"],
    channel: ["channel", "Extra channel subscription"],
    free: ["free", "Free"],
    ads: ["free", "Free with ads"],
    rent: ["paid", "Rent"],
    buy: ["paid", "Buy"],
    disc: ["disc", "\u{1F4BF} Physical disc (shop)"],
  };
  const DISCS = { DVD: "DVD", BLURAY: "Blu-ray", BLURAY_4K: "4K Blu-ray" };
  const FILTERS = {
    included: { access: ["included", "free", "ads"], none: "Not in a subscription or free on your services." },
    paid: { access: ["rent", "buy"], none: "Not for rent or sale on your services." },
  };

  function accessBadges(access) {
    const both = access.includes("rent") && access.includes("buy");
    return access.filter((a) => ACCESS[a] && !(both && a === "buy")).map((a) => {
      const [color, label] = ACCESS[a];
      return `<span class="access access-${color}">${both && a === "rent" ? "Rent / Buy" : label}</span>`;
    }).join("");
  }

  function renderOffers() {
    const t = current.data;
    const box = $("title-offers");
    if (!t.offers.length) {
      box.innerHTML = `<p class="hint">None of your services offers this in the chosen country.</p>`;
      return;
    }
    const experimental = !!root.dataset.experimental;
    // The price filter of the list applies here too; the rest is one click away.
    // Discs are a category of their own.
    const filter = FILTERS[state.access];
    const isDisc = (o) => o.access.includes("disc");
    const fits = (o) => !filter || isDisc(o) || o.access.some((a) => filter.access.includes(a));
    const digital = t.offers.filter((o) => !isDisc(o));
    const some = !filter || digital.some(fits);
    const hidden = some && !current.allOffers ? t.offers.filter((o) => !fits(o)).length : 0;
    const note = !digital.length ? `<p class="hint">None of your services has this to stream, rent or buy here.</p>`
      : some ? "" : `<p class="hint">${filter.none} These are the other offers:</p>`;
    box.innerHTML = note + t.offers.map((o, i) => {
      if (hidden && !fits(o)) return "";
      const disc = isDisc(o);
      const quality = disc ? o.quality.map((q) => DISCS[q] || q).join(", ")
        : o.quality.includes("4K") ? "4K" : o.quality.includes("HD") ? "HD" : o.quality[0] || "";
      const tags = [
        quality,
        o.video.includes("DOLBY_VISION") ? "Dolby Vision" : "",
        o.video.some((v) => v.startsWith("HDR10")) ? "HDR10" : "",
      ].filter(Boolean).map((x) => `<span class="offer-tag">${escapeHtml(x)}</span>`).join("");
      const languages = [o.audio.length ? `Audio: ${o.audio.join(", ")}` : "",
        o.subtitles.length ? `Subtitles: ${o.subtitles.join(", ")}` : ""].filter(Boolean);
      const p = providerOf(o.service);
      const off = o.usable ? "" : " disabled";
      return `
        <div class="offer offer-${(ACCESS[o.access[0]] || ["other"])[0]}">
          ${p && p.icon ? `<img class="offer-icon" src="${escapeHtml(p.icon)}" alt="">` : `<span class="offer-icon"></span>`}
          <div class="offer-main">
            <div><strong>${escapeHtml(o.providers.join(", ") || o.service)}</strong> ${tags}</div>
            <div class="offer-access">${accessBadges(o.access)}</div>
            ${disc ? "" : `<div class="offer-url">${/^https?:/.test(o.url) ? "" : "ID: "}${escapeHtml(o.url)}</div>`}
            ${o.note ? `<div class="hint${o.usable ? "" : " warn"}">${escapeHtml(o.note)}</div>` : ""}
            ${languages.length ? `<details class="offer-langs"><summary>Languages</summary>${languages.map((l) => `<div class="hint">${escapeHtml(l)}</div>`).join("")}</details>` : ""}
          </div>
          <div class="offer-actions">
            ${disc ? "" : `
            ${experimental ? `<button type="button" class="primary" data-act="check" data-offer="${i}"${off}>Check availability</button>` : ""}
            <button type="button"${experimental ? "" : ' class="primary"'} data-act="setup" data-offer="${i}"${off}>Set up download</button>
            <button type="button" data-act="queue" data-offer="${i}"${off}>Add to queue</button>`}
            <a class="button-link" href="${escapeHtml(o.web_url)}" target="_blank" rel="noopener noreferrer">Open &#8599;</a>
          </div>
        </div>`;
    }).join("") + (hidden ? `<button type="button" data-act="all-offers">Show ${hidden} other offer${hidden > 1 ? "s" : ""}</button>` : "");
  }

  function loadEpisodes() {
    const season = current.data.seasons.find((s) => s.number === current.season);
    const select = $("title-episode");
    if (!season || !select) return;
    const want = `${current.id}:${season.number}`;
    api(`/api/discover/season/${encodeURIComponent(season.id)}`, {}).then(({ episodes }) => {
      if (!current || `${current.id}:${current.season}` !== want) return;
      select.innerHTML = `<option value="">All episodes</option>` + episodes.map((e) =>
        `<option value="${e.number}">E${pad(e.number)}${e.title ? ` · ${escapeHtml(e.title)}` : ""}</option>`).join("");
      select.disabled = false;
    }).catch(() => {
      if (!current || `${current.id}:${current.season}` !== want) return;
      // No episode list: still let any number the season has be picked.
      select.innerHTML = `<option value="">All episodes</option>` + Array.from({ length: season.episodes || 0 },
        (_, i) => `<option value="${i + 1}">E${pad(i + 1)}</option>`).join("");
      select.disabled = false;
    });
  }

  // The link / ID, -w value and details to hand to the downloader for one offer.
  async function target(offer) {
    const t = current.data;
    const wanted = current.season ? `S${pad(current.season)}${current.episode ? `E${pad(current.episode)}` : ""}` : "";
    const season = t.seasons.find((s) => s.number === current.season);
    let url = offer.url;
    if (offer.resolve) {
      // An episode link: ask for the show's link, which covers every season.
      const r = await fetch("/api/discover/resolve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.error || "Couldn't look up the show's link.");
      url = body.url;
    } else if (season && season.links[offer.service]) {
      url = season.links[offer.service];
    }
    return { service: offer.service, url, wanted, name: t.title, year: t.year, poster: t.poster };
  }

  function act(button) {
    const offer = current.data.offers[Number(button.dataset.offer)];
    const act = button.dataset.act;
    const msg = $("title-message");
    const wholeShow = current.data.kind === "show" && !current.season && (offer.exact || offer.resolve);
    if (act === "queue" && wholeShow &&
        !confirm("No season picked: this queues ALL episodes of the show. Continue?")) return;
    button.disabled = true;
    msg.textContent = offer.resolve ? "Looking up the show's link…" : "";
    target(offer).then((x) => {
      if (act === "queue") return queue(x).then((job) => {
        msg.textContent = `Added to the queue (job ${job.id}). Start it on the Queue tab.`;
      });
      const params = new URLSearchParams({ service: x.service, title: x.url, wanted: x.wanted, name: x.name || "" });
      if (x.year) params.set("year", x.year);
      if (x.poster) params.set("poster", x.poster);
      if (act === "check") params.set("check", "1");
      window.location.href = `/download?${params}`;
      return null;
    }).catch((e) => { msg.textContent = e.message; }).finally(() => { button.disabled = false; });
  }

  function queue(x) {
    const data = {
      mode: "queue", service: x.service, title: x.url,
      meta: { name: x.name, year: x.year, poster: x.poster },
    };
    if (x.wanted) data.wanted = x.wanted;
    return fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    }).then(async (r) => {
      const body = await r.json();
      if (!r.ok) throw new Error(body.error || "Couldn't add it to the queue.");
      return body;
    });
  }

  /* ---- events ---- */

  results.addEventListener("click", (e) => {
    const card = e.target.closest(".title-card");
    if (card) openTitle(card.dataset.id, card.dataset.season);
  });
  dialogBody.addEventListener("click", (e) => {
    const button = e.target.closest("[data-act]");
    if (!button || !current || !current.data) return;
    if (button.dataset.act === "all-offers") {
      current.allOffers = true;
      renderOffers();
    } else act(button);
  });
  dialogBody.addEventListener("change", (e) => {
    if (e.target.id === "title-season") {
      current.season = e.target.value ? Number(e.target.value) : null;
      current.episode = null;
      const episode = $("title-episode");
      episode.disabled = true;
      episode.innerHTML = `<option value="">${current.season ? "Loading…" : "(pick a season first)"}</option>`;
      if (current.season) loadEpisodes();
    } else if (e.target.id === "title-episode") {
      current.episode = e.target.value ? Number(e.target.value) : null;
    }
  });
  $("title-dialog-close").addEventListener("click", () => dialog.close());
  // A click on the backdrop (the dialog element itself) closes it too.
  dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); });
  dialog.addEventListener("close", () => { current = null; });

  root.querySelectorAll("[data-mode]").forEach((b) => b.addEventListener("click", () => {
    state.mode = b.dataset.mode;
    state.query = "";
    $("discover-search").value = "";
    reload();
  }));
  root.querySelectorAll("[data-kind]").forEach((b) => b.addEventListener("click", () => {
    state.kind = b.dataset.kind;
    reload();
  }));
  $("discover-access").addEventListener("change", (e) => {
    state.access = e.target.value;
    reload();
  });
  $("discover-sort").addEventListener("change", (e) => {
    state.sort = e.target.value;
    reload();
  });
  $("discover-providers").addEventListener("click", (e) => {
    const button = e.target.closest("[data-service]");
    if (!button) return;
    const service = button.dataset.service;
    if (!service) state.services.clear();
    else if (state.services.has(service)) state.services.delete(service);
    else state.services.add(service);
    renderProviders();
    reload();
  });
  let searchTimer = null;
  $("discover-search").addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      const query = e.target.value.trim();
      if (query === state.query) return;
      state.query = query;
      reload();
    }, 450);
  });
  moreButton.addEventListener("click", () => load(next));

  function start() {
    syncSwitches();
    results.innerHTML = "";
    message.textContent = "Loading…";
    loadProviders().then(() => load()).catch((e) => { message.textContent = e.message; });
  }
  ["discover-country", "discover-language"].forEach((id) => $(id).addEventListener("change", () => {
    saveUiSetting(id.replace("-", "_"), $(id).value);
    listRequest++;
    start();
  }));
  start();
})();
