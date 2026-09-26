"use strict";

(() => {
  const { h, api, run, can, time, user, badge, table, go } = Panel;

  function discordLink(url, label) {
    return h("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, label);
  }

  function outcome(resolved) {
    return resolved ? badge("solved", "ok") : badge("unresolved", "warn");
  }

  async function openTickets(card) {
    const data = await api("/api/tickets");
    const reload = () => openTickets(card);
    const toggle = (ticket) => {
      const button = h("button", { class: "btn small", type: "button" }, ticket.ai_enabled ? "Turn AI off" : "Turn AI on");
      button.addEventListener("click", () => run(button, async () => {
        await api(`/api/tickets/${ticket.id}/ai`, { method: "POST", body: { enabled: !ticket.ai_enabled } });
        await reload();
      }, `AI ${ticket.ai_enabled ? "disabled" : "enabled"} in #${ticket.name}`));
      return button;
    };
    const columns = [
      { label: "Channel", render: (t) => discordLink(t.url, `#${t.name}`) },
      { label: "Requester", render: (t) => user(t.requester) },
      { label: "Opened", render: (t) => time(t.created_at) },
      { label: "AI", render: (t) => (t.ai_enabled ? badge("on", "ok") : badge("off", "danger")) },
    ];
    if (can("tickets.manage")) columns.push({ label: "", render: toggle });
    const notes = [];
    if (!data.category_configured) notes.push("ai_ticket_category_id isn't set, so no channel counts as a ticket.");
    if (!data.cog_loaded) notes.push("The AI tickets cog isn't loaded; toggles are saved and apply when it loads.");
    card.replaceChildren(
      h("div", { class: "row spread" }, h("h2", {}, `Open tickets (${data.tickets.length})`),
        h("button", { class: "btn small ghost", type: "button", onclick: reload }, "Refresh")),
      notes.map((n) => h("p", { class: "muted small" }, n)),
      table(columns, data.tickets, { empty: "No open tickets." }));
  }

  function transcriptList(card) {
    const search = h("input", { type: "search", placeholder: "Title, problem, tag, channel or requester ID…", "aria-label": "Search transcripts", size: "40" });
    const results = h("div");
    let page = 1;

    const load = async () => {
      const q = encodeURIComponent(search.value.trim());
      const data = await api(`/api/transcripts?q=${q}&page=${page}`);
      const prev = h("button", { class: "btn small ghost", type: "button", disabled: page <= 1 }, "Previous");
      const next = h("button", { class: "btn small ghost", type: "button", disabled: !data.has_more }, "Next");
      prev.addEventListener("click", () => { page -= 1; run(prev, load); });
      next.addEventListener("click", () => { page += 1; run(next, load); });
      results.replaceChildren(
        table([
          { label: "Closed", render: (t) => time(t.closed_at) },
          { label: "Title", render: (t) => t.title || h("span", { class: "muted" }, `#${t.channel_name || t.channel_id}`) },
          { label: "Requester", render: (t) => user(t.requester) },
          { label: "Outcome", render: (t) => outcome(t.resolved) },
          { label: "Tags", render: (t) => h("div", { class: "row" }, t.tags.map((tag) => badge(tag))) },
          { label: "Msgs", render: (t) => String(t.message_count) },
        ], data.transcripts, {
          onRowClick: (t) => go(`#/tickets/transcript/${t.id}`),
          empty: search.value.trim() ? "No transcripts match." : "No transcripts yet.",
        }),
        h("div", { class: "row end" }, h("span", { class: "muted small" }, `Page ${data.page}`), prev, next));
    };

    const submit = h("button", { class: "btn small primary", type: "submit" }, "Search");
    const form = h("form", { class: "row" }, search, submit);
    form.addEventListener("submit", (e) => { e.preventDefault(); page = 1; run(submit, load); });
    card.append(h("h2", {}, "Transcripts"), form, results);
    return load();
  }

  function fieldRow(label, value) {
    return h("div", {}, h("div", { class: "muted small" }, label), h("div", {}, value));
  }

  async function transcriptDetail(view, id) {
    const t = await api(`/api/transcripts/${encodeURIComponent(id)}`);
    view.append(
      h("p", {}, h("a", { href: "#/tickets" }, "← Back to tickets")),
      h("h1", {}, t.title || `#${t.channel_name || t.channel_id}`),
      h("div", { class: "card" },
        h("div", { class: "grid" },
          fieldRow("Channel", `#${t.channel_name || "?"} (${t.channel_id})`),
          fieldRow("Requester", user(t.requester)),
          fieldRow("Closed by", user(t.closed_by)),
          fieldRow("Closed", time(t.closed_at)),
          fieldRow("Outcome", outcome(t.resolved)),
          fieldRow("AI confidence", t.ai_confidence === null ? "n/a" : `${Math.round(t.ai_confidence * 100)}%`),
          fieldRow("Messages", String(t.message_count)),
          fieldRow("AI knowledge", t.openai_file_id ? badge("added", "accent") : badge("not added"))),
        t.tags.length ? h("div", { class: "row" }, t.tags.map((tag) => badge(tag))) : null),
      h("div", { class: "card stack" },
        h("h3", {}, "Problem"), h("p", {}, t.problem || "—"),
        h("h3", {}, "Resolution"), h("p", {}, t.resolution || "—")),
      h("div", { class: "card" }, h("h2", {}, "Transcript"), h("pre", { class: "log" }, t.transcript || "(empty)")));
  }

  Panel.page({
    id: "tickets",
    title: "Tickets",
    perm: "tickets.view",
    async render(view, args) {
      if (args[0] === "transcript" && args[1]) return transcriptDetail(view, args[1]);
      const open = h("div", { class: "card" });
      const transcripts = h("div", { class: "card" });
      view.append(h("h1", {}, "Tickets"), open, transcripts);
      await Promise.all([openTickets(open), transcriptList(transcripts)]);
    },
  });
})();
