"use strict";

// Support info page: known issues (temporary) and custom answers (permanent) the support AI reads.
// Suggestions from the AI are approved or discarded here, the same as from the Discord review card.

(() => {
  const { h, api, run, time, badge, table, field, dialog, toast, t, i18n } = Panel;
  const TITLE_MAX = 100;
  const BODY_MAX = 1500;
  const TABS = [["active", "Active"], ["suggested", "Suggested"], ["closed", "Removed & discarded"]];

  let tab = "active";

  function sourceCell(entry) {
    if (entry.source !== "ai") return t("manual");
    return entry.ticket_id ? t("AI · ticket #{id}", { id: entry.ticket_id }) : t("AI");
  }

  function expiryCell(entry) {
    if (!entry.expires_at) return h("span", { class: "muted" }, "—");
    return h("span", {}, time(entry.expires_at), entry.expired ? [" ", badge(t("expired"), "warn")] : null);
  }

  function statusBadge(entry) {
    if (entry.status === "removed") return badge(t("removed"), "danger");
    if (entry.status === "discarded") return badge(t("discarded"));
    return null;
  }

  // Local end of the chosen day, so "expires 20 Oct" lasts through the 20th for whoever typed it.
  function endOfDay(value) {
    const [year, month, day] = value.split("-").map(Number);
    return new Date(year, month - 1, day, 23, 59, 59).toISOString();
  }

  function localDate(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }

  // The add/edit card. entry null = add. onSaved reloads the list.
  function form(entry, onClose, onSaved) {
    const kind = h("select", {},
      h("option", { value: "temporary" }, t("Temporary (known issue, posted daily)")),
      h("option", { value: "permanent" }, t("Permanent (custom answer)")));
    const title = h("input", { type: "text", maxlength: String(TITLE_MAX) });
    const body = h("textarea", { maxlength: String(BODY_MAX), rows: "6" });
    const expires = h("input", { type: "date" });
    const counter = h("span", { class: "muted small" });
    const error = h("p", { class: "error", hidden: true });
    if (entry) {
      kind.value = entry.kind;
      title.value = entry.title;
      body.value = entry.body;
      expires.value = localDate(entry.expires_at);
    }
    const sync = () => {
      expires.disabled = kind.value !== "temporary";
      if (expires.disabled) expires.value = "";
      counter.textContent = `${body.value.length}/${BODY_MAX}`;
    };
    kind.addEventListener("change", sync);
    body.addEventListener("input", sync);
    sync();

    const save = h("button", { class: "btn primary", type: "button" }, entry ? t("Save") : t("Add"));
    save.addEventListener("click", async () => {
      error.hidden = true;
      save.disabled = true;
      try {
        await api(entry ? `/api/support-info/${entry.id}` : "/api/support-info", {
          method: entry ? "PUT" : "POST",
          body: { kind: kind.value, title: title.value, body: body.value, expires_at: expires.value ? endOfDay(expires.value) : null },
        });
        toast(entry ? t("Saved") : t("Added"));
        onClose();
        await onSaved();
      } catch (e) {
        error.textContent = e.message;
        error.hidden = false;
      } finally {
        save.disabled = false;
      }
    });
    const el = h("div", { class: "card stack" },
      h("strong", {}, entry ? t("Edit #{id}", { id: entry.id }) : t("Add support info")),
      field(t("Kind"), kind),
      field(t("Title"), title),
      h("div", {}, field(t("Text"), body), counter),
      field(t("Expires (optional, temporary only)"), expires),
      error,
      h("div", { class: "row" }, save, h("button", { class: "btn ghost", type: "button", onclick: onClose }, t("Cancel"))));
    return { el, focus: () => title.focus() };
  }

  async function render(view) {
    const holder = h("div", {});
    const box = h("div", {});
    const tabs = h("div", { class: "tabs", role: "tablist" });
    const expanded = new Set();
    let entries = [];

    function drawTabs() {
      tabs.replaceChildren(...TABS.map(([id, label]) => h("button", {
        type: "button", role: "tab", "aria-current": String(id === tab),
        onclick: () => { tab = id; run(null, reload); },
      }, t(label))));
    }

    async function reload() {
      entries = (await api(`/api/support-info?status=${tab}`)).entries;
      drawTabs();
      draw();
    }

    function openForm(entry) {
      const f = form(entry, () => box.replaceChildren(), reload);
      box.replaceChildren(f.el);
      f.focus();
    }

    async function act(button, path, successMessage) {
      await run(button, async () => { await api(path, { method: "POST" }); await reload(); }, successMessage);
    }

    async function remove(entry) {
      const ok = await dialog(t("Remove #{id}?", { id: entry.id }),
        h("p", {}, t("The support AI stops using it and it is no longer posted. It stays in Removed & discarded.")),
        { confirmLabel: t("Remove"), danger: true });
      if (ok) await act(null, `/api/support-info/${entry.id}/remove`, t("Removed"));
    }

    function actions(entry) {
      const btn = (label, cls, fn) => h("button", {
        class: `btn small ${cls}`, type: "button",
        onclick: (event) => { event.stopPropagation(); fn(event.currentTarget); },
      }, label);
      if (entry.status === "suggested") {
        return h("div", { class: "row tight" },
          btn(t("Approve"), "primary", (b) => act(b, `/api/support-info/${entry.id}/approve`, t("Approved"))),
          btn(t("Edit"), "", () => openForm(entry)),
          btn(t("Discard"), "ghost", (b) => act(b, `/api/support-info/${entry.id}/discard`, t("Discarded"))));
      }
      if (entry.status === "active") {
        return h("div", { class: "row tight" }, btn(t("Edit"), "", () => openForm(entry)), btn(t("Remove"), "danger", () => remove(entry)));
      }
      return null;
    }

    function draw() {
      holder.replaceChildren(table([
        { label: "#", render: (e) => String(e.id) },
        { label: t("Kind"), render: (e) => badge(t(e.kind), e.kind === "temporary" ? "warn" : "accent") },
        { label: t("Title"), render: (e) => h("span", {}, h("strong", {}, e.title), " ", statusBadge(e)) },
        { label: t("Text"), render: (e) => (expanded.has(e.id) || e.body.length <= 120 ? e.body : `${e.body.slice(0, 120)}…`) },
        { label: t("Expires"), render: expiryCell },
        { label: t("Source"), render: sourceCell },
        { label: t("Updated"), render: (e) => time(e.updated_at) },
        { label: "", render: actions },
      ], entries, {
        empty: t("Nothing here."),
        hint: t("Click a row to show the full text."),
        onRowClick: (e) => { if (expanded.has(e.id)) expanded.delete(e.id); else expanded.add(e.id); draw(); },
      }));
    }

    view.append(
      h("h1", {}, t("Support info")),
      h("p", { class: "muted" }, t("Known issues and custom answers for the support AI. Active entries are read by the AI; active temporary ones are also posted publicly every 24 hours until they expire or are removed.")),
      h("div", { class: "row" }, h("button", { class: "btn primary", type: "button", onclick: () => openForm(null) }, t("Add"))),
      box, tabs, holder);
    await reload();
  }

  Panel.page({ id: "support-info", title: "Support info", perm: "support_info.edit", render });

  i18n({
    "Support info": "Info de soporte",
    "Active": "Activas",
    "Suggested": "Sugeridas",
    "Removed & discarded": "Eliminadas y descartadas",
    "Known issues and custom answers for the support AI. Active entries are read by the AI; active temporary ones are also posted publicly every 24 hours until they expire or are removed.":
      "Problemas conocidos y respuestas personalizadas para la IA de soporte. La IA lee las entradas activas; las temporales activas también se publican cada 24 horas hasta que caducan o se eliminan.",
    "Temporary (known issue, posted daily)": "Temporal (problema conocido, se publica a diario)",
    "Permanent (custom answer)": "Permanente (respuesta personalizada)",
    "Kind": "Tipo",
    "Text": "Texto",
    "Source": "Origen",
    "Updated": "Actualizado",
    "temporary": "temporal",
    "permanent": "permanente",
    "manual": "manual",
    "AI": "IA",
    "AI · ticket #{id}": "IA · ticket #{id}",
    "expired": "caducada",
    "removed": "eliminada",
    "discarded": "descartada",
    "Add": "Añadir",
    "Added": "Añadida",
    "Add support info": "Añadir info de soporte",
    "Edit #{id}": "Editar #{id}",
    "Expires (optional, temporary only)": "Caduca (opcional, solo temporales)",
    "Click a row to show the full text.": "Pulsa una fila para ver el texto completo.",
    "Remove #{id}?": "¿Eliminar #{id}?",
    "The support AI stops using it and it is no longer posted. It stays in Removed & discarded.":
      "La IA de soporte deja de usarla y ya no se publica. Se queda en Eliminadas y descartadas.",
    "Remove": "Eliminar",
    "Removed": "Eliminada",
    "Approve": "Aprobar",
    "Approved": "Aprobada",
    "Discarded": "Descartada",
    "Edit": "Editar",
    "Discard": "Descartar",
    "Save": "Guardar",
    "Saved": "Guardado",
    "Title": "Título",
    "Expires": "Caduca",
  });
})();
