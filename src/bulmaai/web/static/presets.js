"use strict";

(() => {
  const { h, api, run, badge, table, field, dialog, channelSelect, channelName, toast, go } = Panel;
  const LANGS = { en: "English", es: "Español", pt: "Português" };
  const KINDS = { rules: "Rules", support: "Support us" };
  const LONG_SUPPORT_FIELDS = /(description|_value)$/;

  const hex = (n) => (typeof n === "number" ? `#${n.toString(16).padStart(6, "0").toUpperCase()}` : "");

  // Text-only stand-in for a Discord embed (e = embed.to_dict()); Markdown is shown as typed.
  function embedView(e) {
    return h("div", { class: "card stack" },
      e.color !== undefined ? h("div", { class: "row" }, badge(hex(e.color))) : null,
      e.title ? h("h3", {}, e.title) : null,
      e.url ? h("div", { class: "muted small" }, `Title link: ${e.url}`) : null,
      e.description ? h("pre", {}, e.description) : null,
      (e.fields || []).map((f) => h("div", {},
        h("div", { class: "row" }, h("strong", {}, f.name), f.inline ? badge("inline") : null),
        h("pre", {}, f.value))),
      e.thumbnail && e.thumbnail.url ? h("div", { class: "muted small" }, `Thumbnail: ${e.thumbnail.url}`) : null,
      e.image && e.image.url ? h("div", { class: "muted small" }, `Image: ${e.image.url}`) : null,
      e.footer && e.footer.text ? h("div", { class: "muted small" }, e.footer.text) : null);
  }

  function presetPreview(preset) {
    return [
      preset.embeds.map(embedView),
      preset.buttons.length
        ? h("div", { class: "row" }, preset.buttons.map((label) => h("button", { class: "btn small", type: "button", disabled: true }, label)))
        : null,
    ];
  }

  // ---------- Presets ----------

  function rulesForm(container, data) {
    const title = h("input", { type: "text", value: data.title, maxlength: "256", size: "60" });
    const list = h("div", { class: "stack" });
    const makeSection = (s) => ({
      title: h("input", { type: "text", value: s.title || "", maxlength: "256", size: "60" }),
      content: h("textarea", { rows: "6", value: s.content || "" }),
    });
    const sections = data.sections.map(makeSection);
    const move = (from, to) => { sections.splice(to, 0, sections.splice(from, 1)[0]); draw(); };

    const draw = () => list.replaceChildren(...sections.map((s, i) => h("div", { class: "card stack" },
      h("div", { class: "row spread" },
        h("strong", {}, `Section ${i + 1}${i === 0 ? " (first embed, carries the title)" : ""}`),
        h("div", { class: "row" },
          h("button", { class: "btn small ghost", type: "button", disabled: i === 0, onclick: () => move(i, i - 1) }, "Move up"),
          h("button", { class: "btn small ghost", type: "button", disabled: sections.length === 1, onclick: () => { sections.splice(i, 1); draw(); } }, "Remove"))),
      field("Heading (optional; without one the content is the embed text, up to 4096 chars, with one it's a field, up to 1024)", s.title),
      field("Content", s.content))));

    const add = h("button", { class: "btn small", type: "button", onclick: () => { sections.push(makeSection({})); draw(); } }, "Add section");
    container.append(field("Title (shown on the first embed)", title), list, h("div", { class: "row" }, add, h("span", { class: "muted small" }, "Each section is its own embed; Discord allows 10 per message.")));
    draw();
    return () => ({
      title: title.value,
      sections: sections.map((s) => ({ title: s.title.value.trim() ? s.title.value : null, content: s.content.value })),
    });
  }

  function supportForm(container, data) {
    const inputs = Object.fromEntries(Object.entries(data).map(([key, value]) => [key,
      LONG_SUPPORT_FIELDS.test(key) ? h("textarea", { rows: "4", value }) : h("input", { type: "text", value, size: "60" })]));
    container.append(...Object.entries(inputs).map(([key, input]) => field(key, input)));
    return () => Object.fromEntries(Object.entries(inputs).map(([key, input]) => [key, input.value]));
  }

  async function presetEditor(view, kind, language) {
    const preset = (await api("/api/presets")).presets.find((p) => p.kind === kind && p.language === language);
    if (!preset) { view.append(h("p", { class: "error" }, "Unknown preset.")); return; }
    const url = `/api/presets/${encodeURIComponent(kind)}/${encodeURIComponent(language)}`;
    const status = h("span");
    const preview = h("div", { class: "stack" });
    const show = (p) => {
      status.replaceChildren(p.customized ? badge("customized", "accent") : badge("default"));
      preview.replaceChildren(...presetPreview(p).flat().filter(Boolean));
    };

    const form = h("div", { class: "stack" });
    const collect = kind === "rules" ? rulesForm(form, preset.data) : supportForm(form, preset.data);

    const previewBtn = h("button", { class: "btn", type: "button" }, "Preview");
    previewBtn.addEventListener("click", () => run(previewBtn, async () => {
      const draft = await api(`${url}/preview`, { method: "POST", body: { data: collect() } });
      preview.replaceChildren(h("p", { class: "muted small" }, "Unsaved draft:"), ...presetPreview(draft).flat().filter(Boolean));
    }));
    const save = h("button", { class: "btn primary", type: "button" }, "Save");
    save.addEventListener("click", () => run(save, async () => {
      show(await api(url, { method: "PUT", body: { data: collect() } }));
    }, "Preset saved"));
    const reset = h("button", { class: "btn ghost", type: "button" }, "Reset to default");
    reset.addEventListener("click", async () => {
      const ok = await dialog("Reset preset?", h("p", {}, `Replace ${KINDS[kind]} (${LANGS[language] || language}) with the built-in default text. The current text is kept in the audit log.`), { confirmLabel: "Reset", danger: true });
      if (ok) await run(reset, async () => { await api(url, { method: "DELETE" }); await Panel.refresh(); }, "Preset reset");
    });

    view.append(
      h("p", {}, h("a", { href: "#/presets" }, "← All presets")),
      h("div", { class: "row" }, h("h1", {}, `${KINDS[kind]} · ${LANGS[language] || language}`), status),
      h("div", { class: "card stack" }, form, h("div", { class: "row" }, previewBtn, save, reset)),
      h("h2", {}, "What the bot posts"),
      preview);
    show(preset);
  }

  Panel.page({
    id: "presets",
    title: "Presets",
    perm: "presets.edit",
    async render(view, args) {
      if (args.length === 2) { await presetEditor(view, args[0], args[1]); return; }
      const { presets } = await api("/api/presets");
      view.append(
        h("h1", {}, "Message presets"),
        h("p", { class: "muted" }, "Text for the rules and support-us messages (/rules setup, /supportus setup). Language buttons read these live; a message that's already posted keeps its old English text until it's posted again."),
        h("div", { class: "card" }, table([
          { label: "Preset", render: (p) => KINDS[p.kind] || p.kind },
          { label: "Language", render: (p) => LANGS[p.language] || p.language },
          { label: "Status", render: (p) => (p.customized ? badge("customized", "accent") : badge("default")) },
          { label: "Embeds", render: (p) => String(p.embeds.length) },
        ], presets, { onRowClick: (p) => go(`#/presets/${p.kind}/${p.language}`) })));
    },
  });

  // ---------- Announce ----------

  const EMBED_INPUTS = {
    title: ["Title", () => h("input", { type: "text", maxlength: "256", size: "60" })],
    description: ["Description", () => h("textarea", { rows: "6", maxlength: "4096" })],
    color: ["Color (hex)", () => h("input", { type: "text", maxlength: "7", size: "10", placeholder: "#F39C12" })],
    url: ["Title link", () => h("input", { type: "url", size: "60", placeholder: "https://…" })],
    image_url: ["Image URL", () => h("input", { type: "url", size: "60", placeholder: "https://…" })],
    thumbnail_url: ["Thumbnail URL", () => h("input", { type: "url", size: "60", placeholder: "https://…" })],
    footer: ["Footer", () => h("input", { type: "text", maxlength: "2048", size: "60" })],
  };

  // Presets can have fields; the single-embed form folds them into the description.
  function presetEmbedToForm(e) {
    return {
      title: e.title || "",
      description: [e.description, ...(e.fields || []).map((f) => `**${f.name}**\n${f.value}`)].filter(Boolean).join("\n\n"),
      color: hex(e.color),
      footer: (e.footer && e.footer.text) || "",
    };
  }

  Panel.page({
    id: "announce",
    title: "Announce",
    perm: "announce.send",
    async render(view) {
      let editing = null;
      const channel = channelSelect(null, { types: ["text", "news"], includeNone: true });
      const content = h("textarea", { rows: "5", maxlength: "2000" });
      const inputs = Object.fromEntries(Object.entries(EMBED_INPUTS).map(([key, [, make]]) => [key, make()]));
      const pings = h("input", { type: "checkbox", id: "announce-pings" });
      const preview = h("div", { class: "stack" });
      const result = h("div");
      const mode = h("div", { class: "row" });
      const send = h("button", { class: "btn primary", type: "button" }, "Send");

      const embedValues = () => Object.fromEntries(Object.entries(inputs).map(([key, input]) => [key, input.value]));
      const setEmbed = (values) => { for (const [key, input] of Object.entries(inputs)) input.value = values[key] || ""; drawPreview(); };

      function drawPreview() {
        const v = embedValues();
        const color = /^#?[0-9a-fA-F]{6}$/.test(v.color) ? parseInt(v.color.replace("#", ""), 16) : undefined;
        const hasEmbed = Object.values(v).some((x) => x.trim());
        preview.replaceChildren(...[
          content.value.trim() ? h("pre", {}, content.value) : null,
          hasEmbed ? embedView({
            title: v.title, description: v.description, url: v.url, color,
            image: { url: v.image_url }, thumbnail: { url: v.thumbnail_url }, footer: { text: v.footer },
          }) : null,
          !content.value.trim() && !hasEmbed ? h("div", { class: "empty" }, "Nothing to send yet.") : null,
        ].filter(Boolean));
      }

      function drawMode() {
        channel.disabled = Boolean(editing);
        send.textContent = editing ? "Save edit" : "Send";
        mode.replaceChildren(...(editing ? [
          badge("editing", "warn"),
          h("a", { href: editing.jump_url, target: "_blank", rel: "noopener noreferrer" }, `message ${editing.message_id}`),
          h("button", { class: "btn small ghost", type: "button", onclick: () => { editing = null; drawMode(); } }, "Stop editing (send new instead)"),
        ] : []));
      }

      // Pre-fill from a preset embed.
      const presetSelect = h("select", {}, h("option", { value: "" }, "— choose a preset embed —"));
      const presetEmbeds = [];
      api("/api/announce/presets").then(({ presets }) => {
        for (const p of presets) {
          p.embeds.forEach((e, i) => {
            presetEmbeds.push(e);
            presetSelect.append(h("option", { value: String(presetEmbeds.length - 1) },
              `${KINDS[p.kind] || p.kind} · ${LANGS[p.language] || p.language} · embed ${i + 1}${e.title ? ` · ${e.title}` : ""}`));
          });
        }
      }).catch((error) => toast(error.message, true));
      const usePreset = h("button", { class: "btn small", type: "button" }, "Fill embed");
      usePreset.addEventListener("click", () => {
        const e = presetEmbeds[Number(presetSelect.value)];
        if (presetSelect.value && e) setEmbed(presetEmbedToForm(e));
      });

      // Load a message the bot sent, to edit it.
      const ref = h("input", { type: "text", size: "60", placeholder: "Message link (or ID of a message in the selected channel)" });
      const load = h("button", { class: "btn small", type: "button" }, "Load");
      load.addEventListener("click", () => run(load, async () => {
        const query = new URLSearchParams({ ref: ref.value.trim(), channel_id: channel.value });
        const message = await api(`/api/announce/message?${query}`);
        editing = message;
        channel.value = message.channel_id;
        content.value = message.content;
        setEmbed(message.embed || {});
        drawMode();
        result.replaceChildren();
      }, "Message loaded"));

      send.addEventListener("click", async () => {
        if (!editing && !channel.value) { toast("Pick a channel first.", true); return; }
        const body = { content: content.value, embed: embedValues(), allow_pings: pings.checked };
        const where = await channelName(editing ? editing.channel_id : channel.value);
        const ok = await dialog(editing ? "Save changes to this message?" : "Send this message?", [
          h("p", {}, editing ? `Edit the bot's message ${editing.message_id} in ${where}.` : `Post as the bot in ${where}.`),
          pings.checked
            ? h("p", { class: "error" }, "Role, user and @everyone pings are enabled and will notify people.")
            : h("p", { class: "muted" }, "Pings are off: mentions render but notify nobody."),
        ], { confirmLabel: editing ? "Save edit" : "Send" });
        if (!ok) return;
        const sent = await run(send, () => (editing
          ? api(`/api/announce/${editing.channel_id}/${editing.message_id}`, { method: "PATCH", body })
          : api("/api/announce", { method: "POST", body: { ...body, channel_id: channel.value } })),
        editing ? "Message edited" : "Message sent");
        if (sent) {
          result.replaceChildren(h("p", {}, editing ? "Edited: " : "Sent: ",
            h("a", { href: sent.jump_url, target: "_blank", rel: "noopener noreferrer" }, sent.jump_url)));
        }
      });

      content.addEventListener("input", drawPreview);
      for (const input of Object.values(inputs)) input.addEventListener("input", drawPreview);

      view.append(
        h("h1", {}, "Announce"),
        h("p", { class: "muted" }, "Post or edit a message as the bot. Content and one embed; Discord's length limits are checked on send."),
        h("div", { class: "card stack" },
          h("h2", {}, "Edit an existing bot message"),
          h("div", { class: "row" }, ref, load),
          mode),
        h("div", { class: "card stack" },
          field("Channel", channel),
          field("Content", content),
          h("h3", {}, "Embed"),
          h("div", { class: "row" }, presetSelect, usePreset),
          Object.entries(EMBED_INPUTS).map(([key, [label]]) => field(label, inputs[key])),
          h("label", { for: "announce-pings", class: "row" }, pings, "Allow role and @everyone pings"),
          h("div", { class: "row" }, send),
          result),
        h("h2", {}, "Preview"),
        preview);
      drawMode();
      drawPreview();
    },
  });
})();
