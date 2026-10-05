"use strict";

// Default embeds page: pick an embed (rules / support us) and a language from the dropdowns, edit it
// on the left, watch the Discord preview update on the right, save, then post it to a channel.

(() => {
  const { h, api, run, badge, field, dialog, channelSelect, channelName, toast, t, i18n } = Panel;
  const LANGS = { en: "English", es: "Español", pt: "Português" };
  const KINDS = {
    rules: { label: "Server rules", help: "The rules message with English / Español / Português buttons." },
    support: { label: "Support us", help: "The Patreon / GitHub / server boosting message with language buttons." },
  };
  // Support-us fields, grouped the way they appear in the message. [key, label, long text?]
  const SUPPORT_GROUPS = [
    ["Intro", [["description", "Intro text", true]]],
    ["Patreon perks", [["perks_title", "Heading"], ["perks_value", "Text", true]]],
    ["Development", [["development_title", "Heading"], ["development_value", "Text", true]]],
    ["Credits", [["credits_title", "Heading"], ["credits_value", "Text", true]]],
    ["Community", [["community_title", "Heading"], ["community_value", "Text", true]]],
    ["Server boosting (second embed)", [
      ["boosting_title", "Title"], ["boosting_description", "Text", true],
      ["boost_tier1_title", "Tier 1 heading"], ["boost_tier1_value", "Tier 1 text", true],
      ["boost_tier2_title", "Tier 2 heading"], ["boost_tier2_value", "Tier 2 text", true],
      ["boost_tier3_title", "Tier 3 heading"], ["boost_tier3_value", "Tier 3 text", true],
      ["boosting_footer", "Footer"]]],
    ["Buttons", [["patreon_label", "Patreon button label"], ["github_label", "GitHub button label"]]],
  ];

  const hex = (n) => (typeof n === "number" ? `#${n.toString(16).padStart(6, "0")}` : "");

  // embed.to_dict() (what the bot sends) -> the shape Panel.messagePreview draws.
  function fromDiscord(e) {
    return {
      title: e.title || "", url: e.url || "", description: e.description || "", color: hex(e.color),
      fields: (e.fields || []).map((f) => ({ name: f.name, value: f.value, inline: Boolean(f.inline) })),
      footer: (e.footer && e.footer.text) || "", image_url: (e.image && e.image.url) || "",
      thumbnail_url: (e.thumbnail && e.thumbnail.url) || "", author_name: (e.author && e.author.name) || "",
    };
  }

  function preview(preset) {
    return Panel.messagePreview({ content: "", embeds: preset.embeds.map(fromDiscord), buttons: preset.buttons });
  }

  function counter(input, limit) {
    const el = h("div", { class: "muted small" });
    const update = () => {
      const max = typeof limit === "function" ? limit() : limit;
      el.textContent = `${input.value.length} / ${max}`;
      el.classList.toggle("error", input.value.length > max);
    };
    input.addEventListener("input", update);
    update();
    return { el, update };
  }

  function rulesForm(container, data, onChange) {
    const title = h("input", { type: "text", value: data.title, maxlength: "256" });
    const list = h("div", { class: "stack" });
    const makeSection = (s) => ({
      title: h("input", { type: "text", value: s.title || "", maxlength: "256", placeholder: t("Optional") }),
      content: h("textarea", { rows: "8", value: s.content || "" }),
      open: !s.content,
    });
    const sections = data.sections.map(makeSection);
    const move = (from, to) => { sections.splice(to, 0, sections.splice(from, 1)[0]); draw(); onChange(); };

    const draw = () => list.replaceChildren(...sections.map((s, i) => {
      const count = counter(s.content, () => (s.title.value.trim() ? 1024 : 4096));
      s.title.oninput = () => { count.update(); onChange(); };
      const name = () => s.title.value.trim() || t("(no heading)");
      const summary = h("summary", {}, t("Section {n}", { n: i + 1 }), " · ", h("span", { class: "muted" }, name()));
      s.title.addEventListener("input", () => { summary.lastChild.textContent = name(); });
      const card = h("details", { class: "card section-card", open: s.open || undefined },
        summary,
        h("div", { class: "stack" },
          field(t("Heading"), s.title),
          h("p", { class: "muted small" }, t("With a heading the section is a field (1024 characters max); without one it's the embed's main text (4096 max).")),
          field(t("Text"), s.content),
          count.el,
          h("div", { class: "row" },
            h("button", { class: "btn small ghost", type: "button", disabled: i === 0, onclick: () => move(i, i - 1) }, t("Move up")),
            h("button", { class: "btn small ghost", type: "button", disabled: i === sections.length - 1, onclick: () => move(i, i + 1) }, t("Move down")),
            h("button", { class: "btn small danger", type: "button", disabled: sections.length === 1, onclick: () => { sections.splice(i, 1); draw(); onChange(); } }, t("Delete section")))));
      card.addEventListener("toggle", () => { s.open = card.open; });
      return card;
    }));

    const add = h("button", { class: "btn small", type: "button", onclick: () => { sections.push(makeSection({})); draw(); onChange(); } }, t("Add section"));
    container.addEventListener("input", onChange);
    container.append(
      field(t("Title (top of the message)"), title),
      list,
      h("div", { class: "row" }, add, h("span", { class: "muted small" }, t("Each section is its own embed; Discord allows 10 per message."))));
    draw();
    return () => ({
      title: title.value,
      sections: sections.map((s) => ({ title: s.title.value.trim() ? s.title.value : null, content: s.content.value })),
    });
  }

  function supportForm(container, data, onChange) {
    const inputs = {};
    container.addEventListener("input", onChange);
    for (const [group, fields] of SUPPORT_GROUPS) {
      container.append(h("details", { class: "card section-card", open: group === "Intro" || undefined },
        h("summary", {}, t(group)),
        h("div", { class: "stack" }, fields.map(([key, label, long]) => {
          inputs[key] = long ? h("textarea", { rows: "5", value: data[key] }) : h("input", { type: "text", value: data[key] });
          return field(t(label), inputs[key]);
        }))));
    }
    return () => Object.fromEntries(Object.entries(inputs).map(([key, input]) => [key, input.value]));
  }

  function postCard(kind) {
    const channel = channelSelect(null, { types: ["text", "news"], includeNone: true });
    const post = h("button", { class: "btn primary", type: "button" }, t("Post"));
    post.addEventListener("click", async () => {
      if (!channel.value) { toast(t("Pick a channel first."), true); return; }
      const where = await channelName(channel.value);
      const ok = await dialog(t("Post {name}?", { name: t(KINDS[kind].label) }),
        h("p", {}, t("Posts the saved English version in #{channel}. Members can switch language with the buttons under it.", { channel: where })),
        { confirmLabel: t("Post"), danger: false });
      if (!ok) return;
      const result = await run(post, () => api(`/api/presets/${kind}/post`, { method: "POST", body: { channel_id: channel.value } }), t("Posted"));
      if (result) toast(t("Posted in #{channel}", { channel: where }));
    });
    return h("div", { class: "card stack" },
      h("strong", {}, t("Post to a channel")),
      h("p", { class: "muted" }, t("Posting sends the saved version (save first). An old copy already in a channel isn't changed: delete it and post again.")),
      h("div", { class: "embed-picker" }, field(t("Channel"), channel), post));
  }

  async function render(view, kind, language) {
    const { presets } = await api("/api/presets");
    const preset = presets.find((p) => p.kind === kind && p.language === language);
    if (!preset) { view.append(h("p", { class: "error" }, t("Unknown embed."))); return; }
    const url = `/api/presets/${kind}/${language}`;

    let dirty = false;
    const status = h("span", { class: "row" });
    const showStatus = (p) => status.replaceChildren(...[
      p.customized ? badge(t("edited"), "accent") : badge(t("default text")),
      dirty ? badge(t("unsaved changes"), "warn") : null].filter(Boolean));

    const previewBox = h("div", { class: "stack" });
    const problem = h("p", { class: "error", hidden: true });
    const showPreview = (p) => previewBox.replaceChildren(preview(p));

    // Pickers: switching asks first when there are unsaved edits.
    const kindSelect = h("select", {}, Object.entries(KINDS).map(([id, k]) => h("option", { value: id }, t(k.label))));
    const langSelect = h("select", {}, Object.entries(LANGS).map(([id, label]) => h("option", { value: id }, label)));
    kindSelect.value = kind;
    langSelect.value = language;
    const switchTo = async () => {
      if (dirty && !(await dialog(t("Discard unsaved changes?"), h("p", {}, t("Your edits to this embed haven't been saved.")), { confirmLabel: t("Discard"), danger: true }))) {
        kindSelect.value = kind;
        langSelect.value = language;
        return;
      }
      dirty = false;
      Panel.go(`#/presets/${kindSelect.value}/${langSelect.value}`);
    };
    kindSelect.addEventListener("change", switchTo);
    langSelect.addEventListener("change", switchTo);

    // Live preview: re-render the draft through the bot's own builder after a short pause in typing.
    let timer;
    let collect = () => preset.data;
    const onChange = () => {
      dirty = true;
      showStatus(preset);
      clearTimeout(timer);
      timer = setTimeout(async () => {
        try {
          showPreview(await api(`${url}/preview`, { method: "POST", body: { data: collect() } }));
          problem.hidden = true;
        } catch (error) {
          problem.textContent = error.message;
          problem.hidden = false;
        }
      }, 400);
    };
    const form = h("div", { class: "stack" });
    collect = kind === "rules" ? rulesForm(form, preset.data, onChange) : supportForm(form, preset.data, onChange);

    const save = h("button", { class: "btn primary", type: "button" }, t("Save"));
    save.addEventListener("click", () => run(save, async () => {
      const saved = await api(url, { method: "PUT", body: { data: collect() } });
      dirty = false;
      Object.assign(preset, saved);
      showStatus(saved);
      showPreview(saved);
      problem.hidden = true;
    }, t("Saved")));
    const discard = h("button", { class: "btn ghost", type: "button" }, t("Discard changes"));
    discard.addEventListener("click", () => { dirty = false; Panel.refresh(); });
    const reset = h("button", { class: "btn ghost", type: "button" }, t("Reset to default"));
    reset.addEventListener("click", async () => {
      const ok = await dialog(t("Reset to the default text?"), h("p", {}, t("Replaces this embed's text with the built-in default. The current text is kept in the audit log.")), { confirmLabel: t("Reset"), danger: true });
      if (ok) await run(reset, async () => { await api(url, { method: "DELETE" }); dirty = false; await Panel.refresh(); }, t("Reset to default"));
    });

    view.append(
      h("h1", {}, t("Default embeds")),
      h("div", { class: "card stack" },
        h("div", { class: "embed-picker" }, field(t("Embed"), kindSelect), field(t("Language"), langSelect), status),
        h("p", { class: "muted small" }, t(KINDS[kind].help))),
      postCard(kind),
      h("div", { class: "embed-workbench" },
        h("div", { class: "stack" },
          form,
          h("div", { class: "card row" }, save, discard, reset)),
        h("div", { class: "stack preview-pane" },
          h("h2", {}, t("Preview")),
          problem,
          previewBox,
          h("p", { class: "muted small" }, t("Buttons are shown as they'll appear; they only work in Discord.")))));
    showStatus(preset);
    showPreview(preset);
  }

  Panel.page({
    id: "presets",
    title: "Default embeds",
    perm: "presets.edit",
    group: "Community",
    async render(view, args) {
      const kind = KINDS[args[0]] ? args[0] : "rules";
      const language = LANGS[args[1]] ? args[1] : "en";
      await render(view, kind, language);
    },
  });

  i18n({
    "Default embeds": "Embeds predeterminados",
    "Server rules": "Reglas del servidor",
    "Support us": "Apóyanos",
    "The rules message with English / Español / Português buttons.": "El mensaje de reglas con botones English / Español / Português.",
    "The Patreon / GitHub / server boosting message with language buttons.": "El mensaje de Patreon / GitHub / boosts del servidor con botones de idioma.",
    "Intro": "Introducción",
    "Intro text": "Texto de introducción",
    "Patreon perks": "Beneficios de Patreon",
    "Development": "Desarrollo",
    "Credits": "Créditos",
    "Community": "Comunidad",
    "Server boosting (second embed)": "Boosts del servidor (segundo embed)",
    "Buttons": "Botones",
    "Heading": "Encabezado",
    "Text": "Texto",
    "Title": "Título",
    "Tier 1 heading": "Encabezado nivel 1",
    "Tier 1 text": "Texto nivel 1",
    "Tier 2 heading": "Encabezado nivel 2",
    "Tier 2 text": "Texto nivel 2",
    "Tier 3 heading": "Encabezado nivel 3",
    "Tier 3 text": "Texto nivel 3",
    "Footer": "Pie",
    "Patreon button label": "Texto del botón de Patreon",
    "GitHub button label": "Texto del botón de GitHub",
    "Optional": "Opcional",
    "(no heading)": "(sin encabezado)",
    "Section {n}": "Sección {n}",
    "With a heading the section is a field (1024 characters max); without one it's the embed's main text (4096 max).":
      "Con encabezado la sección es un campo (máx. 1024 caracteres); sin él es el texto principal del embed (máx. 4096).",
    "Move up": "Subir",
    "Move down": "Bajar",
    "Delete section": "Eliminar sección",
    "Add section": "Agregar sección",
    "Title (top of the message)": "Título (arriba del mensaje)",
    "Each section is its own embed; Discord allows 10 per message.": "Cada sección es su propio embed; Discord permite 10 por mensaje.",
    "Post": "Publicar",
    "Post {name}?": "¿Publicar {name}?",
    "Posts the saved English version in #{channel}. Members can switch language with the buttons under it.":
      "Publica la versión en inglés guardada en #{channel}. Los miembros pueden cambiar de idioma con los botones de abajo.",
    "Posted": "Publicado",
    "Posted in #{channel}": "Publicado en #{channel}",
    "Pick a channel first.": "Primero elige un canal.",
    "Post to a channel": "Publicar en un canal",
    "Posting sends the saved version (save first). An old copy already in a channel isn't changed: delete it and post again.":
      "Se publica la versión guardada (guarda primero). Una copia anterior en un canal no cambia: bórrala y publica de nuevo.",
    "Channel": "Canal",
    "Unknown embed.": "Embed desconocido.",
    "edited": "editado",
    "default text": "texto predeterminado",
    "unsaved changes": "cambios sin guardar",
    "Discard unsaved changes?": "¿Descartar los cambios sin guardar?",
    "Your edits to this embed haven't been saved.": "Tus cambios en este embed no se han guardado.",
    "Discard": "Descartar",
    "Save": "Guardar",
    "Saved": "Guardado",
    "Discard changes": "Descartar cambios",
    "Reset to default": "Restablecer al predeterminado",
    "Reset to the default text?": "¿Restablecer el texto predeterminado?",
    "Replaces this embed's text with the built-in default. The current text is kept in the audit log.":
      "Reemplaza el texto de este embed por el predeterminado. El texto actual se conserva en el registro de auditoría.",
    "Reset": "Restablecer",
    "Embed": "Embed",
    "Language": "Idioma",
    "Preview": "Vista previa",
    "Buttons are shown as they'll appear; they only work in Discord.": "Los botones se ven como aparecerán; solo funcionan en Discord.",
  });
})();
