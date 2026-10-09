"use strict";
// Settings page: every runtime setting, grouped into sections (web/settings_meta.py) with a
// search across all of them. Rows save one setting at a time; switches save on change.

(() => {
  const { h, api, run, can, badge, channelName, roleName, userName, t } = Panel;

  Panel.i18n({
    "Settings": "Configuración",
    "Changes apply right away. Some features only pick them up after a reload or restart.": "Los cambios se aplican al instante. Algunas funciones solo los toman después de una recarga o un reinicio.",
    "Search settings…": "Buscar configuraciones…",
    "Search settings": "Buscar configuraciones",
    "Only changed": "Solo modificadas",
    "Sections": "Secciones",
    "No settings match.": "Ninguna configuración coincide.",
    "No changed settings in this section.": "No hay configuraciones modificadas en esta sección.",
    "Infrastructure settings. A wrong value here can break the bot or lock people out.": "Configuración de infraestructura. Un valor incorrecto aquí puede romper el bot o dejar a gente sin acceso.",
    "Save": "Guardar",
    "Reset": "Restablecer",
    "Saved {name}": "Se guardó {name}",
    "Reset {name}": "Se restableció {name}",
    "changed": "modificada",
    "owner only": "solo propietario",
    "Default: {value}": "Predeterminado: {value}",
    "— none —": "— ninguno —",
    "Filter channels…": "Filtrar canales…",
    "Filter roles…": "Filtrar roles…",
  });

  const ADVANCED = "advanced";
  const CHANNEL_TYPES = ["text", "news", "forum", "voice", "stage_voice"];
  const isList = (s) => s.kind.endsWith("_list");
  const locked = (s) => !can("settings.edit") || (s.owner_only && !can("settings.edit_panel"));
  const put = (s, value) => api(`/api/settings/${encodeURIComponent(s.name)}`, { method: "PUT", body: { value } });

  function asText(value) {
    if (value === null || value === undefined) return "";
    if (Array.isArray(value)) return value.join(", ");
    return String(value);
  }

  // The PUT format: plain string; lists comma-separated.
  function normalize(s, raw) {
    if (!isList(s)) return raw;
    return raw.split(/[,\n]/).map((part) => part.trim()).filter(Boolean).join(",");
  }

  const loadedRaw = (s) => (isList(s) ? (s.value || []).join(",") : asText(s.value));

  function keepUnknown(select, value) {
    if (!value) return;
    Panel.guild().then(() => {
      if (select.value === value) return;
      select.append(h("option", { value }, value));
      select.value = value;
    }).catch(() => {});
  }

  function roleSelect(s, g) {
    const value = s.value ? String(s.value) : "";
    const select = h("select", {}, s.optional ? h("option", { value: "" }, t("— none —")) : null,
      g.roles.map((r) => h("option", { value: r.id }, `@${r.name}`)));
    if (value && !g.roles.some((r) => r.id === value)) select.append(h("option", { value }, value));
    select.value = value;
    return select;
  }

  function channelItems(g) {
    return g.channels.filter((c) => CHANNEL_TYPES.includes(c.type)).sort((a, b) => a.position - b.position)
      .map((c) => ({ id: c.id, label: `#${c.name}${c.category ? ` (${c.category})` : ""}` }));
  }

  // { el, raw } for a non-bool setting: pickers for channel/role ids when the guild is known.
  function control(s, g) {
    const single = s.kind === "int" && (s.hint === "channel" || s.hint === "role");
    if (single && s.hint === "channel" && g) {
      const value = s.value ? String(s.value) : "";
      const types = s.name.endsWith("_category_id") ? ["category"] : CHANNEL_TYPES;
      const select = Panel.channelSelect(value, { includeNone: s.optional, types });
      keepUnknown(select, value);
      return { el: select, raw: () => select.value };
    }
    if (single && g) {
      const select = roleSelect(s, g);
      return { el: select, raw: () => select.value };
    }
    if (s.kind === "int_list" && (s.hint === "channel" || s.hint === "role") && g && Panel.idPicker) {
      const items = s.hint === "channel" ? channelItems(g) : g.roles.map((r) => ({ id: r.id, label: `@${r.name}` }));
      const picker = Panel.idPicker(items, s.value || [], t(s.hint === "channel" ? "Filter channels…" : "Filter roles…"));
      return { el: picker.el, raw: picker.get };
    }
    const long = isList(s) || asText(s.value).length > 60;
    const number = s.kind === "int" || s.kind === "float";
    const input = long
      ? h("textarea", { rows: "2", value: asText(s.value) })
      : h("input", { type: number ? "number" : "text", step: s.kind === "float" ? "any" : null, value: asText(s.value) });
    return { el: input, raw: () => input.value };
  }

  // "#channel" / "@role" / "@user" for raw ids, so nobody edits blind.
  function namesOf(s, value) {
    const span = h("span", {}, asText(value) || "—");
    const ids = Array.isArray(value) ? value : value ? [value] : [];
    const resolve = s.hint === "channel" ? channelName : s.hint === "role" ? roleName : s.hint === "user" ? userName : null;
    if (resolve && ids.length) Promise.all(ids.map(resolve)).then((names) => { span.textContent = names.join(", "); });
    return span;
  }

  function settingRow(s, g, onSaved) {
    const lock = locked(s);
    const title = s.label || s.name;
    const reset = h("button", { class: "btn small ghost", type: "button", disabled: lock || !s.overridden }, t("Reset"));
    reset.addEventListener("click", async () => {
      const res = await run(reset, () => api(`/api/settings/${encodeURIComponent(s.name)}`, { method: "DELETE" }), t("Reset {name}", { name: title }));
      if (res) onSaved(s, res.value, false);
    });

    let right;
    if (s.kind === "bool") {
      const onChange = async (input) => {
        let res;
        await run(input, async () => { res = await put(s, String(input.checked)); }, t("Saved {name}", { name: title }));
        if (res) onSaved(s, res.value, true);
        else input.checked = !input.checked;
      };
      const sw = Panel.switchInput
        ? Panel.switchInput(Boolean(s.value), title, lock, onChange)
        : h("input", { type: "checkbox", checked: Boolean(s.value), disabled: lock, "aria-label": title, onchange: (e) => onChange(e.target) });
      right = [h("div", { class: "row nowrap setting-actions" }, sw, reset)];
    } else {
      const { el, raw } = control(s, g);
      const loaded = normalize(s, loadedRaw(s));
      const save = h("button", { class: "btn small primary", type: "button", disabled: true }, t("Save"));
      const dirty = () => { save.disabled = lock || normalize(s, raw()) === loaded; };
      el.addEventListener("input", dirty);
      el.addEventListener("change", dirty);
      save.addEventListener("click", async () => {
        const res = await run(save, () => put(s, normalize(s, raw())), t("Saved {name}", { name: title }));
        if (res) onSaved(s, res.value, true);
        else dirty();
      });
      if (lock) {
        el.disabled = true;
        for (const input of el.querySelectorAll("input, select, textarea")) input.disabled = true;
      }
      right = [el, s.hint === "user" ? h("div", { class: "muted small" }, namesOf(s, s.value)) : null,
        h("div", { class: "row nowrap setting-actions" }, save, reset)];
    }

    return h("div", { class: "setting-row" },
      h("div", { class: "setting-info" },
        h("div", { class: "setting-label" }, title),
        s.help ? h("div", { class: "muted small" }, s.help) : null,
        h("div", { class: "row setting-meta" },
          h("span", { class: "mono setting-key" }, s.name),
          s.overridden ? badge(t("changed"), "accent") : null,
          s.owner_only ? badge(t("owner only"), "warn") : null)),
      h("div", { class: "setting-control" }, right,
        s.overridden ? h("div", { class: "muted small" }, t("Default: {value}", { value: "" }), namesOf(s, s.default)) : null));
  }

  Panel.page({
    id: "settings",
    title: t("Settings"),
    perm: "settings.view",
    async render(view, args) {
      const [data, g] = await Promise.all([api("/api/settings"), Panel.guild().catch(() => null)]);
      const settings = data.settings;
      const sections = [...data.sections.filter((x) => x.id !== ADVANCED), ...data.sections.filter((x) => x.id === ADVANCED)];
      let current = sections.some((x) => x.id === args[0]) ? args[0] : sections[0].id;

      const search = h("input", { type: "search", placeholder: t("Search settings…"), "aria-label": t("Search settings") });
      const onlyChanged = h("input", { type: "checkbox" });
      const list = h("nav", { class: "settings-sections", "aria-label": t("Sections") });
      const pane = h("section", { class: "settings-pane" });

      const query = () => search.value.trim().toLowerCase();
      const visible = (s) => !onlyChanged.checked || s.overridden;
      const matches = (s, q) => `${s.label} ${s.name} ${s.help}`.toLowerCase().includes(q);

      const onSaved = (s, value, overridden) => {
        s.value = value;
        s.overridden = overridden;
        drawList();
        const row = pane.querySelector(`[data-setting="${CSS.escape(s.name)}"]`);
        if (row) row.replaceWith(tagged(s));
      };
      const tagged = (s) => {
        const row = settingRow(s, g, onSaved);
        row.dataset.setting = s.name;
        return row;
      };
      const rows = (items) => h("div", { class: "card setting-rows" }, items.map(tagged));

      const drawList = () => {
        list.replaceChildren(...sections.flatMap((section) => {
          const count = settings.filter((s) => s.section === section.id && visible(s)).length;
          const button = h("button", { type: "button", "aria-current": section.id === current ? "true" : null },
            h("span", {}, section.title), h("span", { class: "muted small" }, String(count)));
          button.addEventListener("click", () => {
            current = section.id;
            search.value = "";
            history.replaceState(null, "", `#/settings/${section.id}`);
            drawList();
            drawPane();
          });
          return section.id === ADVANCED ? [h("hr"), button] : [button];
        }));
      };

      const drawPane = () => {
        const q = query();
        if (q) {
          const groups = sections.map((section) => [section, settings.filter((s) => s.section === section.id && visible(s) && matches(s, q))])
            .filter(([, items]) => items.length);
          pane.replaceChildren(...(groups.length
            ? groups.flatMap(([section, items]) => [h("h3", { class: "settings-group" }, section.title), rows(items)])
            : [h("p", { class: "empty" }, t("No settings match."))]));
          return;
        }
        const section = sections.find((x) => x.id === current);
        const items = settings.filter((s) => s.section === current && visible(s));
        pane.replaceChildren(
          h("h2", {}, section.title),
          h("p", { class: "muted" }, section.description),
          current === ADVANCED ? h("div", { class: "card banner warn", role: "note" }, t("Infrastructure settings. A wrong value here can break the bot or lock people out.")) : null,
          items.length ? rows(items) : h("p", { class: "empty" }, t("No changed settings in this section.")));
      };

      search.addEventListener("input", drawPane);
      onlyChanged.addEventListener("change", () => { drawList(); drawPane(); });
      view.append(
        h("h1", {}, t("Settings")),
        h("p", { class: "muted" }, t("Changes apply right away. Some features only pick them up after a reload or restart.")),
        h("div", { class: "card row toolbar" }, search, h("label", { class: "row check" }, onlyChanged, t("Only changed"))),
        h("div", { class: "settings-layout" }, list, pane));
      drawList();
      drawPane();
    },
  });
})();
