"use strict";

(() => {
  const { h, api, run, badge, field, dialog, channelSelect, channelName, toast, t, i18n } = Panel;
  const LANGS = { en: "English", es: "Español", pt: "Português" };
  const KINDS = { rules: "Rules", support: "Support us" };
  const BOT_AVATAR = "https://cdn.discordapp.com/embed/avatars/0.png";
  const BOT_NAME = "BulmaAI";

  const hex = (n) => (typeof n === "number" ? `#${n.toString(16).padStart(6, "0").toUpperCase()}` : "");

  // ---------- tiny caches for pill labels (roles/channels are cached by Panel.guild() already) ----

  const userCache = new Map();
  function userLabel(id) {
    if (userCache.has(id)) return Promise.resolve(userCache.get(id));
    return api(`/api/users/${id}`).then((d) => {
      const label = `@${d.user.display_name}`;
      userCache.set(id, label);
      return label;
    }).catch(() => `@${id}`);
  }

  function channelLabel(id) {
    const el = h("span", {}, `#${id}`);
    Panel.channelName(id).then((name) => { el.textContent = name; }).catch(() => {});
    return el;
  }

  function mentionPill(kind, id) {
    const el = h("span", { class: "mention" }, kind === "channel" ? `#${id}` : `@${id}`);
    const resolved = kind === "role" ? Panel.roleName(id) : kind === "channel" ? Panel.channelName(id) : userLabel(id);
    Promise.resolve(resolved).then((name) => { if (name) el.textContent = name; }).catch(() => {});
    return el;
  }

  // ---------- Discord markdown -> DOM (never innerHTML) -------------------------------------------

  const INLINE_PATTERNS = [
    { re: /^`([^`]+)`/, fn: (m) => h("code", {}, m[1]) },
    { re: /^\|\|([\s\S]+?)\|\|/, fn: (m) => h("span", { class: "d-spoiler", onclick: (e) => e.currentTarget.classList.toggle("revealed") }, parseInline(m[1])) },
    { re: /^\*\*([\s\S]+?)\*\*/, fn: (m) => h("strong", {}, parseInline(m[1])) },
    { re: /^__([\s\S]+?)__/, fn: (m) => h("u", {}, parseInline(m[1])) },
    { re: /^~~([\s\S]+?)~~/, fn: (m) => h("s", {}, parseInline(m[1])) },
    { re: /^\*([\s\S]+?)\*/, fn: (m) => h("em", {}, parseInline(m[1])) },
    { re: /^_([\s\S]+?)_/, fn: (m) => h("em", {}, parseInline(m[1])) },
    { re: /^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/, fn: (m) => h("a", { href: m[2], target: "_blank", rel: "noopener noreferrer" }, m[1]) },
    { re: /^<a?:(\w+):(\d+)>/, fn: (m) => h("img", { class: "d-emoji", src: `https://cdn.discordapp.com/emojis/${m[2]}.png`, alt: `:${m[1]}:`, title: `:${m[1]}:` }) },
    { re: /^<@!?(\d+)>/, fn: (m) => mentionPill("user", m[1]) },
    { re: /^<@&(\d+)>/, fn: (m) => mentionPill("role", m[1]) },
    { re: /^<#(\d+)>/, fn: (m) => mentionPill("channel", m[1]) },
    { re: /^https?:\/\/[^\s<]+/, fn: (m) => h("a", { href: m[0], target: "_blank", rel: "noopener noreferrer" }, m[0]) },
  ];

  function parseInline(text) {
    const nodes = [];
    let buffer = "";
    let rest = text;
    const flush = () => { if (buffer) { nodes.push(document.createTextNode(buffer)); buffer = ""; } };
    while (rest) {
      let hit = null;
      for (const p of INLINE_PATTERNS) {
        const m = p.re.exec(rest);
        if (m) { hit = { node: p.fn(m), length: m[0].length }; break; }
      }
      if (hit) { flush(); nodes.push(hit.node); rest = rest.slice(hit.length); }
      else { buffer += rest[0]; rest = rest.slice(1); }
    }
    flush();
    return nodes;
  }

  function isBlockStart(line) {
    return line.startsWith("```") || /^#{1,3}\s/.test(line) || line.startsWith("> ") || /^-\s/.test(line);
  }

  function joinLines(lines) {
    const nodes = [];
    lines.forEach((line, i) => {
      if (i > 0) nodes.push(h("br"));
      nodes.push(...parseInline(line));
    });
    return nodes;
  }

  function renderContent(text) {
    const container = h("div", { class: "d-content" });
    const lines = String(text).split("\n");
    let i = 0;
    while (i < lines.length) {
      if (lines[i].startsWith("```")) {
        let j = i + 1;
        const code = [];
        while (j < lines.length && !lines[j].startsWith("```")) { code.push(lines[j]); j++; }
        container.append(h("pre", { class: "d-code-block" }, h("code", {}, code.join("\n"))));
        i = j + 1;
        continue;
      }
      if (/^#{1,3}\s/.test(lines[i])) {
        const level = lines[i].match(/^#{1,3}/)[0].length;
        container.append(h(level === 1 ? "h1" : level === 2 ? "h2" : "h3", { class: "d-heading" }, parseInline(lines[i].replace(/^#{1,3}\s/, ""))));
        i++;
        continue;
      }
      if (lines[i].startsWith("> ")) {
        const quote = [];
        let j = i;
        while (j < lines.length && lines[j].startsWith("> ")) { quote.push(lines[j].slice(2)); j++; }
        container.append(h("blockquote", { class: "d-quote" }, joinLines(quote)));
        i = j;
        continue;
      }
      if (/^-\s/.test(lines[i])) {
        const items = [];
        let j = i;
        while (j < lines.length && /^-\s/.test(lines[j])) { items.push(lines[j].replace(/^-\s/, "")); j++; }
        container.append(h("ul", { class: "d-list" }, items.map((it) => h("li", {}, parseInline(it)))));
        i = j;
        continue;
      }
      if (lines[i].trim() === "") { i++; continue; }
      const para = [];
      let j = i;
      while (j < lines.length && lines[j].trim() !== "" && !isBlockStart(lines[j])) { para.push(lines[j]); j++; }
      container.append(h("p", { class: "d-para" }, joinLines(para)));
      i = j;
    }
    return container;
  }

  // ---------- Discord-style message preview ----------------------------------------------------

  function groupFields(fields) {
    const rows = [];
    let row = [];
    for (const f of fields) {
      if (!f.inline) {
        if (row.length) { rows.push(row); row = []; }
        rows.push([f]);
        continue;
      }
      row.push(f);
      if (row.length === 3) { rows.push(row); row = []; }
    }
    if (row.length) rows.push(row);
    return rows;
  }

  function embedPreview(e) {
    const rows = groupFields(e.fields || []);
    const card = h("div", { class: "d-embed" },
      h("div", { class: "d-embed-body" },
        e.author_name ? h("div", { class: "d-embed-author" },
          e.author_icon_url ? h("img", { class: "d-embed-author-icon", src: e.author_icon_url, alt: "" }) : null,
          e.author_url ? h("a", { href: e.author_url, target: "_blank", rel: "noopener noreferrer" }, e.author_name) : e.author_name) : null,
        e.title ? h("div", { class: "d-embed-title" }, e.url ? h("a", { href: e.url, target: "_blank", rel: "noopener noreferrer" }, e.title) : e.title) : null,
        e.description ? renderContent(e.description) : null,
        rows.length ? h("div", { class: "d-embed-fields" }, rows.map((r) => h("div", { class: `d-embed-field-row cols-${r.length}` },
          r.map((f) => h("div", { class: "d-embed-field" },
            h("div", { class: "d-embed-field-name" }, parseInline(f.name)),
            h("div", { class: "d-embed-field-value" }, renderContent(f.value))))))) : null,
        e.image_url ? h("img", { class: "d-embed-image", src: e.image_url, alt: "" }) : null,
        (e.footer || e.timestamp) ? h("div", { class: "d-embed-footer" }, [e.footer, e.timestamp ? new Date().toLocaleString(Panel.locale()) : null].filter(Boolean).join(" · ")) : null),
      e.thumbnail_url ? h("img", { class: "d-embed-thumbnail", src: e.thumbnail_url, alt: "" }) : null);
    if (e.color) card.style.borderLeftColor = e.color;
    return card;
  }

  function messagePreview({ content, embeds, buttons }) {
    return h("div", { class: "d-message" },
      h("img", { class: "d-avatar", src: BOT_AVATAR, alt: "" }),
      h("div", { class: "d-body" },
        h("div", { class: "d-header" },
          h("span", { class: "d-name" }, BOT_NAME),
          h("span", { class: "d-bot-tag" }, t("BOT")),
          h("span", { class: "d-timestamp" }, new Date().toLocaleTimeString(Panel.locale(), { hour: "numeric", minute: "2-digit" }))),
        content ? renderContent(content) : null,
        embeds.map(embedPreview),
        buttons.length ? h("div", { class: "d-buttons" }, buttons.map((b) => h("a", { class: "d-link-button", href: b.url, target: "_blank", rel: "noopener noreferrer" }, b.label, " ↗"))) : null));
  }

  // ---------- composer sub-editors ----------------------------------------------------------------

  function embedEditor(onChange, initial) {
    initial = initial || {};
    const inputs = {
      author_name: h("input", { type: "text", value: initial.author_name || "", maxlength: "256", size: "30" }),
      author_url: h("input", { type: "url", value: initial.author_url || "", size: "30", placeholder: "https://…" }),
      author_icon_url: h("input", { type: "url", value: initial.author_icon_url || "", size: "30", placeholder: "https://…" }),
      title: h("input", { type: "text", value: initial.title || "", maxlength: "256", size: "40" }),
      url: h("input", { type: "url", value: initial.url || "", size: "40", placeholder: "https://…" }),
      description: h("textarea", { rows: "4", maxlength: "4096", value: initial.description || "" }),
      image_url: h("input", { type: "url", value: initial.image_url || "", size: "40", placeholder: "https://…" }),
      thumbnail_url: h("input", { type: "url", value: initial.thumbnail_url || "", size: "40", placeholder: "https://…" }),
      footer: h("input", { type: "text", value: initial.footer || "", maxlength: "2048", size: "40" }),
    };
    const useColor = h("input", { type: "checkbox", checked: Boolean(initial.color) });
    const colorInput = h("input", { type: "color", value: initial.color || "#f39c12" });
    const timestamp = h("input", { type: "checkbox", checked: Boolean(initial.timestamp) });

    let fields = [];
    const fieldsBox = h("div", { class: "stack" });
    function drawFields() {
      fieldsBox.replaceChildren(...fields.map((f, i) => h("div", { class: "field-row" },
        field(t("Field name"), f.name),
        field(t("Field value"), f.value),
        h("label", { class: "row" }, f.inline, t("Inline")),
        h("button", { class: "btn small ghost", type: "button", onclick: () => { fields.splice(i, 1); drawFields(); onChange(); } }, t("Remove")))));
    }
    function addField(data) {
      data = data || {};
      if (fields.length >= 25) { toast(t("Discord allows at most 25 fields per embed."), true); return; }
      const name = h("input", { type: "text", value: data.name || "", maxlength: "256" });
      const value = h("textarea", { rows: "2", value: data.value || "" });
      const inline = h("input", { type: "checkbox", checked: Boolean(data.inline) });
      for (const el of [name, value]) el.addEventListener("input", onChange);
      inline.addEventListener("change", onChange);
      fields.push({ name, value, inline });
      drawFields();
      onChange();
    }
    (initial.fields || []).forEach(addField);

    for (const input of Object.values(inputs)) input.addEventListener("input", onChange);
    for (const input of [useColor, colorInput, timestamp]) input.addEventListener("change", onChange);
    colorInput.addEventListener("input", onChange);

    const el = h("div", { class: "embed-editor stack" },
      h("div", { class: "grid" },
        field(t("Author name"), inputs.author_name),
        field(t("Author link"), inputs.author_url),
        field(t("Author icon URL"), inputs.author_icon_url)),
      field(t("Title"), inputs.title),
      field(t("Title link"), inputs.url),
      field(t("Description"), inputs.description),
      h("div", { class: "row" },
        h("label", { class: "row" }, useColor, t("Color")), colorInput,
        h("label", { class: "row" }, timestamp, t("Show timestamp (time of sending)"))),
      h("h4", {}, t("Fields")),
      fieldsBox,
      h("button", { class: "btn small", type: "button", onclick: () => addField() }, t("Add field")),
      h("div", { class: "grid" },
        field(t("Image URL"), inputs.image_url),
        field(t("Thumbnail URL"), inputs.thumbnail_url)),
      field(t("Footer"), inputs.footer));

    return {
      el,
      get() {
        return {
          author_name: inputs.author_name.value, author_url: inputs.author_url.value, author_icon_url: inputs.author_icon_url.value,
          title: inputs.title.value, url: inputs.url.value, description: inputs.description.value,
          color: useColor.checked ? colorInput.value : "", timestamp: timestamp.checked,
          fields: fields.map((f) => ({ name: f.name.value, value: f.value.value, inline: f.inline.checked })),
          image_url: inputs.image_url.value, thumbnail_url: inputs.thumbnail_url.value, footer: inputs.footer.value,
        };
      },
    };
  }

  function embedsController(onChange) {
    let items = [];
    const box = h("div", { class: "stack" });
    function draw() {
      box.replaceChildren(...items.map((e, i) => h("div", { class: "card stack" },
        h("div", { class: "row spread" }, h("strong", {}, t("Embed {n}", { n: i + 1 })),
          h("button", { class: "btn small ghost", type: "button", onclick: () => { items.splice(i, 1); draw(); onChange(); } }, t("Remove embed"))),
        e.el)));
    }
    return {
      el: box,
      add(data) {
        if (items.length >= 10) { toast(t("Discord allows at most 10 embeds per message."), true); return; }
        items.push(embedEditor(onChange, data));
        draw();
        onChange();
      },
      reset(list) { items = (list || []).map((data) => embedEditor(onChange, data)); draw(); onChange(); },
      get: () => items.map((e) => e.get()),
    };
  }

  function buttonsController(onChange) {
    let items = [];
    const box = h("div", { class: "stack" });
    function draw() {
      box.replaceChildren(...items.map((b, i) => h("div", { class: "row" },
        field(t("Label"), b.label), field(t("URL"), b.url),
        h("button", { class: "btn small ghost", type: "button", onclick: () => { items.splice(i, 1); draw(); onChange(); } }, t("Remove")))));
    }
    function add(data) {
      data = data || {};
      if (items.length >= 5) { toast(t("Discord allows at most 5 buttons."), true); return; }
      const label = h("input", { type: "text", value: data.label || "", maxlength: "80", size: "20" });
      const url = h("input", { type: "url", value: data.url || "", size: "40", placeholder: "https://…" });
      for (const el of [label, url]) el.addEventListener("input", onChange);
      items.push({ label, url });
      draw();
      onChange();
    }
    return {
      el: h("div", { class: "stack" }, box, h("button", { class: "btn small", type: "button", onclick: () => add() }, t("Add button"))),
      reset(list) { items = []; (list || []).forEach(add); },
      get: () => items.map((b) => ({ label: b.label.value, url: b.url.value })).filter((b) => b.label || b.url),
    };
  }

  function rolesPicker(onChange) {
    const box = h("div", { class: "role-picker muted small" }, t("Loading roles…"));
    let checkboxes = new Map();
    let pending = null;
    function apply(ids) { for (const [id, cb] of checkboxes) cb.checked = ids.includes(id); }
    Panel.guild().then((g) => {
      checkboxes = new Map();
      const items = g.roles.map((r) => {
        const cb = h("input", { type: "checkbox" });
        cb.addEventListener("change", onChange);
        checkboxes.set(r.id, cb);
        return h("label", {}, cb, `@${r.name}`);
      });
      box.replaceChildren(...(items.length ? items : [h("span", { class: "muted small" }, t("No roles in this server."))]));
      if (pending) { apply(pending); pending = null; }
    }).catch(() => { box.replaceChildren(h("span", { class: "error small" }, t("Couldn't load roles."))); });
    return {
      el: box,
      get: () => [...checkboxes.entries()].filter(([, cb]) => cb.checked).map(([id]) => id),
      setSelected(ids) { if (checkboxes.size) apply(ids); else pending = ids; },
    };
  }

  function presetEmbedToForm(e) {
    return {
      author_name: (e.author && e.author.name) || "", author_url: (e.author && e.author.url) || "", author_icon_url: (e.author && e.author.icon_url) || "",
      title: e.title || "", url: e.url || "", description: e.description || "",
      color: hex(e.color), timestamp: Boolean(e.timestamp),
      fields: (e.fields || []).map((f) => ({ name: f.name, value: f.value, inline: Boolean(f.inline) })),
      image_url: (e.image && e.image.url) || "", thumbnail_url: (e.thumbnail && e.thumbnail.url) || "", footer: (e.footer && e.footer.text) || "",
    };
  }

  function toLocalInputValue(iso) {
    const d = new Date(iso);
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  // ---------- page ---------------------------------------------------------------------------------

  Panel.page({
    id: "announce",
    title: "Announce",
    perm: "announce.send",
    group: "Community",
    async render(view) {
      let composerMode = null; // null | {kind:"edit", channel_id, message_id, jump_url, panel_id} | {kind:"schedule", id}
      let draftId = null;

      const channel = channelSelect(null, { types: ["text", "news"], includeNone: true });
      const content = h("textarea", { rows: "5", maxlength: "2000" });
      const scheduleAt = h("input", { type: "datetime-local" });
      const everyone = h("input", { type: "checkbox" });
      const translateEs = h("input", { type: "checkbox" });
      const translatePt = h("input", { type: "checkbox" });
      const preview = h("div", { class: "stack" });
      const result = h("div");
      const modeRow = h("div", { class: "row" });
      const send = h("button", { class: "btn primary", type: "button" }, t("Send"));

      function drawPreview() {
        const payload = currentPayload();
        preview.replaceChildren(messagePreview({
          content: payload.content,
          embeds: payload.embeds.filter((e) => e.title || e.description || e.author_name || e.fields.length || e.image_url || e.thumbnail_url || e.footer),
          buttons: payload.buttons,
        }));
      }

      const embedsCtl = embedsController(drawPreview);
      const buttonsCtl = buttonsController(drawPreview);
      const roles = rolesPicker(drawPreview);

      function currentPayload() {
        return {
          content: content.value,
          embeds: embedsCtl.get(),
          buttons: buttonsCtl.get(),
          mention_roles: roles.get(),
          mention_everyone: everyone.checked,
          translate: { es: translateEs.checked, pt: translatePt.checked },
        };
      }

      function pingsWarning(payload) {
        const pinging = payload.mention_everyone || payload.mention_roles.length;
        return pinging
          ? h("p", { class: "error" }, t("This will ping people (selected roles and/or @everyone/@here)."))
          : h("p", { class: "muted" }, t("No pings: mentions render but notify nobody unless you picked roles or checked @everyone/@here."));
      }

      function applyPayload(payload) {
        content.value = payload.content || "";
        embedsCtl.reset(payload.embeds || []);
        buttonsCtl.reset(payload.buttons || []);
        everyone.checked = Boolean(payload.mention_everyone);
        translateEs.checked = Boolean(payload.translate && payload.translate.es);
        translatePt.checked = Boolean(payload.translate && payload.translate.pt);
        roles.setSelected(payload.mention_roles || []);
        drawPreview();
      }

      const stopBtn = h("button", { class: "btn small ghost", type: "button", onclick: () => resetComposer() }, t("Cancel / start new"));

      function updateSendLabel() {
        if (composerMode && composerMode.kind === "edit") send.textContent = t("Save edit");
        else if (composerMode && composerMode.kind === "schedule") send.textContent = t("Save schedule");
        else send.textContent = scheduleAt.value ? t("Schedule send") : t("Send");
      }

      function drawMode() {
        const isEdit = composerMode && composerMode.kind === "edit";
        const isSchedule = composerMode && composerMode.kind === "schedule";
        channel.disabled = isEdit;
        scheduleWrap.hidden = isEdit;
        translateWrap.hidden = isEdit;
        updateSendLabel();
        modeRow.replaceChildren(...(isEdit ? [
          badge(t("editing"), "warn"),
          h("a", { href: composerMode.jump_url, target: "_blank", rel: "noopener noreferrer" }, t("message {id}", { id: composerMode.message_id })),
          stopBtn,
        ] : isSchedule ? [badge(t("editing scheduled send"), "warn"), stopBtn] : []));
      }

      function resetComposer() {
        composerMode = null;
        draftId = null;
        channel.value = "";
        content.value = "";
        scheduleAt.value = "";
        everyone.checked = false;
        translateEs.checked = false;
        translatePt.checked = false;
        embedsCtl.reset([]);
        buttonsCtl.reset([]);
        roles.setSelected([]);
        result.replaceChildren();
        drawMode();
        drawPreview();
      }

      async function doCreate() {
        if (!channel.value) { toast(t("Pick a channel first."), true); return; }
        const payload = { ...currentPayload(), channel_id: channel.value, send_at: scheduleAt.value ? new Date(scheduleAt.value).toISOString() : null };
        const where = await channelName(channel.value);
        const scheduling = Boolean(payload.send_at);
        const ok = await dialog(scheduling ? t("Schedule this message?") : t("Send this message?"), [
          h("p", {}, scheduling
            ? t("Post as the bot in {where} at {time}.", { where, time: new Date(payload.send_at).toLocaleString(Panel.locale()) })
            : t("Post as the bot in {where}.", { where })),
          pingsWarning(payload),
        ], { confirmLabel: scheduling ? t("Schedule") : t("Send") });
        if (!ok) return;
        const sent = await run(send, () => api("/api/announce", { method: "POST", body: payload }), scheduling ? t("Scheduled") : t("Message sent"));
        if (!sent) return;
        if (draftId) { await api(`/api/announce/drafts/${draftId}`, { method: "DELETE" }).catch(() => {}); draftId = null; }
        result.replaceChildren(scheduling
          ? h("p", {}, t("Scheduled for {time}.", { time: new Date(sent.send_at).toLocaleString(Panel.locale()) }))
          : h("p", {},
              h("span", {}, t("Sent: ")),
              h("a", { href: sent.jump_url, target: "_blank", rel: "noopener noreferrer" }, sent.jump_url),
              sent.translation_errors && Object.keys(sent.translation_errors).length
                ? h("div", { class: "error small" }, t("Translation failed for: {langs}", { langs: Object.keys(sent.translation_errors).join(", ") }))
                : null));
        await Promise.all([refreshScheduled(), refreshHistory()]);
      }

      async function doSaveEdit() {
        const payload = { ...currentPayload(), panel_id: composerMode.panel_id || null };
        const where = await channelName(composerMode.channel_id);
        const ok = await dialog(t("Save changes to this message?"), [
          h("p", {}, t("Edit the bot's message {id} in {where}.", { id: composerMode.message_id, where })),
          pingsWarning(payload),
        ], { confirmLabel: t("Save edit") });
        if (!ok) return;
        const edited = await run(send, () => api(`/api/announce/${composerMode.channel_id}/${composerMode.message_id}`, { method: "PATCH", body: payload }), t("Message edited"));
        if (!edited) return;
        result.replaceChildren(h("p", {}, h("span", {}, t("Edited: ")), h("a", { href: edited.jump_url, target: "_blank", rel: "noopener noreferrer" }, edited.jump_url)));
        await refreshHistory();
      }

      async function doSaveSchedule() {
        if (!channel.value) { toast(t("Pick a channel first."), true); return; }
        if (!scheduleAt.value) { toast(t("Pick a time first."), true); return; }
        const payload = { ...currentPayload(), channel_id: channel.value, send_at: new Date(scheduleAt.value).toISOString() };
        const row = await run(send, () => api(`/api/announce/scheduled/${composerMode.id}`, { method: "PUT", body: payload }), t("Schedule updated"));
        if (!row) return;
        resetComposer();
        await refreshScheduled();
      }

      send.addEventListener("click", () => {
        if (composerMode && composerMode.kind === "edit") return doSaveEdit();
        if (composerMode && composerMode.kind === "schedule") return doSaveSchedule();
        return doCreate();
      });

      const saveDraftBtn = h("button", { class: "btn ghost", type: "button" }, t("Save draft"));
      saveDraftBtn.addEventListener("click", () => run(saveDraftBtn, async () => {
        const body = { ...currentPayload(), channel_id: channel.value || null };
        const row = draftId
          ? await api(`/api/announce/drafts/${draftId}`, { method: "PUT", body })
          : await api("/api/announce/drafts", { method: "POST", body });
        draftId = row.id;
        await refreshDrafts();
      }, t("Draft saved")));

      // ---------- fill embed from a preset ----------

      const presetSelect = h("select", {}, h("option", { value: "" }, t("— choose a preset embed —")));
      const presetEmbeds = [];
      api("/api/announce/presets").then(({ presets }) => {
        for (const p of presets) {
          p.embeds.forEach((e, i) => {
            presetEmbeds.push(e);
            presetSelect.append(h("option", { value: String(presetEmbeds.length - 1) },
              `${t(KINDS[p.kind]) || p.kind} · ${LANGS[p.language] || p.language} · ${t("embed {n}", { n: i + 1 })}${e.title ? ` · ${e.title}` : ""}`));
          });
        }
      }).catch((error) => toast(error.message, true));
      const usePreset = h("button", { class: "btn small", type: "button" }, t("Add as embed"));
      usePreset.addEventListener("click", () => {
        const e = presetEmbeds[Number(presetSelect.value)];
        if (presetSelect.value && e) embedsCtl.add(presetEmbedToForm(e));
      });

      // ---------- load an existing bot message to edit ad hoc ----------

      const ref = h("input", { type: "text", size: "50", placeholder: t("Message link (or ID of a message in the selected channel)") });
      const load = h("button", { class: "btn small", type: "button" }, t("Load"));
      load.addEventListener("click", () => run(load, async () => {
        const query = new URLSearchParams({ ref: ref.value.trim(), channel_id: channel.value });
        const message = await api(`/api/announce/message?${query}`);
        composerMode = { kind: "edit", channel_id: message.channel_id, message_id: message.message_id, jump_url: message.jump_url, panel_id: null };
        draftId = null;
        channel.value = message.channel_id;
        content.value = message.content;
        embedsCtl.reset(message.embeds);
        buttonsCtl.reset(message.buttons);
        everyone.checked = false;
        roles.setSelected([]);
        drawMode();
        drawPreview();
        result.replaceChildren();
      }, t("Message loaded")));

      content.addEventListener("input", drawPreview);
      scheduleAt.addEventListener("input", updateSendLabel);

      const translateWrap = h("div", { class: "row" },
        h("label", { class: "row" }, translateEs, t("Also post a Spanish translation")),
        h("label", { class: "row" }, translatePt, t("Also post a Portuguese translation")));
      const scheduleWrap = field(t("Send at (leave blank to send immediately)"), scheduleAt);

      // ---------- scheduled / history / drafts lists ----------

      const panels = { scheduled: h("div"), history: h("div"), drafts: h("div") };

      function loadScheduledIntoComposer(row) {
        composerMode = { kind: "schedule", id: row.id };
        draftId = null;
        channel.value = row.channel_id;
        applyPayload(row.payload);
        scheduleAt.value = toLocalInputValue(row.send_at);
        drawMode();
        view.querySelector("#announce-compose-anchor")?.scrollIntoView({ block: "start" });
      }

      async function refreshScheduled() {
        const { items } = await api("/api/announce/scheduled");
        panels.scheduled.replaceChildren(items.length
          ? h("div", { class: "stack" }, items.map((row) => {
              const editBtn = h("button", { class: "btn small", type: "button" }, t("Load to edit"));
              editBtn.addEventListener("click", () => loadScheduledIntoComposer(row));
              const sendNowBtn = h("button", { class: "btn small", type: "button" }, t("Send now"));
              sendNowBtn.addEventListener("click", () => run(sendNowBtn, async () => {
                await api(`/api/announce/scheduled/${row.id}/send-now`, { method: "POST" });
                await Promise.all([refreshScheduled(), refreshHistory()]);
              }, t("Sent")));
              const cancelBtn = h("button", { class: "btn small danger", type: "button" }, t("Cancel"));
              cancelBtn.addEventListener("click", () => run(cancelBtn, async () => {
                const ok = await dialog(t("Cancel this scheduled announcement?"), h("p", {}, t("This can't be undone.")), { confirmLabel: t("Cancel send"), danger: true });
                if (!ok) return;
                await api(`/api/announce/scheduled/${row.id}/cancel`, { method: "POST" });
                await refreshScheduled();
              }));
              return h("div", { class: "card row spread" },
                h("div", {},
                  h("div", {}, row.payload.content ? row.payload.content.slice(0, 80) : (row.payload.embeds[0] && row.payload.embeds[0].title) || t("(no content)")),
                  h("div", { class: "muted small" }, channelLabel(row.channel_id), " · ", new Date(row.send_at).toLocaleString(Panel.locale())),
                  row.status === "failed" ? h("div", { class: "error small" }, row.error) : null),
                h("div", { class: "row" }, editBtn, sendNowBtn, cancelBtn));
            }))
          : h("div", { class: "empty" }, t("Nothing scheduled.")));
      }

      async function refreshHistory() {
        const { items } = await api("/api/announce/history");
        panels.history.replaceChildren(items.length
          ? h("div", { class: "stack" }, items.map((row) => {
              const messageIds = row.message_ids && row.message_ids.main;
              const editBtn = h("button", { class: "btn small", type: "button", disabled: !messageIds || !messageIds.length }, t("Load to edit"));
              editBtn.addEventListener("click", () => {
                composerMode = { kind: "edit", channel_id: row.channel_id, message_id: messageIds[0], jump_url: `https://discord.com/channels/${(Panel.me() || {}).guild?.id || "@me"}/${row.channel_id}/${messageIds[0]}`, panel_id: row.id };
                draftId = null;
                channel.value = row.channel_id;
                applyPayload(row.payload);
                drawMode();
                view.querySelector("#announce-compose-anchor")?.scrollIntoView({ block: "start" });
              });
              const statusKind = row.status === "sent" ? "ok" : row.status === "failed" ? "danger" : "";
              return h("div", { class: "card row spread" },
                h("div", {},
                  h("div", {}, row.payload.content ? row.payload.content.slice(0, 80) : (row.payload.embeds && row.payload.embeds[0] && row.payload.embeds[0].title) || t("(no content)")),
                  h("div", { class: "row muted small" }, badge(t(row.status), statusKind), channelLabel(row.channel_id), Panel.time(row.sent_at || row.updated_at)),
                  row.error ? h("div", { class: "error small" }, row.error) : null,
                  row.payload.translation_errors ? h("div", { class: "error small" }, t("Translation failed for: {langs}", { langs: Object.keys(row.payload.translation_errors).join(", ") })) : null),
                h("div", { class: "row" }, editBtn));
            }))
          : h("div", { class: "empty" }, t("No announcements sent from the panel yet.")));
      }

      async function refreshDrafts() {
        const { items } = await api("/api/announce/drafts");
        panels.drafts.replaceChildren(items.length
          ? h("div", { class: "stack" }, items.map((row) => {
              const loadBtn = h("button", { class: "btn small", type: "button" }, t("Load"));
              loadBtn.addEventListener("click", () => {
                composerMode = null;
                draftId = row.id;
                channel.value = row.channel_id || "";
                applyPayload(row.payload);
                drawMode();
                view.querySelector("#announce-compose-anchor")?.scrollIntoView({ block: "start" });
              });
              const deleteBtn = h("button", { class: "btn small danger", type: "button" }, t("Delete"));
              deleteBtn.addEventListener("click", () => run(deleteBtn, async () => {
                await api(`/api/announce/drafts/${row.id}`, { method: "DELETE" });
                await refreshDrafts();
              }, t("Draft deleted")));
              return h("div", { class: "card row spread" },
                h("div", {}, row.payload.content ? row.payload.content.slice(0, 80) : t("(no content)"),
                  h("div", { class: "muted small" }, Panel.time(row.updated_at))),
                h("div", { class: "row" }, loadBtn, deleteBtn));
            }))
          : h("div", { class: "empty" }, t("No saved drafts.")));
      }

      view.append(
        h("h1", {}, t("Announce")),
        h("p", { class: "muted" }, t("Compose a rich message as the bot: embeds, buttons, role pings, scheduling and optional translations.")),
        h("div", { id: "announce-compose-anchor" }),
        h("div", { class: "card stack" },
          h("h2", {}, t("Load an existing bot message to edit")),
          h("div", { class: "row" }, ref, load),
          modeRow),
        h("div", { class: "card stack" },
          field(t("Channel"), channel),
          field(t("Content"), content),
          h("h3", {}, t("Embeds (up to 10)")),
          h("div", { class: "row" }, presetSelect, usePreset),
          embedsCtl.el,
          h("h3", {}, t("Buttons (up to 5 link buttons)")),
          buttonsCtl.el,
          h("h3", {}, t("Pings")),
          h("div", { class: "stack" },
            h("div", {}, h("div", { class: "small muted" }, t("Roles that can ping when mentioned in the text above")), roles.el),
            h("label", { class: "row" }, everyone, t("Allow @everyone/@here to ping (only if the text contains it)"))),
          translateWrap,
          scheduleWrap,
          h("div", { class: "row" }, send, saveDraftBtn),
          result),
        h("h2", {}, t("Preview")),
        preview,
        h("h2", {}, t("Scheduled")),
        panels.scheduled,
        h("h2", {}, t("History")),
        panels.history,
        h("h2", {}, t("Drafts")),
        panels.drafts);

      drawMode();
      drawPreview();
      await Promise.all([refreshScheduled(), refreshHistory(), refreshDrafts()]);
    },
  });

  i18n({
    "BOT": "BOT",
    "Author name": "Nombre del autor",
    "Author link": "Enlace del autor",
    "Author icon URL": "URL del ícono del autor",
    "Title": "Título",
    "Title link": "Enlace del título",
    "Description": "Descripción",
    "Color": "Color",
    "Show timestamp (time of sending)": "Mostrar fecha y hora (momento del envío)",
    "Fields": "Campos",
    "Field name": "Nombre del campo",
    "Field value": "Valor del campo",
    "Inline": "En línea",
    "Remove": "Quitar",
    "Add field": "Agregar campo",
    "Image URL": "URL de la imagen",
    "Thumbnail URL": "URL de la miniatura",
    "Footer": "Pie de página",
    "Discord allows at most 25 fields per embed.": "Discord permite hasta 25 campos por embed.",
    "Embed {n}": "Embed {n}",
    "Remove embed": "Quitar embed",
    "Discord allows at most 10 embeds per message.": "Discord permite hasta 10 embeds por mensaje.",
    "Label": "Etiqueta",
    "URL": "URL",
    "Add button": "Agregar botón",
    "Discord allows at most 5 buttons.": "Discord permite hasta 5 botones.",
    "Loading roles…": "Cargando roles…",
    "No roles in this server.": "No hay roles en este servidor.",
    "Couldn't load roles.": "No se pudieron cargar los roles.",
    "Send": "Enviar",
    "Save edit": "Guardar edición",
    "Save schedule": "Guardar programación",
    "Schedule send": "Programar envío",
    "editing": "editando",
    "editing scheduled send": "editando envío programado",
    "message {id}": "mensaje {id}",
    "Cancel / start new": "Cancelar / empezar de nuevo",
    "Pick a channel first.": "Elige un canal primero.",
    "Pick a time first.": "Elige una hora primero.",
    "This will ping people (selected roles and/or @everyone/@here).": "Esto notificará a personas (roles seleccionados y/o @everyone/@here).",
    "No pings: mentions render but notify nobody unless you picked roles or checked @everyone/@here.":
      "Sin notificaciones: las menciones se muestran pero no avisan a nadie salvo que hayas elegido roles o marcado @everyone/@here.",
    "Schedule this message?": "¿Programar este mensaje?",
    "Send this message?": "¿Enviar este mensaje?",
    "Post as the bot in {where} at {time}.": "Publicar como el bot en {where} el {time}.",
    "Post as the bot in {where}.": "Publicar como el bot en {where}.",
    "Schedule": "Programar",
    "Scheduled": "Programado",
    "Message sent": "Mensaje enviado",
    "Scheduled for {time}.": "Programado para el {time}.",
    "Sent: ": "Enviado: ",
    "Translation failed for: {langs}": "Falló la traducción para: {langs}",
    "Save changes to this message?": "¿Guardar cambios en este mensaje?",
    "Edit the bot's message {id} in {where}.": "Edita el mensaje {id} del bot en {where}.",
    "Message edited": "Mensaje editado",
    "Edited: ": "Editado: ",
    "Schedule updated": "Programación actualizada",
    "Draft saved": "Borrador guardado",
    "— choose a preset embed —": "— elige un embed de preset —",
    "embed {n}": "embed {n}",
    "Add as embed": "Agregar como embed",
    "Message link (or ID of a message in the selected channel)": "Enlace del mensaje (o ID de un mensaje en el canal elegido)",
    "Load": "Cargar",
    "Message loaded": "Mensaje cargado",
    "Also post a Spanish translation": "También publicar una traducción al español",
    "Also post a Portuguese translation": "También publicar una traducción al portugués",
    "Send at (leave blank to send immediately)": "Enviar el (deja vacío para enviar de inmediato)",
    "Announce": "Anunciar",
    "Compose a rich message as the bot: embeds, buttons, role pings, scheduling and optional translations.":
      "Redacta un mensaje como el bot: embeds, botones, menciones de roles, programación y traducciones opcionales.",
    "Load an existing bot message to edit": "Cargar un mensaje existente del bot para editarlo",
    "Channel": "Canal",
    "Content": "Contenido",
    "Embeds (up to 10)": "Embeds (hasta 10)",
    "Buttons (up to 5 link buttons)": "Botones (hasta 5 botones de enlace)",
    "Pings": "Menciones",
    "Roles that can ping when mentioned in the text above": "Roles que pueden notificar si se mencionan en el texto de arriba",
    "Allow @everyone/@here to ping (only if the text contains it)": "Permitir que @everyone/@here notifique (solo si el texto lo contiene)",
    "Save draft": "Guardar borrador",
    "Preview": "Vista previa",
    "Scheduled": "Programados",
    "History": "Historial",
    "Drafts": "Borradores",
    "Nothing scheduled.": "No hay nada programado.",
    "(no content)": "(sin contenido)",
    "Load to edit": "Cargar para editar",
    "Send now": "Enviar ahora",
    "Sent": "Enviado",
    "Cancel": "Cancelar",
    "Cancel this scheduled announcement?": "¿Cancelar este anuncio programado?",
    "This can't be undone.": "Esto no se puede deshacer.",
    "Cancel send": "Cancelar envío",
    "No announcements sent from the panel yet.": "Aún no se ha enviado ningún anuncio desde el panel.",
    "draft": "borrador",
    "scheduled": "programado",
    "sending": "enviando",
    "sent": "enviado",
    "failed": "fallido",
    "cancelled": "cancelado",
    "No saved drafts.": "No hay borradores guardados.",
    "Delete": "Eliminar",
    "Draft deleted": "Borrador eliminado",
    "Rules": "Reglas",
    "Support us": "Apóyanos",
  });
})();
