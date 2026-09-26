"use strict";

(() => {
  const { h, api, run, can, badge, channelName, roleName, userName, t } = Panel;

  Panel.i18n({
    "Settings": "Configuración",
    "Filter settings…": "Filtrar configuraciones…",
    "Filter settings": "Filtrar configuraciones",
    "Runtime overrides saved to data/settings_overrides.json. Lists are comma-separated. Some components only pick up changes after a cog reload or restart.": "Sobrescrituras de tiempo de ejecución guardadas en data/settings_overrides.json. Las listas están separadas por comas. Algunos componentes solo recogen cambios después de una recarga de cog o reinicio.",
    "Only overridden": "Solo sobrescritos",
    "Setting": "Configuración",
    "Value": "Valor",
    "Save": "Guardar",
    "Reset": "Restablecer",
    "Saved {name}": "Se guardó {name}",
    "Reset {name}": "Se restableció {name}",
    "overridden": "sobrescrito",
    "owner only": "solo propietario",
    "Default: {value}": "Predeterminado: {value}",
  });

  function asText(value) {
    if (value === null || value === undefined) return "";
    if (Array.isArray(value)) return value.join(", ");
    return String(value);
  }

  function inputFor(setting) {
    if (setting.kind === "bool") return h("input", { type: "checkbox", checked: setting.value });
    const type = setting.kind === "int" || setting.kind === "float" ? "number" : "text";
    const long = setting.kind.endsWith("_list") || asText(setting.value).length > 60;
    if (long) return h("textarea", { rows: "2", value: asText(setting.value) });
    return h("input", { type, step: setting.kind === "float" ? "any" : null, value: asText(setting.value), size: "40" });
  }

  // Show "#channel" / "@role" next to ID settings so admins don't edit blind.
  function resolvedNames(setting) {
    const span = h("div", { class: "muted small" });
    const ids = Array.isArray(setting.value) ? setting.value : setting.value ? [setting.value] : [];
    const resolve = setting.hint === "channel" ? channelName : setting.hint === "role" ? roleName
      : setting.hint === "user" ? userName : null;
    if (resolve && ids.length) Promise.all(ids.map(resolve)).then((names) => { span.textContent = names.join(", "); });
    return span;
  }

  function settingRow(setting, reload) {
    const input = inputFor(setting);
    const locked = !can("settings.edit") || (setting.owner_only && !can("settings.edit_panel"));
    input.disabled = locked;
    const raw = () => (setting.kind === "bool" ? String(input.checked) : input.value);

    const save = h("button", { class: "btn small primary", type: "button", disabled: locked }, t("Save"));
    save.addEventListener("click", () => run(save, async () => {
      await api(`/api/settings/${encodeURIComponent(setting.name)}`, { method: "PUT", body: { value: raw() } });
      await reload();
    }, t("Saved {name}", { name: setting.name })));

    const reset = h("button", { class: "btn small ghost", type: "button", disabled: locked || !setting.overridden }, t("Reset"));
    reset.addEventListener("click", () => run(reset, async () => {
      await api(`/api/settings/${encodeURIComponent(setting.name)}`, { method: "DELETE" });
      await reload();
    }, t("Reset {name}", { name: setting.name })));

    return h("tr", {},
      h("td", {},
        h("div", { class: "mono" }, setting.name),
        h("div", { class: "row" },
          badge(setting.kind),
          setting.overridden ? badge(t("overridden"), "accent") : null,
          setting.owner_only ? badge(t("owner only"), "warn") : null)),
      h("td", {}, input, setting.hint ? resolvedNames(setting) : null,
        setting.overridden ? h("div", { class: "muted small" }, t("Default: {value}", { value: asText(setting.default) || "—" })) : null),
      h("td", {}, h("div", { class: "row" }, save, reset)));
  }

  Panel.page({
    id: "settings",
    title: t("Settings"),
    perm: "settings.view",
    async render(view) {
      const search = h("input", { type: "search", placeholder: t("Filter settings…"), "aria-label": t("Filter settings") });
      const onlyOverridden = h("input", { type: "checkbox", id: "only-overridden" });
      const body = h("tbody");
      let settings = [];

      const draw = () => {
        const q = search.value.trim().toLowerCase();
        body.replaceChildren(...settings
          .filter((s) => (!q || s.name.includes(q)) && (!onlyOverridden.checked || s.overridden))
          .map((s) => settingRow(s, reload)));
      };
      const reload = async () => { settings = (await api("/api/settings")).settings; draw(); };

      search.addEventListener("input", draw);
      onlyOverridden.addEventListener("change", draw);
      view.append(
        h("h1", {}, t("Settings")),
        h("p", { class: "muted" }, t("Runtime overrides saved to data/settings_overrides.json. Lists are comma-separated. Some components only pick up changes after a cog reload or restart.")),
        h("div", { class: "card" },
          h("div", { class: "row" }, search, h("label", { for: "only-overridden", class: "row" }, onlyOverridden, t("Only overridden"))),
          h("div", { class: "table-wrap" }, h("table", {},
            h("thead", {}, h("tr", {}, h("th", {}, t("Setting")), h("th", {}, t("Value")), h("th", {}, ""))),
            body))));
      await reload();
    },
  });
})();
