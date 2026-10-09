"use strict";

(() => {
  const { h, api, run, can, time, user, badge, table, toast, go, t } = Panel;

  Panel.i18n({
    "Dashboard": "Inicio",
    "Bot online · {ms} ms": "Bot en línea · {ms} ms",
    "AI paused": "IA pausada",
    "Bot issue": "Problema con el bot",
    "Find a member: name or ID…": "Buscar un miembro: nombre o ID…",
    "No member found.": "No se encontró ningún miembro.",
    "Open tickets": "Tickets abiertos",
    "{n} unclaimed": "{n} sin reclamar",
    "Flagged joiners waiting": "Ingresos marcados en espera",
    "Cases (24h)": "Casos (24 h)",
    "All clear": "Todo en orden",
    "Needs attention": "Requiere atención",
    "Nothing needs attention right now.": "Nada requiere atención ahora mismo.",
    "New account": "Cuenta nueva",
    "Returning offender": "Infractor reincidente",
    "expires": "vence",
    "Review": "Revisar",
    "Open in Discord": "Abrir en Discord",
    "View all flagged joiners ›": "Ver todos los ingresos marcados ›",
    "View all tickets ›": "Ver todos los tickets ›",
    "Recent cases": "Casos recientes",
    "No cases yet.": "Aún no hay casos.",
    "View all cases ›": "Ver todos los casos ›",
    "Refresh": "Actualizar",
  });

  const MAX_ITEMS = 8;
  const DAY_MS = 86400000;
  const isId = (v) => /^\d{17,20}$/.test(v);
  const openUser = (id) => go(`#/users/${encodeURIComponent(id)}`);
  const failed = (result) => h("p", { class: "muted small" }, result.reason && result.reason.message ? result.reason.message : String(result.reason));

  function healthPill(s) {
    if (!s.bot || !s.db.ok) return h("a", { class: "badge danger", href: "#/overview" }, t("Bot issue"));
    if (s.ai_budget.paused) return h("a", { class: "badge warn", href: "#/overview" }, t("AI paused"));
    return h("a", { class: "badge ok", href: "#/overview" }, t("Bot online · {ms} ms", { ms: s.latency_ms ?? "—" }));
  }

  function finder() {
    const input = h("input", { type: "search", class: "dash-find", placeholder: t("Find a member: name or ID…"), "aria-label": t("Find a member: name or ID…") });
    input.addEventListener("change", () => { const v = input.value.trim(); if (isId(v)) openUser(v); });
    input.addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      e.preventDefault();
      const q = input.value.trim();
      if (!q) return;
      if (isId(q)) { openUser(q); return; }
      run(null, async () => {
        const first = (await api(`/api/users/search?q=${encodeURIComponent(q)}`)).results[0];
        if (first) openUser(first.id); else toast(t("No member found."));
      });
    });
    return h("div", { class: "card dash-finder" }, Panel.userSuggest(input));
  }

  function tile(label, href, result, value, sub, warn) {
    if (result.status === "rejected") return h("a", { class: "card stat dash-tile", href }, h("div", { class: "label" }, label), failed(result));
    return h("a", { class: warn ? "card stat dash-tile warn" : "card stat dash-tile", href },
      h("div", { class: "label" }, label), h("div", { class: "value" }, value),
      sub || value === 0 ? h("div", { class: "muted small" }, sub || t("All clear")) : null);
  }

  function joinerItem(a) {
    const reasons = { new_account: t("New account"), returning_offender: t("Returning offender") };
    return h("div", { class: "row dash-item" },
      user(a.user || a.user_id), badge(reasons[a.reason] || a.reason, "accent"), time(a.created_at),
      h("span", { class: "muted small" }, t("expires"), " ", time(a.expires_at)),
      h("button", { class: "btn small", type: "button", onclick: () => openUser(a.user_id) }, t("Review")));
  }

  function ticketItem(k) {
    return h("div", { class: "row dash-item" },
      h("strong", {}, k.number ? `#${k.number} ${k.category || ""}`.trim() : `#${k.name}`),
      user(k.requester), time(k.created_at),
      h("a", { class: "btn small ghost", href: k.url, target: "_blank", rel: "noopener" }, t("Open in Discord")));
  }

  function attention(joinersR, ticketsR, pending, unclaimed) {
    const errors = [joinersR, ticketsR].filter((r) => r && r.status === "rejected").map(failed);
    const items = pending.map((a) => ["j", joinerItem(a)]).concat(unclaimed.map((k) => ["t", ticketItem(k)]));
    if (!items.length) return h("div", { class: "card" }, errors, errors.length ? null : h("p", { class: "muted" }, t("Nothing needs attention right now.")));
    const hidden = new Set(items.slice(MAX_ITEMS).map(([kind]) => kind));
    return h("div", { class: "card" }, h("h2", {}, t("Needs attention")), errors,
      h("div", { class: "dash-list" }, items.slice(0, MAX_ITEMS).map(([, node]) => node)),
      hidden.size ? h("div", { class: "row small" },
        hidden.has("j") ? h("a", { href: "#/joiner-alerts" }, t("View all flagged joiners ›")) : null,
        hidden.has("t") ? h("a", { href: "#/tickets" }, t("View all tickets ›")) : null) : null);
  }

  function recentCases(casesR) {
    return h("div", { class: "card" }, h("h2", {}, t("Recent cases")),
      casesR.status === "rejected" ? failed(casesR) : table(Panel.caseColumns({ withUser: true }), casesR.value.cases.slice(0, MAX_ITEMS),
        { onRowClick: (c) => openUser(c.user_id), empty: t("No cases yet.") }),
      h("p", { class: "small" }, h("a", { href: "#/audit" }, t("View all cases ›"))));
  }

  Panel.page({
    id: "dashboard",
    title: "Dashboard",
    perm: "status.view",
    async render(view) {
      const pill = h("span");
      const body = h("div");
      const skip = Promise.resolve(null);
      const load = async () => {
        const mod = can("mod.cases.view");
        const tix = can("tickets.view");
        const [statusR, ticketsR, joinersR, casesR] = await Promise.allSettled([
          can("status.view") ? api("/api/status") : skip,
          tix ? api("/api/tickets") : skip,
          mod ? api("/api/joiner-alerts?limit=50") : skip,
          mod ? api("/api/cases?limit=100") : skip,
        ]);
        pill.replaceChildren(statusR.status === "rejected" ? failed(statusR) : statusR.value ? healthPill(statusR.value) : "");

        const now = Date.now();
        const tickets = ticketsR.status === "fulfilled" && ticketsR.value ? ticketsR.value.tickets : [];
        const unclaimed = tickets.filter((k) => !k.claimed_by);
        const pending = joinersR.status === "fulfilled" && joinersR.value
          ? joinersR.value.alerts.filter((a) => a.outcome === null && new Date(a.expires_at).getTime() > now) : [];
        const cases = casesR.status === "fulfilled" && casesR.value ? casesR.value.cases : [];
        const recent = cases.filter((c) => now - new Date(c.created_at).getTime() < DAY_MS).length;

        const tiles = [
          tix ? tile(t("Open tickets"), "#/tickets", ticketsR, tickets.length, tickets.length ? t("{n} unclaimed", { n: unclaimed.length }) : null) : null,
          mod ? tile(t("Flagged joiners waiting"), "#/joiner-alerts", joinersR, pending.length, null, pending.length > 0) : null,
          mod ? tile(t("Cases (24h)"), "#/audit", casesR, recent === 100 && cases.length === 100 ? "100+" : recent, null) : null,
        ].filter(Boolean);

        body.replaceChildren(
          can("users.view") ? finder() : "",
          tiles.length ? h("div", { class: "grid dash-tiles" }, tiles) : "",
          mod || tix ? attention(mod && joinersR, tix && ticketsR, pending, unclaimed) : "",
          mod ? recentCases(casesR) : "");
      };
      const refresh = h("button", { class: "btn small ghost", type: "button" }, t("Refresh"));
      refresh.addEventListener("click", () => run(refresh, load));
      view.append(h("div", { class: "row spread" }, h("h1", {}, t("Dashboard")), h("div", { class: "row" }, pill, refresh)), body);
      await load();
    },
  });
})();
