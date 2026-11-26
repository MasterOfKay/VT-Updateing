/* The Settings tab: its sections, the folder picker, and the editor for vinetrimmer.yml. */

(() => {
  const el = (id) => document.getElementById(id);
  const KEEP = "__keep__"; // a value this page can't edit (set per profile in the file)

  // h("label", {class: "field"}, "text", node) - text is never read as HTML.
  function h(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    Object.entries(attrs).forEach(([key, value]) => {
      if (value === false || value === null || value === undefined) return;
      if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else if (key === "value") node.value = value;
      else if (key === "checked") node.checked = !!value;
      else node.setAttribute(key, value === true ? "" : value);
    });
    node.append(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
    return node;
  }

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

  let config = null; // /api/config

  const fieldset = (title, ...children) => h("fieldset", {}, h("legend", {}, title), ...children);
  const hint = (...text) => h("p", { class: "hint" }, ...text);
  const field = (label, input, extra = {}) =>
    h("label", { class: `field ${extra.class || ""}` }, h("span", {}, label), input,
      extra.hint ? h("span", { class: "hint" }, extra.hint) : null);
  const flag = (label, input) => h("label", { class: "field field-flag" }, input, h("span", {}, label));
  const checkbox = (value) => h("input", { type: "checkbox", checked: value });
  const text = (value, attrs = {}) =>
    h("input", { type: "text", autocomplete: "off", spellcheck: "false", value: value ?? "", ...attrs });
  const number = (value, attrs = {}) => h("input", { type: "number", value: value ?? "", ...attrs });

  function select(options, value) {
    const list = options.map((o) => (typeof o === "object" ? o : { value: o, label: o }));
    const current = value === null || value === undefined ? "" : String(value);
    if (current && !list.some((o) => String(o.value) === current)) {
      list.push({ value: current, label: `${current} (not found)` });
    }
    const node = h("select", {}, list.map((o) => h("option", { value: o.value }, o.label)));
    node.value = current;
    return node;
  }

  const removeButton = (row, onRemove) => h("button", {
    type: "button", class: "quiet",
    onclick: () => {
      row.remove();
      if (onRemove) onRemove();
    },
  }, "Remove");

  /* ---------------- one value for all services, plus exceptions ---------------- */

  // make(value) builds an input; read(input) returns what to save.
  function perService({ label, help, data, make, read = (input) => input.value, start = "" }) {
    const editable = (value) => {
      if (value === KEEP) {
        const note = h("input", { type: "text", value: "set per profile in vinetrimmer.yml", disabled: true });
        return { node: note, get: () => KEEP };
      }
      const input = make(value);
      return { node: input, get: () => read(input) };
    };
    const main = editable(data.default ?? start);
    main.node.setAttribute("aria-label", label);
    const rows = h("div", { class: "rows" });
    const readers = new Map();

    const addRow = (service, value) => {
      const name = select([{ value: "", label: "Choose a service…" },
        ...config.choices.services.map((s) => ({ value: s, label: s }))], service);
      name.setAttribute("aria-label", `Service with its own ${label.toLowerCase()}`);
      const control = editable(value);
      control.node.setAttribute("aria-label", `${label} for this service`);
      const row = h("div", { class: "row override-row" }, name, control.node);
      row.append(removeButton(row, () => readers.delete(row)));
      readers.set(row, () => [name.value, control.get()]);
      rows.append(row);
    };
    Object.entries(data.per || {}).forEach(([service, value]) => addRow(service, value));

    const node = h("div", { class: "per-service" },
      h("div", { class: "per-default" }, h("span", { class: "per-label" }, label), main.node),
      help ? hint(help) : null,
      rows,
      h("button", { type: "button", class: "small", onclick: () => addRow("", data.default === KEEP ? start : data.default ?? start) },
        "Different for one service"));
    return {
      node,
      get() {
        const per = {};
        [...rows.children].forEach((row) => {
          const [service, value] = readers.get(row)();
          if (!service) return;
          if (service in per) throw new Error(`${label}: ${service} is listed twice.`);
          per[service] = value;
        });
        return { default: main.get(), per };
      },
    };
  }

  /* ---------------- sections ---------------- */

  const SECTIONS = {};

  SECTIONS.downloader = (data, choices) => {
    const downloader = perService({
      label: "Downloader", data: data.downloader, make: (v) => select(choices.downloaders, v), start: "aria2c",
      help: "The program that fetches the files. Some services only work with one of them, so keep their own entry.",
    });
    const tracks = perService({
      label: "Tracks at the same time", data: data.concurrent_tracks, start: 1,
      make: (v) => number(v, { min: 1, max: 32 }),
    });
    const subs = perService({
      label: "Subtitles at the same time", data: data.concurrent_subtitle_downloads, start: 0,
      make: (v) => number(v, { min: 0, max: 64 }),
      help: "Extra slots only for subtitles. With 0 they share the slots above.",
    });
    const progress = perService({
      label: "Live progress bars in a terminal", data: data.progress_display, start: true,
      make: (v) => select([{ value: "true", label: "On" }, { value: "false", label: "Off" }], String(v)),
      read: (input) => input.value === "true",
      help: "Only matters when you run vinetrimmer yourself in a terminal. The Console tab always shows progress.",
    });
    const crop = perService({
      label: "Crop black bars", data: data.crop, start: "none",
      make: (v) => text(v, { placeholder: "none", list: "crop-values" }),
      help: "none, auto (detected per title with ffmpeg), or four pixel counts: left,top,right,bottom. " +
        "The video isn't re-encoded.",
    });

    const allowed = choices.aria2c_options;
    const ariaRows = h("div", { class: "rows" });
    const addAria = (option, value) => {
      if (option && !allowed.includes(option)) {
        ariaRows.append(h("div", { class: "row aria-row" }, h("code", {}, option),
          h("span", { class: "hint" }, "set in vinetrimmer.yml, left as it is")));
        return;
      }
      const name = select([{ value: "", label: "Choose an option…" }, ...allowed], option);
      name.setAttribute("aria-label", "aria2c option");
      const input = text(value === undefined ? "" : String(value), { "aria-label": "Value" });
      const row = h("div", { class: "row aria-row", "data-edit": true }, name, input);
      row.append(removeButton(row));
      ariaRows.append(row);
    };
    Object.entries(data.aria2c).forEach(([option, value]) => addAria(option, value));

    return {
      nodes: [
        h("datalist", { id: "crop-values" }, h("option", { value: "none" }), h("option", { value: "auto" })),
        fieldset("Downloader", downloader.node),
        fieldset("aria2c options",
          hint("Fine-tuning for downloads done by aria2c, for example max-connection-per-server."),
          ariaRows,
          h("button", { type: "button", class: "small", onclick: () => addAria("", "") }, "Add an option")),
      ],
      side: [
        fieldset("Speed and display", h("div", { class: "per-grid" }, tracks.node, subs.node, crop.node, progress.node)),
      ],
      get() {
        const aria2c = {};
        ariaRows.querySelectorAll("[data-edit]").forEach((row) => {
          const option = row.querySelector("select").value;
          if (option) aria2c[option] = row.querySelector("input").value.trim();
        });
        return {
          downloader: downloader.get(), concurrent_tracks: tracks.get(),
          concurrent_subtitle_downloads: subs.get(), progress_display: progress.get(), crop: crop.get(), aria2c,
        };
      },
    };
  };

  // A list of cards (remote CDMs, key vaults); every card remembers which entry it was.
  function cardList(entries, build, addLabel) {
    const list = h("div", { class: "cards" });
    const readers = new Map();
    const add = (entry) => {
      const card = h("div", { class: "card" });
      const reader = build(entry, card);
      readers.set(card, () => ({ ...(entry.from === undefined ? {} : { from: entry.from }), ...reader() }));
      list.append(card);
      return card;
    };
    entries.forEach(add);
    return {
      node: h("div", {}, list, h("button", {
        type: "button", class: "small", onclick: () => add({}).querySelector("input").focus(),
      }, addLabel)),
      get: () => [...list.children].map((card) => readers.get(card)()),
    };
  }

  const secretInput = (saved) =>
    h("input", { type: "password", autocomplete: "new-password", placeholder: saved ? "saved (empty keeps it)" : "" });

  SECTIONS.devices = (data, choices) => {
    const deviceOptions = [{ value: "", label: "Choose a device…" }, ...choices.devices.map((d) =>
      ({ value: d.name, label: `${d.name} (${d.type}, ${d.source})` }))];
    const cdm = perService({
      label: "Default device", data: data.cdm, make: (v) => select(deviceOptions, v),
      help: "Widevine and PlayReady devices found in the devices folder, and your remote CDMs. A service without " +
        "its own entry uses the default.",
    });
    const remote = cardList(data.cdm_api, (entry, card) => {
      const inputs = {
        name: text(entry.name), type: select(choices.remote_cdm_types, entry.type || "ANDROID"),
        device: text(entry.device), system_id: number(entry.system_id), security_level: number(entry.security_level),
        host: text(entry.host, { placeholder: "https://…" }), challenge_path: text(entry.challenge_path),
        license_path: text(entry.license_path), key: secretInput(entry.key_saved),
      };
      const labels = {
        name: "Name", type: "Type", device: "Device name on the server", system_id: "System ID",
        security_level: "Security level", host: "Server address", challenge_path: "Challenge path",
        license_path: "License path", key: "API key",
      };
      card.append(h("div", { class: "card-grid" }, Object.entries(inputs).map(([k, input]) => field(labels[k], input))),
        removeButton(card));
      return () => Object.fromEntries(Object.entries(inputs).map(([k, input]) => [k, input.value.trim()]));
    }, "Add a remote CDM");

    const found = choices.devices.length
      ? h("ul", { class: "device-list" }, choices.devices.map((d) =>
        h("li", {}, h("strong", {}, d.name), h("span", { class: `tag tag-${d.type.toLowerCase()}` }, d.type),
          h("span", { class: "hint" }, d.source))))
      : hint("No devices found. Put .wvd or .prd files, or device folders, into vinetrimmer/devices.");

    return {
      nodes: [
        fieldset("Which device each service uses", cdm.node),
        fieldset("Remote CDMs", hint("A CDM that runs on another server. Save first, then it can be chosen above."),
          remote.node),
      ],
      side: [
        fieldset("Devices found", found,
          h("button", { type: "button", class: "small", onclick: () => reload("devices") }, "Look again")),
      ],
      get: () => ({ cdm: cdm.get(), cdm_api: remote.get() }),
    };
  };

  const VAULT_TYPES = {
    local: "File on this computer (SQLite)", remote: "MySQL server", http: "HTTP vault", httpapi: "HTTP API vault",
  };
  const VAULT_LABELS = {
    path: "Database file", host: "Server address", port: "Port", database: "Database", username: "Username",
    password: "Password", key: "API key",
  };

  SECTIONS.vaults = (data, choices) => {
    const vaults = cardList(data.key_vaults, (entry, card) => {
      const name = text(entry.name);
      const type = select(Object.keys(choices.vault_fields).map((t) => ({ value: t, label: VAULT_TYPES[t] || t })),
        entry.type || "local");
      const fields = h("div", { class: "card-grid" });
      let inputs = {};
      const draw = () => {
        const same = type.value === entry.type;
        inputs = {};
        fields.replaceChildren(...(choices.vault_fields[type.value] || []).map((key) => {
          const secret = key === "password" || key === "key";
          const input = secret ? secretInput(same && entry[`${key}_saved`])
            : key === "port" ? number(same ? entry[key] : "") : text(same ? entry[key] : "");
          inputs[key] = input;
          return field(VAULT_LABELS[key] || key, input, key === "path" ? { class: "full" } : {});
        }));
      };
      type.addEventListener("change", draw);
      draw();
      card.append(h("div", { class: "card-grid" }, field("Name", name), field("Type", type)), fields,
        h("div", { class: "card-actions" },
          h("button", {
            type: "button", class: "quiet",
            title: "Vaults are asked from top to bottom",
            onclick: () => { if (card.previousElementSibling) card.previousElementSibling.before(card); markDirty("vaults"); },
          }, "Move up"),
          h("button", {
            type: "button", class: "quiet",
            onclick: () => {
              if (confirm(`Remove the vault "${name.value || "without a name"}" from the settings? ` +
                "The vault itself and its keys aren't deleted.")) {
                card.remove();
                markDirty("vaults");
              }
            },
          }, "Remove")));
      return () => ({
        name: name.value.trim(), type: type.value,
        ...Object.fromEntries(Object.entries(inputs).map(([k, input]) =>
          [k, input.type === "password" ? input.value : input.value.trim()])),
      });
    }, "Add a vault");

    const results = h("ul", { class: "vault-results", "aria-live": "polite" });
    const check = h("button", {
      type: "button",
      onclick: () => {
        check.disabled = true;
        results.replaceChildren(h("li", {}, "Opening the vaults…"));
        request("/api/vaults/check", { method: "POST" })
          .then((list) => results.replaceChildren(...list.map((v) =>
            h("li", { class: v.ok ? "ok" : "err" }, h("strong", {}, v.name), ` ${v.message}`))))
          .catch((e) => results.replaceChildren(h("li", { class: "err" }, e.message)))
          .finally(() => { check.disabled = false; });
      },
    }, "Check the saved vaults");

    return {
      nodes: [fieldset("Key vaults",
        hint("Where content keys are looked up and stored. Vaults are asked from top to bottom."), vaults.node)],
      side: [fieldset("Do they open?", check, results)],
      get: () => ({ key_vaults: vaults.get() }),
    };
  };

  const SAMPLE = {
    title: "Silo", season_episode: "S02E03", episode_name: "Solo", quality: "2160p", source: "ATVP",
    audio: "DD+5.1", video: "H.265", tag: "TAG",
  };

  SECTIONS.output = (data, choices) => {
    const pattern = (kind) => {
      const input = text(data[kind], { class: "mono" });
      const preview = h("span", { class: "hint" });
      const show = () => {
        const sample = kind === "movies" ? { ...SAMPLE, title: "Dune", year: "2021" } : SAMPLE;
        preview.textContent = "Looks like: " + input.value.replace(/\{(\w+)\}/g, (m, k) => sample[k] ?? m);
      };
      input.addEventListener("input", show);
      show();
      const chips = h("span", { class: "avail-chips" }, choices.template_fields[kind].map((name) => h("button", {
        type: "button", class: "chip",
        onclick: () => {
          input.setRangeText(`{${name}}`, input.selectionStart ?? input.value.length,
            input.selectionEnd ?? input.value.length, "end");
          input.focus();
          show();
          markDirty("output");
        },
      }, `{${name}}`)));
      return {
        input,
        node: h("div", { class: "field" }, h("span", {}, kind === "movies" ? "Movies" : "Episodes"), input, chips, preview),
      };
    };
    const movies = pattern("movies");
    const series = pattern("series");
    movies.input.setAttribute("aria-label", "File name pattern for movies");
    series.input.setAttribute("aria-label", "File name pattern for episodes");
    const lastAudio = checkbox(data.use_last_audio);
    const format = perService({
      label: "Subtitle format", data: data.subtitle_format, start: "srt",
      make: (v) => select(choices.subtitle_formats, String(v).toLowerCase()),
    });
    const attach = checkbox(data.attach_fonts);
    const download = checkbox(data.download_fonts);

    const style = data.subtitle_style;
    const font = text(style.default.font, { placeholder: "as the service sends it" });
    const size = number(style.default.size, { min: 1, max: 200 });
    const styleRows = h("div", { class: "rows" });
    const addStyle = (service, values = {}) => {
      const name = select([{ value: "", label: "Choose a service…" }, ...choices.services], service);
      name.setAttribute("aria-label", "Service with its own subtitle style");
      const row = h("div", { class: "row style-row" }, name,
        text(values.font, { placeholder: "Font", "aria-label": "Font", "data-f": "font" }),
        number(values.size, { placeholder: "Size", "aria-label": "Size", "data-f": "size", min: 1, max: 200 }));
      row.append(removeButton(row));
      styleRows.append(row);
    };
    Object.entries(style.per).forEach(([service, values]) => addStyle(service, values));

    return {
      nodes: [
        fieldset("File names", movies.node, series.node,
          flag("Name the audio after the last audio track instead of the first", lastAudio)),
      ],
      side: [
        fieldset("Subtitles", format.node,
          h("div", { class: "check-fields" },
            field("Font for ASS subtitles", font), field("Font size", size, { class: "narrow" })),
          styleRows,
          h("button", { type: "button", class: "small", onclick: () => addStyle("") }, "Different style for one service"),
          flag("Attach the fonts to the video file", attach),
          flag("Download fonts that are missing", download)),
      ],
      get() {
        const per = {};
        [...styleRows.children].forEach((row) => {
          const service = row.querySelector("select").value;
          if (!service) return;
          per[service] = {
            font: row.querySelector('[data-f="font"]').value.trim(),
            size: row.querySelector('[data-f="size"]').value,
          };
        });
        return {
          movies: movies.input.value, series: series.input.value, use_last_audio: lastAudio.checked,
          subtitle_format: format.get(), attach_fonts: attach.checked, download_fonts: download.checked,
          subtitle_style: { default: { font: font.value.trim(), size: size.value }, per },
        };
      },
    };
  };

  SECTIONS.general = (data, choices) => {
    const tag = text(data.tag);
    const tagSd = text(data.tag_sd);
    const logs = checkbox(data.generate_logs);
    const decrypter = select(choices.decrypters, data.decrypter);
    return {
      nodes: [fieldset("General",
        h("div", { class: "check-fields" },
          field("Group tag added to file names", tag),
          field("Group tag for SD releases", tagSd),
          field("Program that decrypts", decrypter)),
        flag("Write a log file for every download", logs))],
      get: () => ({ tag: tag.value, tag_sd: tagSd.value, generate_logs: logs.checked, decrypter: decrypter.value }),
    };
  };

  // What an option does, with its command line flag small behind it.
  const flagLabel = (help, flags) => h("span", {},
    help ? `${help.charAt(0).toUpperCase()}${help.slice(1)} ` : "", h("code", { class: "opt-flag" }, flags));

  // One command line option as a form field; read() gives "" / false for "not set".
  function optionField(option, label, value) {
    if (option.kind === "flag") {
      const input = checkbox(value === true || value === "true");
      return { node: flag(label, input), read: () => input.checked };
    }
    let input;
    if (option.kind === "choice") {
      const current = (option.choices || []).find((c) => String(c).toLowerCase() === String(value ?? "").toLowerCase());
      input = select([{ value: "", label: option.default ? `not set (${option.default})` : "not set" },
        ...(option.choices || []).map((c) => ({ value: c, label: c }))], current ?? value);
    } else if (option.kind === "int") {
      input = number(value);
    } else {
      input = text(Array.isArray(value) ? value.join(",") : value,
        { placeholder: option.placeholder || "not set" });
    }
    return { node: field(label, input), read: () => input.value.trim() };
  }

  // Options that describe one download, so they make no sense as a default.
  const NOT_A_DEFAULT = ["wanted", "keys", "list_", "selected", "audio_pick", "first_title"];

  SECTIONS.defaults = (data, choices) => {
    const readers = {};
    const grids = { primary: h("div", { class: "option-grid" }), advanced: h("div", { class: "option-grid" }) };
    choices.global_options.filter((o) => !NOT_A_DEFAULT.includes(o.key)).forEach((option) => {
      const made = optionField(option, flagLabel(option.help, option.flag), data.global[option.key]);
      readers[option.key] = made.read;
      grids[option.section].append(made.node);
    });

    const cards = h("div", { class: "cards" });
    const services = new Map(); // service -> () => values
    const picker = select([{ value: "", label: "Choose a service…" }, ...choices.services], "");
    picker.setAttribute("aria-label", "Service to set defaults for");

    const addService = (service, values) => {
      if (services.has(service)) return;
      const body = h("div", { class: "option-grid" }, h("p", { class: "hint" }, "Loading its options…"));
      const card = h("div", { class: "card" }, h("h3", {}, service), body);
      card.append(removeButton(card, () => services.delete(service)));
      cards.append(card);
      services.set(service, () => values); // until the options are loaded: as it was
      request(`/api/service-options/${encodeURIComponent(service)}`).then((options) => {
        const made = options.map((o) =>
          [o.name, optionField(o, flagLabel(o.help, o.opts.join(" ")), values[o.name])]);
        body.replaceChildren(...(made.length ? made.map(([, m]) => m.node)
          : [hint("This service has no options of its own.")]));
        services.set(service, () => ({ ...values, ...Object.fromEntries(made.map(([name, m]) => [name, m.read()])) }));
      }).catch(() => body.replaceChildren(hint("Its options can't be read. What's set stays as it is.")));
    };
    Object.entries(data.services).forEach(([service, values]) => addService(service, values));

    return {
      nodes: [
        fieldset("Defaults for every download",
          hint("Used on the command line when you leave an option out, and filled in on the Download page."),
          grids.primary,
          h("details", {}, h("summary", {}, "More options"), grids.advanced)),
      ],
      side: [
        fieldset("Defaults for one service",
          cards,
          h("div", { class: "inline" }, picker, h("button", {
            type: "button",
            onclick: () => { if (picker.value) { addService(picker.value, {}); picker.value = ""; markDirty("defaults"); } },
          }, "Add"))),
      ],
      get() {
        const global = Object.fromEntries(NOT_A_DEFAULT.filter((k) => k in data.global).map((k) => [k, data.global[k]]));
        Object.entries(readers).forEach(([key, read]) => (global[key] = read()));
        return { global, services: Object.fromEntries([...services].map(([name, read]) => [name, read()])) };
      },
    };
  };

  SECTIONS.profiles = (data) => {
    const profiles = perService({
      label: "Profile", data: data.profiles, start: "default",
      make: (v) => text(v === false ? "none" : v),
      read: (input) => (input.value.trim().toLowerCase() === "none" ? false : input.value.trim()),
      help: "A profile picks which login and cookie file a service uses (cookies/<Service>/<profile>.txt). " +
        "Write none to use no profile.",
    });
    return {
      nodes: [fieldset("Profiles", profiles.node)],
      get: () => ({ profiles: profiles.get() }),
    };
  };

  SECTIONS.network = (data) => {
    const rows = h("div", { class: "rows" });
    const add = (name, value) => {
      const row = h("div", { class: "row header-row" },
        text(name, { placeholder: "Header", "aria-label": "Header name" }),
        text(value, { placeholder: "Value", "aria-label": "Header value" }));
      row.append(removeButton(row));
      rows.append(row);
    };
    Object.entries(data.headers).forEach(([name, value]) => add(name, value));
    return {
      nodes: [fieldset("Request headers",
        hint("Sent with every request vinetrimmer makes to a service, for example User-Agent."),
        rows, h("button", { type: "button", class: "small", onclick: () => add("", "") }, "Add a header"))],
      get() {
        const headers = {};
        [...rows.children].forEach((row) => {
          const [name, value] = [...row.querySelectorAll("input")].map((i) => i.value.trim());
          if (name) headers[name] = value;
        });
        return { headers };
      },
    };
  };

  /* ---------------- drawing and saving ---------------- */

  const dirty = new Set();

  function markDirty(section) {
    dirty.add(section);
    const bar = document.querySelector(`[data-config="${section}"] .save-bar`);
    if (!bar) return;
    bar.classList.add("unsaved");
    say(bar.querySelector(".message"), "Not saved yet.");
  }

  function draw(section, message = "", kind = "") {
    const box = document.querySelector(`[data-config="${section}"]`);
    if (!box) return;
    const form = SECTIONS[section](config.sections[section], config.choices);
    const status = h("p", { class: "message", "aria-live": "polite" });
    say(status, message, kind);
    const save = h("button", { type: "button", class: "primary" }, `Save ${SAVE_NAMES[section]}`);
    save.addEventListener("click", () => {
      let data;
      try {
        data = form.get();
      } catch (e) {
        say(status, e.message, "err");
        return;
      }
      save.disabled = true;
      say(status, "");
      postJson(`/api/config/${section}`, data)
        .then((fresh) => {
          config = fresh;
          dirty.delete(section);
          draw(section, "Saved. The next download uses it.", "ok");
        })
        .catch((e) => {
          say(status, e.message, "err");
          save.disabled = false;
        });
    });
    const columns = h("div", { class: "cols" }, h("div", {}, form.nodes), form.side ? h("div", {}, form.side) : null);
    box.replaceChildren(columns, h("div", { class: "save-bar" }, save, status));
  }

  const SAVE_NAMES = {
    downloader: "downloader settings", devices: "device settings", vaults: "vaults", output: "file settings",
    general: "general settings", defaults: "defaults", profiles: "profiles", network: "headers",
  };

  // Read the file again and redraw one section, e.g. after adding a device.
  function reload(section) {
    if (dirty.has(section) && !confirm("Looking again drops the changes you haven't saved here. Continue?")) return;
    request("/api/config").then((fresh) => {
      config = fresh;
      dirty.delete(section);
      draw(section);
    });
  }

  function watchChanges() {
    document.querySelectorAll("[data-config]").forEach((box) => {
      const changed = (e) => {
        if (e.target.closest(".save-bar")) return;
        // Choosing a service in an "add" picker changes nothing yet.
        if (e.target.closest(".inline")) return;
        markDirty(box.dataset.config);
      };
      box.addEventListener("input", changed);
      box.addEventListener("change", changed);
      box.addEventListener("click", (e) => {
        // A "Remove" button that took its row with it.
        if (e.target.closest("button.quiet") && !e.target.isConnected) markDirty(box.dataset.config);
      });
    });
    window.addEventListener("beforeunload", (e) => {
      if (dirty.size) e.preventDefault();
    });
  }

  /* ---------------- which section is shown ---------------- */

  function showPage() {
    const pages = [...document.querySelectorAll("[data-page]")];
    const wanted = location.hash.slice(1);
    const page = pages.some((p) => p.dataset.page === wanted) ? wanted : "storage";
    pages.forEach((p) => (p.hidden = p.dataset.page !== page));
    document.querySelectorAll(".settings-nav a").forEach((a) => {
      const active = a.getAttribute("href") === `#${page}`;
      a.classList.toggle("active", active);
      if (active) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
    // on a phone the sections are one row to swipe through: keep the open one in view
    const nav = document.querySelector(".settings-nav");
    const open = nav.querySelector("a.active");
    if (open) nav.scrollLeft = open.offsetLeft - (nav.clientWidth - open.offsetWidth) / 2;
    window.scrollTo(0, 0);
  }

  /* ---------------- folder picker ---------------- */

  let target = null; // the input the picker fills
  let current = null; // the listing on screen

  function openFolder(path) {
    say(el("folder-message"), "");
    return request(`/api/folders${path ? `?path=${encodeURIComponent(path)}` : ""}`)
      .then((listing) => {
        current = listing;
        el("folder-current").textContent = listing.path;
        el("folder-up").disabled = !listing.parent;
        el("folder-note").textContent = listing.docker
          ? "These are the folders the container can see: only what's mounted into it. Files saved anywhere else " +
            "are lost when the container is recreated."
          : "Folders on the computer the web UI runs on.";
        el("folder-roots").replaceChildren(...listing.roots.map((root) =>
          h("button", { type: "button", class: "chip", onclick: () => openFolder(root) }, root)));
        el("folder-list").replaceChildren(...(listing.folders.length
          ? listing.folders.map((f) =>
            h("button", { type: "button", class: "folder", onclick: () => openFolder(f.path) }, f.name))
          : [hint("No folders in here.")]));
        el("folder-list").scrollTop = 0;
        el("folder-choose").disabled = !listing.writable;
        say(el("folder-message"), listing.error ||
          (listing.writable ? "" : "Nothing can be written into this folder."), listing.error ? "err" : "");
      })
      .catch((e) => { say(el("folder-message"), e.message, "err"); });
  }

  function initFolderPicker() {
    const dialog = el("folder-dialog");
    document.querySelectorAll("[data-browse]").forEach((button) => button.addEventListener("click", () => {
      target = el(button.dataset.browse);
      el("folder-new").value = "";
      dialog.showModal();
      openFolder(target.value.trim() || el(`${target.id}-effective`).textContent);
    }));
    el("folder-up").addEventListener("click", () => openFolder(current.parent));
    el("folder-cancel").addEventListener("click", () => dialog.close());
    el("folder-choose").addEventListener("click", () => {
      target.value = current.path;
      dialog.close();
      say(el("storage-message"), "Chosen. Press Save folders to use it.");
    });
    el("folder-create").addEventListener("click", () => {
      postJson("/api/folders", { parent: current.path, name: el("folder-new").value })
        .then((listing) => {
          el("folder-new").value = "";
          openFolder(listing.path);
        })
        .catch((e) => { say(el("folder-message"), e.message, "err"); });
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    window.addEventListener("hashchange", showPage);
    showPage();
    initFolderPicker();
    request("/api/config")
      .then((data) => {
        config = data;
        Object.keys(SECTIONS).forEach((section) => draw(section));
        watchChanges();
      })
      .catch((e) => { el("settings-error").textContent = `The settings couldn't be loaded: ${e.message}`; });
  });
})();
