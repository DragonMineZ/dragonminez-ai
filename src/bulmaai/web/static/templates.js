"use strict";

// Templates page (replaces "Default embeds"): reusable Components V2 cards (rules, support us, anything else)
// with optional English / Español / Português language buttons. Edited with the shared block editor.

(() => {
  const { h, api, run, badge, field, dialog, channelSelect, channelName, toast, t, i18n } = Panel;
  const LANGS = { en: "English", es: "Español", pt: "Português" };
  const clone = (x) => JSON.parse(JSON.stringify(x));

  const statusBadges = (tpl) => [
    tpl.builtin ? badge(t("built-in"), "accent") : badge(t("custom")),
    tpl.builtin && tpl.customized ? badge(t("customized"), "warn") : null,
    tpl.language_buttons ? badge(t("language buttons")) : null,
  ];

  // ---------- list ---------------------------------------------------------------------------------

  async function renderList(view) {
    const { templates } = await api("/api/templates");
    const name = h("input", { type: "text", maxlength: "60", placeholder: t("Template name"), "aria-label": t("Template name") });
    const create = h("button", { class: "btn primary", type: "button" }, t("Create template"));
    const submit = () => run(create, async () => {
      const title = name.value.trim();
      if (!title) throw new Error(t("Give the template a name."));
      const body = {
        name: title,
        language_buttons: false,
        languages: {
          en: { accent_color: "#F39C12", blocks: [{ type: "text", text: `## ${title}\n${t("Write your message here.")}` }] },
          es: null,
          pt: null,
        },
      };
      const tpl = await api("/api/templates", { method: "POST", body });
      Panel.go(`#/templates/${encodeURIComponent(tpl.id)}`);
    });
    create.addEventListener("click", submit);
    name.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });

    view.append(
      h("h1", {}, t("Templates")),
      h("p", { class: "muted" }, t("Reusable messages (rules, support us, …). Edit them with the same block editor as announcements, translate them, and post them to a channel. A template can show English / Español / Português buttons that give each member a private copy in their language.")),
      h("div", { class: "card row" }, name, create),
      templates.length
        ? h("div", { class: "tiles" }, templates.map((tpl) => h("a", { class: "tile", href: `#/templates/${encodeURIComponent(tpl.id)}` },
          h("div", { class: "grow" },
            h("strong", {}, tpl.name),
            h("div", { class: "row tight" }, statusBadges(tpl))),
          h("span", { class: "go" }, t("Edit")))))
        : h("div", { class: "empty" }, t("No templates yet.")));
  }

  // ---------- editor -------------------------------------------------------------------------------

  async function renderEdit(view, id) {
    const { templates } = await api("/api/templates");
    let tpl = templates.find((x) => x.id === id);
    if (!tpl) {
      view.append(h("p", { class: "error" }, t("Unknown template.")), h("a", { href: "#/templates" }, t("Back to templates")));
      return;
    }

    let langs = clone(tpl.languages);
    let cur = "en";
    let touched = false;
    const isDirty = () => touched || editor.dirty();

    const editor = Panel.cardEditor({ onChange: () => { touched = true; showStatus(); } });
    const nameInput = h("input", { type: "text", value: tpl.name, maxlength: "60", "aria-label": t("Template name") });
    const langCheck = h("input", { type: "checkbox", checked: tpl.language_buttons });
    const status = h("span", { class: "row" });
    const tabs = h("div", { class: "tabs", role: "tablist" });
    const langTools = h("div", { class: "row" });
    const emptyBox = h("div", { class: "card stack" });
    const editorBox = h("div", {}, editor.el);

    function showStatus() {
      status.replaceChildren(...[...statusBadges({ ...tpl, language_buttons: langCheck.checked }), isDirty() ? badge(t("unsaved changes"), "warn") : null].filter(Boolean));
    }

    nameInput.addEventListener("input", () => { touched = true; showStatus(); });
    langCheck.addEventListener("change", () => { touched = true; editor.setLanguageButtons(langCheck.checked); showStatus(); });

    // Pull whatever the editor is showing back into langs before switching or saving.
    function stash() {
      if (langs[cur] !== null) langs[cur] = editor.get();
    }

    function drawTabs() {
      tabs.replaceChildren(...Object.entries(LANGS).map(([code, label]) => h("button", {
        type: "button", role: "tab", "aria-current": String(code === cur),
        onclick: () => switchTo(code),
      }, label, langs[code] === null ? h("span", { class: "muted small" }, ` · ${t("uses English")}`) : null)));
    }

    function show() {
      const has = langs[cur] !== null;
      editorBox.hidden = !has;
      emptyBox.hidden = has;
      if (has) editor.set(langs[cur]);
      editor.setLanguageButtons(langCheck.checked);
      drawTabs();
      drawTools();
      if (!has) {
        emptyBox.replaceChildren(
          h("strong", {}, t("No {lang} version yet.", { lang: LANGS[cur] })),
          h("p", { class: "muted" }, t("Members who click {lang} will get the English text instead. You can leave it like this.", { lang: LANGS[cur] })),
          h("div", { class: "row" },
            h("button", { class: "btn primary", type: "button", onclick: () => copyEnglish(false) }, t("Copy English into {lang}", { lang: LANGS[cur] })),
            h("button", { class: "btn", type: "button", onclick: startBlank }, t("Start blank"))));
      }
    }

    function switchTo(code) {
      if (code === cur) return;
      stash();
      cur = code;
      show();
    }

    function copyEnglish(confirmFirst) {
      const apply = () => {
        stash();
        langs[cur] = clone(langs.en);
        touched = true;
        show();
        showStatus();
      };
      if (!confirmFirst) { apply(); return; }
      dialog(t("Replace {lang} with English?", { lang: LANGS[cur] }), h("p", {}, t("The current {lang} text will be overwritten (until you leave without saving).", { lang: LANGS[cur] })),
        { confirmLabel: t("Replace"), danger: true }).then((ok) => { if (ok) apply(); });
    }

    function startBlank() {
      langs[cur] = { accent_color: (langs.en && langs.en.accent_color) || null, blocks: [{ type: "text", text: "" }] };
      touched = true;
      show();
      showStatus();
    }

    function drawTools() {
      langTools.replaceChildren(...(cur === "en" ? [] : [
        h("button", { class: "btn small", type: "button", onclick: () => copyEnglish(true) }, t("Copy English into {lang}", { lang: LANGS[cur] })),
        langs[cur] !== null ? h("button", {
          class: "btn small ghost", type: "button",
          onclick: () => { langs[cur] = null; touched = true; show(); showStatus(); },
        }, t("Remove (fall back to English)")) : null,
      ].filter(Boolean)));
    }

    // ---- actions ----

    const save = h("button", { class: "btn primary", type: "button" }, t("Save"));
    save.addEventListener("click", () => run(save, async () => {
      const title = nameInput.value.trim();
      if (!title) throw new Error(t("Give the template a name."));
      const problems = langs[cur] === null ? [] : editor.issues();
      if (problems.length) throw new Error(problems[0]);
      stash();
      const saved = await api(`/api/templates/${encodeURIComponent(tpl.id)}`, {
        method: "PUT", body: { name: title, language_buttons: langCheck.checked, languages: langs },
      });
      tpl = saved;
      langs = clone(saved.languages);
      touched = false;
      nameInput.value = saved.name;
      langCheck.checked = saved.language_buttons;
      title_.textContent = saved.name;
      remove.disabled = tpl.builtin && !tpl.customized;
      show();
      showStatus();
    }, t("Saved")));

    const discard = h("button", { class: "btn ghost", type: "button" }, t("Discard changes"));
    discard.addEventListener("click", () => { touched = false; Panel.refresh(); });

    const remove = h("button", { class: "btn danger", type: "button" }, tpl.builtin ? t("Reset to default") : t("Delete"));
    remove.disabled = tpl.builtin && !tpl.customized;
    remove.addEventListener("click", async () => {
      const ok = await dialog(
        tpl.builtin ? t("Reset to the default text?") : t("Delete this template?"),
        h("p", {}, tpl.builtin
          ? t("Replaces every language of this template with the built-in default. Posts already in channels are not changed.")
          : t("The template is removed for good. Posts already in channels are not changed, but their language buttons stop working.")),
        { confirmLabel: tpl.builtin ? t("Reset") : t("Delete"), danger: true });
      if (!ok) return;
      await run(remove, async () => {
        await api(`/api/templates/${encodeURIComponent(tpl.id)}`, { method: "DELETE" });
        touched = false;
        if (tpl.builtin) await Panel.refresh(); else Panel.go("#/templates");
      }, tpl.builtin ? t("Reset to default") : t("Deleted"));
    });

    // ---- post to channel ----

    const channel = channelSelect(null, { types: ["text", "news"], includeNone: true });
    const post = h("button", { class: "btn primary", type: "button" }, t("Post"));
    const posted = h("div");
    post.addEventListener("click", async () => {
      if (!channel.value) { toast(t("Pick a channel first."), true); return; }
      const where = await channelName(channel.value);
      const ok = await dialog(t("Post {name}?", { name: tpl.name }), [
        h("p", {}, tpl.language_buttons
          ? t("Posts the saved English version in {where}, with English / Español / Português buttons under it.", { where })
          : t("Posts the saved English version in {where}.", { where })),
        isDirty() ? h("p", { class: "error" }, t("You have unsaved changes: they are NOT included. Save first if you want them posted.")) : null,
      ], { confirmLabel: t("Post") });
      if (!ok) return;
      const result = await run(post, () => api(`/api/templates/${encodeURIComponent(tpl.id)}/post`, { method: "POST", body: { channel_id: channel.value } }), t("Posted"));
      if (result) posted.replaceChildren(h("p", {}, t("Posted: "), h("a", { href: result.jump_url, target: "_blank", rel: "noopener noreferrer" }, result.jump_url)));
    });

    const title_ = h("h1", {}, tpl.name);
    const back = h("a", { href: "#/templates" }, t("← All templates"));
    back.addEventListener("click", async (e) => {
      if (!isDirty()) return;
      e.preventDefault();
      if (await dialog(t("Discard unsaved changes?"), h("p", {}, t("Your edits to this template haven't been saved.")), { confirmLabel: t("Discard"), danger: true })) {
        touched = false;
        Panel.go("#/templates");
      }
    });

    view.append(
      h("div", {}, back),
      h("div", { class: "row spread page-head" }, title_, status),
      h("div", { class: "card stack" },
        h("div", { class: "row" },
          h("div", { class: "grow-field" }, h("label", {}, t("Template name")), nameInput),
          h("label", { class: "row check" }, langCheck, t("Language buttons"))),
        h("p", { class: "muted small" }, t("Language buttons add English / Español / Português under the post. Clicking one gives that member a private copy in the language; the public post stays English. A language left empty falls back to English.")),
        tabs,
        langTools,
        emptyBox,
        editorBox,
        h("div", { class: "row" }, save, discard, remove)),
      h("div", { class: "card stack" },
        h("strong", {}, t("Post to a channel")),
        h("p", { class: "muted" }, t("Posting sends the saved version (save first). A copy already in a channel isn't changed: delete it and post again.")),
        h("div", { class: "row end-align" }, field(t("Channel"), channel), post),
        posted));

    show();
    touched = false;
    showStatus();
  }

  Panel.page({
    id: "templates",
    title: "Templates",
    perm: "presets.edit",
    group: "Community",
    async render(view, args) {
      if (args[0]) await renderEdit(view, args[0]);
      else await renderList(view);
    },
  });

  i18n({
    "Templates": "Plantillas",
    "built-in": "integrada",
    "custom": "personalizada",
    "customized": "modificada",
    "language buttons": "botones de idioma",
    "Template name": "Nombre de la plantilla",
    "Create template": "Crear plantilla",
    "Give the template a name.": "Ponle un nombre a la plantilla.",
    "Write your message here.": "Escribe tu mensaje aquí.",
    "Reusable messages (rules, support us, …). Edit them with the same block editor as announcements, translate them, and post them to a channel. A template can show English / Español / Português buttons that give each member a private copy in their language.":
      "Mensajes reutilizables (reglas, apóyanos, …). Edítalos con el mismo editor de bloques que los anuncios, tradúcelos y publícalos en un canal. Una plantilla puede mostrar botones English / Español / Português que dan a cada miembro una copia privada en su idioma.",
    "Edit": "Editar",
    "No templates yet.": "Aún no hay plantillas.",
    "Unknown template.": "Plantilla desconocida.",
    "Back to templates": "Volver a las plantillas",
    "← All templates": "← Todas las plantillas",
    "uses English": "usa inglés",
    "unsaved changes": "cambios sin guardar",
    "No {lang} version yet.": "Aún no hay versión en {lang}.",
    "Members who click {lang} will get the English text instead. You can leave it like this.":
      "Los miembros que pulsen {lang} recibirán el texto en inglés. Puedes dejarlo así.",
    "Copy English into {lang}": "Copiar el inglés a {lang}",
    "Start blank": "Empezar en blanco",
    "Replace {lang} with English?": "¿Reemplazar {lang} con el inglés?",
    "The current {lang} text will be overwritten (until you leave without saving).":
      "El texto actual en {lang} se sobrescribirá (hasta que salgas sin guardar).",
    "Replace": "Reemplazar",
    "Remove (fall back to English)": "Quitar (usar inglés)",
    "Save": "Guardar",
    "Saved": "Guardado",
    "Discard changes": "Descartar cambios",
    "Discard": "Descartar",
    "Reset to default": "Restablecer al predeterminado",
    "Delete": "Eliminar",
    "Deleted": "Eliminada",
    "Reset": "Restablecer",
    "Reset to the default text?": "¿Restablecer el texto predeterminado?",
    "Delete this template?": "¿Eliminar esta plantilla?",
    "Replaces every language of this template with the built-in default. Posts already in channels are not changed.":
      "Reemplaza todos los idiomas de esta plantilla por el predeterminado. Los mensajes ya publicados en canales no cambian.",
    "The template is removed for good. Posts already in channels are not changed, but their language buttons stop working.":
      "La plantilla se elimina definitivamente. Los mensajes ya publicados no cambian, pero sus botones de idioma dejan de funcionar.",
    "Pick a channel first.": "Primero elige un canal.",
    "Post {name}?": "¿Publicar {name}?",
    "Posts the saved English version in {where}, with English / Español / Português buttons under it.":
      "Publica la versión en inglés guardada en {where}, con botones English / Español / Português debajo.",
    "Posts the saved English version in {where}.": "Publica la versión en inglés guardada en {where}.",
    "You have unsaved changes: they are NOT included. Save first if you want them posted.":
      "Tienes cambios sin guardar: NO se incluyen. Guarda primero si quieres publicarlos.",
    "Post": "Publicar",
    "Posted": "Publicado",
    "Posted: ": "Publicado: ",
    "Discard unsaved changes?": "¿Descartar los cambios sin guardar?",
    "Your edits to this template haven't been saved.": "Tus cambios en esta plantilla no se han guardado.",
    "Language buttons": "Botones de idioma",
    "Language buttons add English / Español / Português under the post. Clicking one gives that member a private copy in the language; the public post stays English. A language left empty falls back to English.":
      "Los botones de idioma añaden English / Español / Português bajo el mensaje. Al pulsar uno, ese miembro recibe una copia privada en ese idioma; el mensaje público sigue en inglés. Un idioma vacío usa el inglés.",
    "Post to a channel": "Publicar en un canal",
    "Posting sends the saved version (save first). A copy already in a channel isn't changed: delete it and post again.":
      "Se publica la versión guardada (guarda primero). Una copia que ya esté en un canal no cambia: bórrala y publica de nuevo.",
    "Channel": "Canal",
  });
})();
