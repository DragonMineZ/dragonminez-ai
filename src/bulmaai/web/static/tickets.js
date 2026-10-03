"use strict";

(() => {
  const { h, api, run, can, time, user, badge, table, go, dialog, t } = Panel;

  Panel.i18n({
    "Msgs": "Msjs",
    "Tickets": "Tickets",
    "Open tickets ({count})": "Tickets abiertos ({count})",
    "Channel": "Canal",
    "Status": "Estado",
    "Category": "Categoría",
    "Claimed by": "Reclamado por",
    "open": "abierto",
    "closed": "cerrado",
    "Game-Breaking Bug": "Bug que rompe el juego",
    "Contributing to the Mod": "Contribuir al mod",
    "Other": "Otro",
    "Requester": "Solicitante",
    "Opened": "Abierto",
    "AI": "IA",
    "on": "encendido",
    "off": "apagado",
    "Turn AI off": "Apagar IA",
    "Turn AI on": "Encender IA",
    "AI {status} in #{channel}": "IA {status} en #{channel}",
    "disabled": "deshabilitada",
    "enabled": "habilitada",
    "Refresh": "Actualizar",
    "No open tickets.": "Sin tickets abiertos.",
    "ai_ticket_category_id isn't set, so no channel counts as a ticket.": "ai_ticket_category_id no está configurado, así que ningún canal cuenta como un ticket.",
    "The AI tickets cog isn't loaded; toggles are saved and apply when it loads.": "El cog de tickets de IA no está cargado; los cambios se guardan y se aplican cuando se carga.",
    "Transcripts": "Transcripciones",
    "Title, problem, tag, channel or requester ID…": "Título, problema, etiqueta, canal o ID del solicitante…",
    "Search transcripts": "Buscar transcripciones",
    "Closed": "Cerrado",
    "Title": "Título",
    "Outcome": "Resultado",
    "Tags": "Etiquetas",
    "solved": "resuelto",
    "unresolved": "sin resolver",
    "No transcripts match.": "Sin transcripciones que coincidan.",
    "No transcripts yet.": "Sin transcripciones aún.",
    "Previous": "Anterior",
    "Next": "Siguiente",
    "Page {page}": "Página {page}",
    "Search": "Buscar",
    "← Back to tickets": "← Volver a tickets",
    "Closed by": "Cerrado por",
    "AI confidence": "Confianza de IA",
    "n/a": "n/a",
    "{confidence}%": "{confidence}%",
    "Messages": "Mensajes",
    "AI knowledge": "Conocimiento de IA",
    "added": "añadido",
    "not added": "no añadido",
    "Problem": "Problema",
    "Resolution": "Resolución",
    "Transcript": "Transcripción",
    "(empty)": "(vacío)",
    "Open the HTML transcript": "Abrir la transcripción HTML",
    "Kept permanently.": "Se conserva de forma permanente.",
    "Expires": "Caduca",
    "Keep permanently": "Conservar permanentemente",
    "Expire normally": "Dejar que caduque",
    "Delete web page": "Eliminar página web",
    "Delete transcript": "Eliminar transcripción",
    "Delete the web page?": "¿Eliminar la página web?",
    "The link stops working immediately. The text record stays in the panel.": "El enlace deja de funcionar al instante. El registro de texto permanece en el panel.",
    "Delete this transcript?": "¿Eliminar esta transcripción?",
    "This removes the summary, the text and the web page. It can't be undone.": "Esto elimina el resumen, el texto y la página web. No se puede deshacer.",
    "Kept permanently": "Conservada permanentemente",
    "Back on the normal expiry": "De nuevo con caducidad normal",
    "Web page deleted": "Página web eliminada",
    "Transcript deleted": "Transcripción eliminada",
    "There is no web page for this transcript: it expired, was deleted, or the ticket was closed before hosted transcripts existed. The plain text is below.": "No hay página web para esta transcripción: caducó, se eliminó o el ticket se cerró antes de existir las transcripciones alojadas. El texto plano está abajo.",
  });

  function discordLink(url, label) {
    return h("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, label);
  }

  const CATEGORY_NAMES = { bug: "Game-Breaking Bug", contribute: "Contributing to the Mod", other: "Other" };

  function outcome(resolved) {
    return resolved ? badge(t("solved"), "ok") : badge(t("unresolved"), "warn");
  }

  async function openTickets(card) {
    const data = await api("/api/tickets");
    const reload = () => openTickets(card);
    const toggle = (ticket) => {
      const button = h("button", { class: "btn small", type: "button" }, ticket.ai_enabled ? t("Turn AI off") : t("Turn AI on"));
      button.addEventListener("click", () => run(button, async () => {
        await api(`/api/tickets/${ticket.id}/ai`, { method: "POST", body: { enabled: !ticket.ai_enabled } });
        await reload();
      }, t("AI {status} in #{channel}", { status: ticket.ai_enabled ? t("disabled") : t("enabled"), channel: ticket.name })));
      return button;
    };
    const columns = [
      { label: t("Channel"), render: (t) => discordLink(t.url, `#${t.name}`) },
      { label: t("Category"), render: (row) => (row.category ? t(CATEGORY_NAMES[row.category] || row.category) : "—") },
      { label: t("Status"), render: (row) => (row.status ? badge(t(row.status), row.status === "open" ? "ok" : "warn") : "—") },
      { label: t("Claimed by"), render: (row) => (row.claimed_by ? user(row.claimed_by) : "—") },
      { label: t("Requester"), render: (t) => user(t.requester) },
      { label: t("Opened"), render: (t) => time(t.created_at) },
      { label: t("AI"), render: (row) => (row.ai_enabled ? badge(t("on"), "ok") : badge(t("off"), "danger")) },
    ];
    if (can("tickets.manage")) columns.push({ label: "", render: toggle });
    const notes = [];
    if (!data.category_configured) notes.push(t("ai_ticket_category_id isn't set, so no channel counts as a ticket."));
    if (!data.cog_loaded) notes.push(t("The AI tickets cog isn't loaded; toggles are saved and apply when it loads."));
    card.replaceChildren(
      h("div", { class: "row spread" }, h("h2", {}, t("Open tickets ({count})", { count: data.tickets.length })),
        h("button", { class: "btn small ghost", type: "button", onclick: reload }, t("Refresh"))),
      ...notes.map((n) => h("p", { class: "muted small" }, n)),
      table(columns, data.tickets, { empty: t("No open tickets.") }));
  }

  function transcriptList(card) {
    const search = h("input", { type: "search", placeholder: t("Title, problem, tag, channel or requester ID…"), "aria-label": t("Search transcripts"), size: "40" });
    const results = h("div");
    let page = 1;

    const load = async () => {
      const q = encodeURIComponent(search.value.trim());
      const data = await api(`/api/transcripts?q=${q}&page=${page}`);
      const prev = h("button", { class: "btn small ghost", type: "button", disabled: page <= 1 }, t("Previous"));
      const next = h("button", { class: "btn small ghost", type: "button", disabled: !data.has_more }, t("Next"));
      prev.addEventListener("click", () => { page -= 1; run(prev, load); });
      next.addEventListener("click", () => { page += 1; run(next, load); });
      results.replaceChildren(
        table([
          { label: t("Closed"), render: (t) => time(t.closed_at) },
          { label: t("Title"), render: (t) => t.title || h("span", { class: "muted" }, `#${t.channel_name || t.channel_id}`) },
          { label: t("Requester"), render: (t) => user(t.requester) },
          { label: t("Outcome"), render: (t) => outcome(t.resolved) },
          { label: t("Tags"), render: (t) => h("div", { class: "row" }, t.tags.map((tag) => badge(tag))) },
          { label: t("Msgs"), render: (t) => String(t.message_count) },
        ], data.transcripts, {
          onRowClick: (t) => go(`#/tickets/transcript/${t.id}`),
          empty: search.value.trim() ? t("No transcripts match.") : t("No transcripts yet."),
        }),
        h("div", { class: "row end" }, h("span", { class: "muted small" }, t("Page {page}", { page: data.page })), prev, next));
    };

    const submit = h("button", { class: "btn small primary", type: "submit" }, t("Search"));
    const form = h("form", { class: "row" }, search, submit);
    form.addEventListener("submit", (e) => { e.preventDefault(); page = 1; run(submit, load); });
    card.append(h("h2", {}, t("Transcripts")), form, results);
    return load();
  }

  function fieldRow(label, value) {
    return h("div", {}, h("div", { class: "muted small" }, label), h("div", {}, value));
  }

  async function transcriptDetail(view, id) {
    const path = `/api/transcripts/${encodeURIComponent(id)}`;
    const tr = await api(path);
    const reload = () => { view.replaceChildren(); return transcriptDetail(view, id); };
    const action = (label, { danger = false, confirm, request, done, after = reload }) => {
      const button = h("button", { class: danger ? "btn small danger" : "btn small", type: "button" }, label);
      button.addEventListener("click", () => run(button, async () => {
        if (confirm && !(await dialog(confirm[0], h("p", {}, confirm[1]), { confirmLabel: label, danger }))) return;
        await request();
        await after();
      }, done));
      return button;
    };
    const manage = can("tickets.manage");
    const transcriptCard = tr.html_url
      ? h("div", { class: "card stack" },
        h("h2", {}, t("Transcript")),
        h("p", {}, h("a", { href: tr.html_url, target: "_blank", rel: "noopener noreferrer" }, t("Open the HTML transcript"))),
        h("p", { class: "muted small" }, tr.html_permanent ? t("Kept permanently.") : [t("Expires"), " ", time(tr.html_expires_at)]),
        manage ? h("div", { class: "row" },
          tr.html_permanent
            ? action(t("Expire normally"), { request: () => api(`${path}/html`, { method: "POST", body: { permanent: false } }), done: t("Back on the normal expiry") })
            : action(t("Keep permanently"), { request: () => api(`${path}/html`, { method: "POST", body: { permanent: true } }), done: t("Kept permanently") }),
          action(t("Delete web page"), {
            danger: true,
            confirm: [t("Delete the web page?"), t("The link stops working immediately. The text record stays in the panel.")],
            request: () => api(`${path}/html`, { method: "DELETE" }),
            done: t("Web page deleted"),
          })) : null)
      : h("div", { class: "card stack" },
        h("h2", {}, t("Transcript")),
        h("p", { class: "muted small" }, t("There is no web page for this transcript: it expired, was deleted, or the ticket was closed before hosted transcripts existed. The plain text is below.")),
        h("pre", { class: "log" }, tr.transcript || t("(empty)")));
    view.append(
      h("p", {}, h("a", { href: "#/tickets" }, t("← Back to tickets"))),
      h("h1", {}, tr.title || `#${tr.channel_name || tr.channel_id}`),
      h("div", { class: "card" },
        h("div", { class: "grid" },
          fieldRow(t("Channel"), `#${tr.channel_name || "?"} (${tr.channel_id})`),
          fieldRow(t("Requester"), user(tr.requester)),
          fieldRow(t("Closed by"), user(tr.closed_by)),
          fieldRow(t("Closed"), time(tr.closed_at)),
          fieldRow(t("Outcome"), outcome(tr.resolved)),
          fieldRow(t("AI confidence"), tr.ai_confidence === null ? t("n/a") : t("{confidence}%", { confidence: Math.round(tr.ai_confidence * 100) })),
          fieldRow(t("Messages"), String(tr.message_count)),
          fieldRow(t("AI knowledge"), tr.openai_file_id ? badge(t("added"), "accent") : badge(t("not added")))),
        tr.tags.length ? h("div", { class: "row" }, tr.tags.map((tag) => badge(tag))) : null),
      h("div", { class: "card stack" },
        h("h3", {}, t("Problem")), h("p", {}, tr.problem || "—"),
        h("h3", {}, t("Resolution")), h("p", {}, tr.resolution || "—")),
      transcriptCard,
      manage ? h("div", { class: "row end" }, action(t("Delete transcript"), {
        danger: true,
        confirm: [t("Delete this transcript?"), t("This removes the summary, the text and the web page. It can't be undone.")],
        request: () => api(path, { method: "DELETE" }),
        done: t("Transcript deleted"),
        after: () => go("#/tickets"),
      })) : null);
  }

  Panel.page({
    id: "tickets",
    title: t("Tickets"),
    perm: "tickets.view",
    group: "Community",
    async render(view, args) {
      if (args[0] === "transcript" && args[1]) return transcriptDetail(view, args[1]);
      const open = h("div", { class: "card" });
      const transcripts = h("div", { class: "card" });
      view.append(h("h1", {}, t("Tickets")), open, transcripts);
      await Promise.all([openTickets(open), transcriptList(transcripts)]);
    },
  });
})();
