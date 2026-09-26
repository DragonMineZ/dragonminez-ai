"use strict";

(() => {
  const { h, api, run, can, time, user, badge, table, field, roleName } = Panel;

  const TIER_ORDER = ["owner", "admin", "moderator", "helper"];

  function duration(seconds) {
    const d = Math.floor(seconds / 86400);
    const hrs = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    return d ? `${d}d ${hrs}h ${m}m` : hrs ? `${hrs}h ${m}m` : `${m}m`;
  }

  function num(value) {
    return Number(value).toLocaleString();
  }

  function stat(label, value, extra) {
    return h("div", { class: "card stat" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value), extra || null);
  }

  function budgetCard(pool) {
    const label = `AI tokens today · ${pool.pool}`;
    if (pool.limit === null || pool.limit === undefined) return stat(label, num(pool.used), h("div", { class: "muted small" }, "no limit"));
    if (pool.limit === 0) return stat(label, num(pool.used), badge("disabled", "warn"));
    const pct = Math.round((pool.used / pool.limit) * 100);
    return stat(label, `${pct}%`,
      h("meter", { min: "0", max: String(pool.limit), value: String(Math.min(pool.used, pool.limit)), low: String(pool.limit * 0.7), high: String(pool.limit * 0.9), optimum: "0", "aria-label": label }),
      h("div", { class: "muted small" }, `${num(pool.used)} / ${num(pool.limit)}`));
  }

  function extensionsCard(data, reload) {
    const canReload = can("bot.reload");
    const rows = data.extensions.map((name) => ({ name }));
    return h("div", { class: "card" },
      h("h2", {}, `Extensions (${data.extensions.length})`),
      table([
        { label: "Extension", render: (r) => h("span", { class: "mono" }, r.name) },
        {
          label: "",
          render: (r) => {
            if (!canReload) return "";
            if (r.name === data.panel_extension) return h("span", { class: "muted small" }, "hosts the panel");
            const button = h("button", { class: "btn small", type: "button" }, "Reload");
            button.addEventListener("click", () => run(button, async () => {
              await api(`/api/status/extensions/${encodeURIComponent(r.name)}/reload`, { method: "POST" });
              await reload();
            }, `Reloaded ${r.name}`));
            return button;
          },
        },
      ], rows, { empty: "No extensions loaded." }));
  }

  Panel.page({
    id: "overview",
    title: "Overview",
    perm: "status.view",
    async render(view) {
      const body = h("div");
      const reload = async () => {
        const data = await api("/api/status");
        const b = data.ai_budget;
        body.replaceChildren(
          h("div", { class: "grid" },
            stat("Bot", data.bot ? data.bot.display_name : "not connected", data.bot ? h("div", { class: "muted small mono" }, data.bot.id) : null),
            stat("Websocket latency", data.latency_ms === null ? "—" : `${data.latency_ms} ms`),
            stat("Uptime", duration(data.uptime_seconds), h("div", { class: "muted small" }, "since ", time(data.started_at))),
            stat("Guild members", data.guild && data.guild.member_count !== null ? num(data.guild.member_count) : "—",
              h("div", { class: "muted small" }, data.guild ? data.guild.name : "guild not available")),
            stat("Database", data.db.ok ? badge("ok", "ok") : badge("error", "danger"),
              data.db.error ? h("div", { class: "error small" }, data.db.error) : null)),
          h("h2", {}, "AI budget"),
          h("p", { class: "muted small" },
            b.paused ? badge("AI paused: small pool spent", "danger") : null, " Pools reset ", time(b.resets_at), " (00:00 UTC)."),
          h("div", { class: "grid" }, b.pools.map(budgetCard)),
          extensionsCard(data, reload));
      };
      const refresh = h("button", { class: "btn small ghost", type: "button" }, "Refresh");
      refresh.addEventListener("click", () => run(refresh, reload));
      view.append(h("div", { class: "row spread" }, h("h1", {}, "Overview"), refresh), body);
      await reload();
    },
  });

  const MAX_LINES = 1500;

  Panel.page({
    id: "logs",
    title: "Logs",
    perm: "logs.view",
    async render(view) {
      const level = h("select", {}, ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"].map((l) => h("option", { value: l }, l)));
      level.value = "INFO";
      const filter = h("input", { type: "search", placeholder: "Filter text…" });
      const pause = h("button", { class: "btn small", type: "button" }, "Pause");
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
          status.textContent = `${lines.length} lines · updated ${new Date().toLocaleTimeString()}`;
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
        pause.textContent = paused ? "Resume" : "Pause";
        if (!paused) poll();
      });

      view.append(
        h("h1", {}, "Logs"),
        h("p", { class: "muted" }, "Recent bot log records kept in memory (last ~2000); cleared on restart."),
        h("div", { class: "card stack" },
          h("div", { class: "row" }, field("Minimum level", level), field("Filter", filter), pause, status),
          log));
      await poll();
    },
  });

  function detailsText(details) {
    if (!details || (typeof details === "object" && !Object.keys(details).length)) return "";
    return JSON.stringify(details, null, 1);
  }

  Panel.page({
    id: "audit",
    title: "Audit log",
    perm: "audit.view",
    async render(view) {
      const actorInput = h("input", { type: "text", placeholder: "Discord user ID", inputmode: "numeric", size: "22" });
      const actionInput = h("input", { type: "text", placeholder: "e.g. mod. or settings.set", size: "22" });
      const apply = h("button", { class: "btn primary", type: "button" }, "Apply");
      const more = h("button", { class: "btn", type: "button" }, "Load more");
      const body = h("div");
      let entries = [];

      const draw = (hasMore) => {
        body.replaceChildren(table([
          { label: "When", render: (e) => time(e.created_at) },
          { label: "Actor", render: (e) => user(e.actor) },
          { label: "Action", render: (e) => badge(e.action) },
          { label: "Target", render: (e) => (e.target ? h("span", { class: "mono" }, e.target) : "") },
          { label: "Details", render: (e) => h("pre", { class: "small" }, detailsText(e.details)) },
        ], entries, { empty: "No audit entries match." }));
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
        h("h1", {}, "Audit log"),
        h("p", { class: "muted" }, "Every change made through this panel. Action filter matches by prefix."),
        h("div", { class: "card stack" },
          h("div", { class: "row" }, field("Actor", actorInput), field("Action", actionInput), apply),
          body,
          h("div", { class: "row" }, more)));
      await load(true);
    },
  });

  Panel.page({
    id: "staff",
    title: "Staff",
    perm: "audit.view",
    async render(view) {
      const data = await api("/api/staff");
      const tiers = TIER_ORDER.filter((t) => data.tiers.includes(t));

      const rolesLine = (tier) => {
        const ids = data.role_ids[tier] || [];
        const span = h("div", { class: "muted small" });
        if (tier === "owner") span.textContent = "Guild owner and Bruno.";
        else if (!ids.length) span.textContent = tier === "admin" ? "Discord Administrator permission (no roles configured)." : "No roles configured.";
        else Promise.all(ids.map(roleName)).then((names) => {
          span.textContent = `Roles: ${names.join(", ")}${tier === "admin" ? " · plus anyone with Discord Administrator" : ""}`;
        });
        return span;
      };

      const groups = tiers.map((tier) => {
        const members = data.members.filter((m) => m.tier === tier);
        return h("div", { class: "card" },
          h("h2", {}, `${tier[0].toUpperCase()}${tier.slice(1)} (${members.length})`),
          rolesLine(tier),
          members.length
            ? h("div", { class: "row" }, members.map((m) => user(m.user)))
            : h("div", { class: "muted small" }, "Nobody."));
      });

      const levels = { helper: 1, moderator: 2, admin: 3, owner: 4 };
      const columns = [{ label: "Permission", render: (p) => h("span", { class: "mono" }, p.name) }].concat(
        tiers.slice().reverse().map((tier) => ({
          label: tier,
          render: (p) => (levels[tier] >= p.tier_level ? badge("yes", p.tier === tier ? "accent" : "ok") : h("span", { class: "muted" }, "—")),
        })));

      view.append(
        h("h1", {}, "Staff"),
        h("p", { class: "muted" },
          "Tiers come from the panel_admin_role_ids, panel_moderator_role_ids and panel_helper_role_ids settings (the owner edits them on the Settings page). ",
          "Anyone with Discord Administrator is admin; the guild owner and Bruno are owner. The highest matching tier wins."),
        groups,
        h("div", { class: "card" },
          h("h2", {}, "Permission matrix"),
          h("p", { class: "muted small" }, "Highlighted cell = minimum tier for that permission."),
          table(columns, data.permissions)));
    },
  });
})();
