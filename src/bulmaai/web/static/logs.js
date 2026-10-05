"use strict";
// Logs group: Audit log (mod cases), Flagged joiners and Website logs (panel actions).

(() => {
  const { h, api, run, time, user, badge, table, field, suggest, userSuggest, t, i18n } = Panel;

  i18n({
    "Actor": "Autor",
    "Moderator": "Moderador",
    "Filter text…": "Filtrar texto…",
    "Filter": "Filtro",
    "Name or Discord ID": "Nombre o ID de Discord",
    "Apply": "Aplicar",
    "Load more": "Cargar más",
    "When": "Cuándo",
    "Action": "Acción",
    "Reason": "Motivo",
    "Dyno (deprecated)": "Dyno (obsoleto)",
    "No entries match.": "Ninguna entrada coincide.",
    "Website logs": "Registros del sitio",
    "Every change made through this panel. Action filter matches by prefix.":
      "Cada cambio hecho a través de este panel. El filtro de acción coincide por prefijo.",
    "Details": "Detalles",
    "No audit entries match.": "Ninguna entrada de auditoría coincide.",
    "All sources": "Todas las fuentes",
    "No cases match.": "Ningún caso coincide.",
    "User": "Usuario",
    "Source": "Fuente",
    "Audit log": "Registro de auditoría",
    "Flagged joiners": "Ingresos marcados",
    "New-account and returning-offender alerts from raid_guard: posted the moment they're flagged, and auto-dismissed after 1h if no staff click resolves them first.":
      "Alertas de raid_guard por cuenta nueva o infractor recurrente: publicadas en el momento en que se marcan, y descartadas automáticamente tras 1h si ningún miembro del staff las resuelve antes.",
    "Flag": "Marca",
    "New account": "Cuenta nueva",
    "Returning offender": "Infractor recurrente",
    "Action taken": "Acción tomada",
    "Outcome": "Resultado",
    "Pending": "Pendiente",
    "Handled": "Resuelto",
    "Auto-dismissed": "Descartado automáticamente",
    "Reviewer": "Revisor",
    "Reviewed": "Revisado",
    "No flagged joiners match.": "Ningún ingreso marcado coincide.",
  });

  // ---- Website logs -----------------------------------------------------------------------------

  function detailsText(details) {
    if (!details || (typeof details === "object" && !Object.keys(details).length)) return "";
    return JSON.stringify(details, null, 1);
  }

  Panel.page({
    id: "website-logs",
    title: "Website logs",
    group: "Moderation",
    perm: "audit.view",
    async render(view) {
      const actorInput = h("input", { type: "text", placeholder: t("Name or Discord ID"), size: "22" });
      const actionInput = h("input", { type: "text", placeholder: t("All actions"), size: "22" });
      const apply = h("button", { class: "btn primary", type: "button" }, t("Apply"));
      const more = h("button", { class: "btn", type: "button" }, t("Load more"));
      const body = h("div");
      let entries = [];

      const actionsData = await api("/api/audit/actions").catch(() => ({ actions: [] }));
      const actorField = userSuggest(actorInput);
      const actionField = suggest(actionInput, actionsData.actions);

      const draw = (hasMore) => {
        body.replaceChildren(table([
          { label: t("When"), render: (e) => time(e.created_at) },
          { label: t("Actor"), render: (e) => user(e.actor) },
          { label: t("Action"), render: (e) => badge(e.action) },
          { label: t("Target"), render: (e) => (e.target ? h("span", { class: "mono" }, e.target) : "") },
          { label: t("Details"), render: (e) => h("pre", { class: "small" }, detailsText(e.details)) },
        ], entries, { empty: t("No audit entries match.") }));
        more.hidden = !hasMore;
      };

      const load = async (reset) => {
        const params = new URLSearchParams({ limit: "50" });
        if (actorInput.value.trim()) params.set("actor_id", actorInput.value.trim());
        if (actionInput.value.trim()) params.set("action", actionInput.value.trim());
        if (!reset && entries.length) params.set("before_id", String(entries[entries.length - 1].id));
        const data = await api(`/api/audit?${params}`);
        entries = reset ? data.entries : entries.concat(data.entries);
        draw(data.has_more);
      };

      apply.addEventListener("click", () => run(apply, () => load(true)));
      more.addEventListener("click", () => run(more, () => load(false)));
      for (const input of [actorInput, actionInput]) {
        input.addEventListener("keydown", (e) => { if (e.key === "Enter") apply.click(); });
      }

      view.append(
        h("h1", {}, t("Website logs")),
        h("p", { class: "muted" }, t("Every change made through this panel. Action filter matches by prefix.")),
        h("div", { class: "card stack" },
          h("div", { class: "row" }, field(t("Actor"), actorField), field(t("Action"), actionField), apply),
          body,
          h("div", { class: "row" }, more)));
      await load(true);
    },
  });

  // ---- Cases (Audit log) -------------------------------------------------------------------------

  Panel.page({
    id: "audit",
    title: "Audit log",
    group: "Moderation",
    perm: "mod.cases.view",
    async render(view, args) {
      const userInput = h("input", { type: "text", placeholder: t("Name or Discord ID"), size: "22", value: args[0] || "" });
      const modInput = h("input", { type: "text", placeholder: t("Name or Discord ID"), size: "22" });
      const actionInput = h("input", { type: "text", placeholder: t("All actions"), size: "22" });
      const sourceSelect = h("select", {}, h("option", { value: "" }, t("All sources")),
        h("option", { value: "panel" }, "panel"), h("option", { value: "automod" }, "automod"),
        h("option", { value: "discord" }, "discord"), h("option", { value: "dyno" }, t("Dyno (deprecated)")));
      const apply = h("button", { class: "btn primary", type: "submit" }, t("Apply"));
      const more = h("button", { class: "btn", type: "button", hidden: true }, t("Load more"));
      const results = h("div");
      let cases = [];
      let nextBefore = null;

      const actionsData = await api("/api/cases/actions").catch(() => ({ actions: [] }));
      const userField = userSuggest(userInput);
      const actionField = suggest(actionInput, actionsData.actions);

      const load = async (append) => {
        const params = new URLSearchParams();
        if (userInput.value.trim()) params.set("user_id", userInput.value.trim());
        if (modInput.value.trim()) params.set("moderator_id", modInput.value.trim());
        if (actionInput.value.trim()) params.set("action", actionInput.value.trim());
        if (sourceSelect.value) params.set("source", sourceSelect.value);
        if (append && nextBefore) params.set("before_id", String(nextBefore));
        const data = await api(`/api/cases?${params}`);
        cases = append ? cases.concat(data.cases) : data.cases;
        nextBefore = data.next_before_id;
        more.hidden = !nextBefore;
        results.replaceChildren(table(Panel.caseColumns({ withUser: true }), cases, { onRowClick: (c) => Panel.go(`#/users/${encodeURIComponent(c.user_id)}`), empty: t("No cases match.") }));
      };

      more.addEventListener("click", () => run(more, () => load(true)));
      const form = h("form", { class: "row", onsubmit: (e) => { e.preventDefault(); run(apply, () => load(false)); } },
        field(t("User"), userField), field(t("Moderator"), userSuggest(modInput)), field(t("Action"), actionField), field(t("Source"), sourceSelect), apply);
      view.append(
        h("h1", {}, t("Audit log")),
        h("div", { class: "card stack" }, form, results, h("div", { class: "row" }, more)));
      await load(false);
    },
  });

  // ---- Flagged joiners (raid_guard) ---------------------------------------------------------

  const JOINER_ALERT_REASONS = { new_account: t("New account"), returning_offender: t("Returning offender") };
  const JOINER_ALERT_OUTCOMES = {
    handled: [t("Handled"), "ok"],
    auto_dismissed: [t("Auto-dismissed"), "danger"],
  };

  Panel.page({
    id: "joiner-alerts",
    title: "Flagged joiners",
    group: "Moderation",
    perm: "mod.cases.view",
    async render(view, args) {
      const userInput = h("input", { type: "text", placeholder: t("Name or Discord ID"), size: "22", value: args[0] || "" });
      const apply = h("button", { class: "btn primary", type: "submit" }, t("Apply"));
      const more = h("button", { class: "btn", type: "button", hidden: true }, t("Load more"));
      const results = h("div");
      let alerts = [];
      let nextBefore = null;

      const userField = userSuggest(userInput);

      const load = async (append) => {
        const params = new URLSearchParams();
        if (userInput.value.trim()) params.set("user_id", userInput.value.trim());
        if (append && nextBefore) params.set("before_id", String(nextBefore));
        const data = await api(`/api/joiner-alerts?${params}`);
        alerts = append ? alerts.concat(data.alerts) : data.alerts;
        nextBefore = data.next_before_id;
        more.hidden = !nextBefore;
        results.replaceChildren(table([
          { label: t("When"), render: (a) => time(a.created_at) },
          { label: t("User"), render: (a) => user(a.user) },
          { label: t("Flag"), render: (a) => badge(JOINER_ALERT_REASONS[a.reason] || a.reason, "accent") },
          { label: t("Action taken"), render: (a) => a.action_taken },
          {
            label: t("Outcome"),
            render: (a) => {
              const [text, kind] = JOINER_ALERT_OUTCOMES[a.outcome] || [t("Pending"), "warn"];
              return badge(text, kind);
            },
          },
          { label: t("Reviewer"), render: (a) => user(a.reviewer) },
          { label: t("Reviewed"), render: (a) => time(a.reviewed_at) },
        ], alerts, { onRowClick: (a) => Panel.go(`#/users/${encodeURIComponent(a.user_id)}`), empty: t("No flagged joiners match.") }));
      };

      apply.addEventListener("click", () => run(apply, () => load(false)));
      more.addEventListener("click", () => run(more, () => load(true)));
      const form = h("form", { class: "row", onsubmit: (e) => { e.preventDefault(); run(apply, () => load(false)); } },
        field(t("User"), userField), apply);

      view.append(
        h("h1", {}, t("Flagged joiners")),
        h("p", { class: "muted" }, t("New-account and returning-offender alerts from raid_guard: posted the moment they're flagged, and auto-dismissed after 1h if no staff click resolves them first.")),
        h("div", { class: "card stack" }, form, results, h("div", { class: "row" }, more)));
      await load(false);
    },
  });
})();
