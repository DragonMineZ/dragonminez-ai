"use strict";

(() => {
  const { h, api, run, badge, field, dialog, t, i18n } = Panel;
  const LANGS = { en: "English", es: "Español", pt: "Português" };
  const KINDS = { rules: "Rules", support: "Support us" };
  const LONG_SUPPORT_FIELDS = /(description|_value)$/;

  const hex = (n) => (typeof n === "number" ? `#${n.toString(16).padStart(6, "0").toUpperCase()}` : "");

  // Text-only stand-in for a Discord embed (e = embed.to_dict()); Markdown is shown as typed.
  function embedView(e) {
    return h("div", { class: "card stack" },
      e.color !== undefined ? h("div", { class: "row" }, badge(hex(e.color))) : null,
      e.title ? h("h3", {}, e.title) : null,
      e.url ? h("div", { class: "muted small" }, t("Title link: {url}", { url: e.url })) : null,
      e.description ? h("pre", {}, e.description) : null,
      (e.fields || []).map((f) => h("div", {},
        h("div", { class: "row" }, h("strong", {}, f.name), f.inline ? badge(t("inline")) : null),
        h("pre", {}, f.value))),
      e.thumbnail && e.thumbnail.url ? h("div", { class: "muted small" }, t("Thumbnail: {url}", { url: e.thumbnail.url })) : null,
      e.image && e.image.url ? h("div", { class: "muted small" }, t("Image: {url}", { url: e.image.url })) : null,
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
        h("strong", {}, i === 0 ? t("Section {n} (first embed, carries the title)", { n: i + 1 }) : t("Section {n}", { n: i + 1 })),
        h("div", { class: "row" },
          h("button", { class: "btn small ghost", type: "button", disabled: i === 0, onclick: () => move(i, i - 1) }, t("Move up")),
          h("button", { class: "btn small ghost", type: "button", disabled: sections.length === 1, onclick: () => { sections.splice(i, 1); draw(); } }, t("Remove")))),
      field(t("Heading (optional; without one the content is the embed text, up to 4096 chars, with one it's a field, up to 1024)"), s.title),
      field(t("Content"), s.content))));

    const add = h("button", { class: "btn small", type: "button", onclick: () => { sections.push(makeSection({})); draw(); } }, t("Add section"));
    container.append(field(t("Title (shown on the first embed)"), title), list, h("div", { class: "row" }, add, h("span", { class: "muted small" }, t("Each section is its own embed; Discord allows 10 per message."))));
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
    if (!preset) { view.append(h("p", { class: "error" }, t("Unknown preset."))); return; }
    const url = `/api/presets/${encodeURIComponent(kind)}/${encodeURIComponent(language)}`;
    const status = h("span");
    const preview = h("div", { class: "stack" });
    const show = (p) => {
      status.replaceChildren(p.customized ? badge(t("customized"), "accent") : badge(t("default")));
      preview.replaceChildren(...presetPreview(p).flat().filter(Boolean));
    };

    const form = h("div", { class: "stack" });
    const collect = kind === "rules" ? rulesForm(form, preset.data) : supportForm(form, preset.data);

    const previewBtn = h("button", { class: "btn", type: "button" }, t("Preview"));
    previewBtn.addEventListener("click", () => run(previewBtn, async () => {
      const draft = await api(`${url}/preview`, { method: "POST", body: { data: collect() } });
      preview.replaceChildren(h("p", { class: "muted small" }, t("Unsaved draft:")), ...presetPreview(draft).flat().filter(Boolean));
    }));
    const save = h("button", { class: "btn primary", type: "button" }, t("Save"));
    save.addEventListener("click", () => run(save, async () => {
      show(await api(url, { method: "PUT", body: { data: collect() } }));
    }, t("Preset saved")));
    const reset = h("button", { class: "btn ghost", type: "button" }, t("Reset to default"));
    reset.addEventListener("click", async () => {
      const ok = await dialog(t("Reset preset?"), h("p", {}, t("Replace {name} with the built-in default text. The current text is kept in the audit log.", { name: `${t(KINDS[kind])} (${LANGS[language] || language})` })), { confirmLabel: t("Reset"), danger: true });
      if (ok) await run(reset, async () => { await api(url, { method: "DELETE" }); await Panel.refresh(); }, t("Preset reset"));
    });

    view.append(
      h("p", {}, h("a", { href: "#/presets" }, t("← All presets"))),
      h("div", { class: "row" }, h("h1", {}, `${t(KINDS[kind])} · ${LANGS[language] || language}`), status),
      h("div", { class: "card stack" }, form, h("div", { class: "row" }, previewBtn, save, reset)),
      h("h2", {}, t("What the bot posts")),
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
        h("h1", {}, t("Message presets")),
        h("p", { class: "muted" }, t("Text for the rules and support-us messages (/rules setup, /supportus setup). Language buttons read these live; a message that's already posted keeps its old English text until it's posted again.")),
        h("div", { class: "tiles" }, presets.map((p) => h("a", { class: "tile", href: `#/presets/${p.kind}/${p.language}` },
          h("div", { class: "grow" },
            h("div", {}, `${t(KINDS[p.kind]) || p.kind} · ${LANGS[p.language] || p.language}`),
            h("div", { class: "muted small" }, t("{n} embeds", { n: p.embeds.length }))),
          p.customized ? badge(t("customized"), "accent") : badge(t("default")),
          h("span", { class: "go" }, t("Edit ›"))))));
    },
  });

  i18n({
    "Rules": "Reglas",
    "Support us": "Apóyanos",
    "Title link: {url}": "Enlace del título: {url}",
    "inline": "en línea",
    "Thumbnail: {url}": "Miniatura: {url}",
    "Image: {url}": "Imagen: {url}",
    "Section {n} (first embed, carries the title)": "Sección {n} (primer embed, lleva el título)",
    "Section {n}": "Sección {n}",
    "Move up": "Subir",
    "Remove": "Quitar",
    "Heading (optional; without one the content is the embed text, up to 4096 chars, with one it's a field, up to 1024)":
      "Encabezado (opcional; sin uno el contenido es el texto del embed, hasta 4096 caracteres; con uno es un campo, hasta 1024)",
    "Content": "Contenido",
    "Add section": "Agregar sección",
    "Title (shown on the first embed)": "Título (se muestra en el primer embed)",
    "Each section is its own embed; Discord allows 10 per message.": "Cada sección es su propio embed; Discord permite 10 por mensaje.",
    "Unknown preset.": "Preset desconocido.",
    "customized": "personalizado",
    "default": "por defecto",
    "Preview": "Vista previa",
    "Unsaved draft:": "Borrador sin guardar:",
    "Save": "Guardar",
    "Preset saved": "Preset guardado",
    "Reset to default": "Restablecer al valor por defecto",
    "Reset preset?": "¿Restablecer el preset?",
    "Replace {name} with the built-in default text. The current text is kept in the audit log.":
      "Reemplaza {name} con el texto original. El texto actual se conserva en el registro de auditoría.",
    "Reset": "Restablecer",
    "Preset reset": "Preset restablecido",
    "← All presets": "← Todos los presets",
    "What the bot posts": "Lo que publica el bot",
    "Message presets": "Presets de mensajes",
    "Text for the rules and support-us messages (/rules setup, /supportus setup). Language buttons read these live; a message that's already posted keeps its old English text until it's posted again.":
      "Texto de los mensajes de reglas y apóyanos (/rules setup, /supportus setup). Los botones de idioma leen esto en vivo; un mensaje ya publicado conserva su texto en inglés anterior hasta que se publique de nuevo.",
    "{n} embeds": "{n} embeds",
    "Edit ›": "Editar ›",
  });
})();
