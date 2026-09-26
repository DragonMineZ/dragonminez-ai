"use strict";
// Logs group: Bot logs (Python log stream), Server logs (Discord audit log + Dyno moderation),
// Audit log (mod cases) and Website logs (panel actions).

(() => {
  const { h, api, run, time, user, badge, table, field, suggest, userSuggest, t, i18n } = Panel;

  i18n({
    "Actor": "Autor",
    "Moderator": "Moderador",
    "Filter text…": "Filtrar texto…",
    "Pause": "Pausar",
    "Resume": "Reanudar",
    "{count} lines · updated {time}": "{count} líneas · actualizado {time}",
    "Bot logs": "Registros del bot",
    "Recent bot log records kept in memory (last ~2000); cleared on restart.":
      "Registros recientes del bot guardados en memoria (últimos ~2000); se borran al reiniciar.",
    "Minimum level": "Nivel mínimo",
    "Filter": "Filtro",
    "Name or Discord ID": "Nombre o ID de Discord",
    "All actions": "Todas las acciones",
    "Apply": "Aplicar",
    "Load more": "Cargar más",
    "When": "Cuándo",
    "Action": "Acción",
    "Executor": "Ejecutor",
    "Target": "Objetivo",
    "Reason": "Motivo",
    "Changes": "Cambios",
    "Dyno (deprecated)": "Dyno (obsoleto)",
    "No entries match.": "Ninguna entrada coincide.",
    "Server logs": "Registros del servidor",
    "Discord's server audit log, live from Discord, plus Dyno's moderation actions from its mod-log channel.":
      "El registro de auditoría del servidor, en vivo desde Discord, más las acciones de moderación de Dyno desde su canal de mod-log.",
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
  });

  const MAX_LINES = 1500;

  // ---- Bot logs -------------------------------------------------------------------------------

  Panel.page({
    id: "bot-logs",
    title: "Bot logs",
    group: "Logs",
    perm: "logs.view",
    async render(view) {
      const level = h("select", {}, ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"].map((l) => h("option", { value: l }, l)));
      level.value = "INFO";
      const filter = h("input", { type: "search", placeholder: t("Filter text…") });
      const pause = h("button", { class: "btn small", type: "button" }, t("Pause"));
      const status = h("span", { class: "muted small" });
      const log = h("div", { class: "log", role: "log", "aria-live": "off" });
      let lines = [];
      let after = 0;
      let paused = false;
      let busy = false;
      let gen = 0;  // bumped when the level changes so stale responses are dropped

      const matches = (r) => {
        const q = filter.value.trim().toLowerCase();
        return !q || r.message.toLowerCase().includes(q) || r.logger.toLowerCase().includes(q);
      };
      const lineFor = (r) => h("pre", { class: r.levelno >= 40 ? "error" : r.levelno < 20 ? "muted" : null },
        `${new Date(r.time).toLocaleTimeString()} ${r.level.padEnd(8)} ${r.logger} | ${r.message}`);
      const redraw = () => {
        log.replaceChildren(...lines.filter((l) => matches(l.record)).map((l) => l.node));
        log.scrollTop = log.scrollHeight;
      };

      const poll = async () => {
        if (busy) return;
        busy = true;
        const mine = gen;
        try {
          const data = await api(`/api/logs?after=${after}&level=${encodeURIComponent(level.value)}`);
          if (mine !== gen) return;
          if (data.last_id < after) { lines = []; log.replaceChildren(); }  // bot restarted
          after = data.last_id;
          const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 24;
          for (const record of data.records) {
            const entry = { record, node: lineFor(record) };
            lines.push(entry);
            if (matches(record)) log.append(entry.node);
          }
          while (lines.length > MAX_LINES) lines.shift().node.remove();
          if (atBottom) log.scrollTop = log.scrollHeight;
          status.textContent = t("{count} lines · updated {time}", { count: lines.length, time: new Date().toLocaleTimeString() });
        } catch (error) {
          status.textContent = error.message;
        } finally {
          busy = false;
        }
      };

      const timer = setInterval(() => {
        if (!log.isConnected) { clearInterval(timer); return; }  // navigated away
        if (!paused) poll();
      }, 3000);
      level.addEventListener("change", () => { gen += 1; lines = []; after = 0; log.replaceChildren(); poll(); });
      filter.addEventListener("input", redraw);
      pause.addEventListener("click", () => {
        paused = !paused;
        pause.textContent = paused ? t("Resume") : t("Pause");
        if (!paused) poll();
      });

      view.append(
        h("h1", {}, t("Bot logs")),
        h("p", { class: "muted" }, t("Recent bot log records kept in memory (last ~2000); cleared on restart.")),
        h("div", { class: "card stack" },
          h("div", { class: "row" }, field(t("Minimum level"), level), field(t("Filter"), filter), pause, status),
          log));
      await poll();
    },
  });

  // ---- Server logs -----------------------------------------------------------------------------

  function targetCell(tgt) {
    if (!tgt) return h("span", { class: "muted" }, "—");
    if ("avatar" in tgt) return user(tgt);
    return h("span", {}, tgt.name || tgt.id || "—", tgt.type ? h("span", { class: "muted small" }, ` (${tgt.type})`) : null);
  }

  function changesCell(changes) {
    if (!changes || !changes.length) return h("span", { class: "muted" }, "—");
    return h("div", { class: "stack" }, changes.map((c) =>
      h("div", { class: "small mono" }, `${c.key}: ${c.before ?? "—"} → ${c.after ?? "—"}`)));
  }

  Panel.page({
    id: "server-logs",
    title: "Server logs",
    group: "Logs",
    perm: "logs.view",
    async render(view) {
      const userInput = h("input", { type: "text", placeholder: t("Name or Discord ID"), size: "22" });
      const actionInput = h("input", { type: "text", placeholder: t("All actions"), size: "26" });
      const apply = h("button", { class: "btn primary", type: "submit" }, t("Apply"));
      const more = h("button", { class: "btn", type: "button", hidden: true }, t("Load more"));
      const results = h("div");
      let entries = [];
      let cursor = null;

      const actionsData = await api("/api/server-logs/actions").catch(() => ({ actions: [] }));
      const userField = userSuggest(userInput);
      const actionField = suggest(actionInput, actionsData.actions);

      const draw = () => {
        results.replaceChildren(table([
          { label: t("When"), render: (e) => time(e.created_at) },
          { label: t("Action"), render: (e) => h("div", { class: "row" }, badge(e.action), e.source === "dyno" ? badge(t("Dyno (deprecated)"), "warn") : null) },
          { label: t("Executor"), render: (e) => user(e.executor) },
          { label: t("Target"), render: (e) => targetCell(e.target) },
          { label: t("Reason"), render: (e) => e.reason || h("span", { class: "muted" }, "—") },
          { label: t("Changes"), render: (e) => changesCell(e.changes) },
        ], entries, { empty: t("No entries match.") }));
      };

      const load = async (reset) => {
        const params = new URLSearchParams({ limit: "50" });
        if (userInput.value.trim()) params.set("user", userInput.value.trim());
        if (actionInput.value.trim()) params.set("action", actionInput.value.trim());
        if (!reset && cursor) params.set("before", cursor);
        const data = await api(`/api/server-logs?${params}`);
        entries = reset ? data.entries : entries.concat(data.entries);
        cursor = data.next_cursor;
        more.hidden = !cursor;
        draw();
      };

      apply.addEventListener("click", () => run(apply, () => load(true)));
      more.addEventListener("click", () => run(more, () => load(false)));
      const form = h("form", { class: "row", onsubmit: (e) => { e.preventDefault(); run(apply, () => load(true)); } },
        field(t("User"), userField), field(t("Action"), actionField), apply);

      view.append(
        h("h1", {}, t("Server logs")),
        h("p", { class: "muted" }, t("Discord's server audit log, live from Discord, plus Dyno's moderation actions from its mod-log channel.")),
        h("div", { class: "card stack" }, form, results, h("div", { class: "row" }, more)));
      await load(true);
    },
  });

  // ---- Website logs -----------------------------------------------------------------------------

  function detailsText(details) {
    if (!details || (typeof details === "object" && !Object.keys(details).length)) return "";
    return JSON.stringify(details, null, 1);
  }

  Panel.page({
    id: "website-logs",
    title: "Website logs",
    group: "Logs",
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
    group: "Logs",
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
})();
