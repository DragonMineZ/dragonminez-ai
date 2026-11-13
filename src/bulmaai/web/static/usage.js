"use strict";
// AI usage page: spend against the monthly budget, daily breakdowns and the costliest calls.
// Charts are hand-built SVG (the CSP allows no chart library); colors come from panel.css tokens.

(() => {
  const { h, api, run, time, badge, table, t, locale, can, dialog } = Panel;

  Panel.i18n({
    "AI usage": "Uso de IA",
    "Refresh": "Actualizar",
    "Range": "Periodo",
    "This cycle": "Este ciclo",
    "7 days": "7 días",
    "30 days": "30 días",
    "90 days": "90 días",
    "Cost": "Costo",
    "Tokens": "Tokens",
    "Metric": "Métrica",
    "Group by": "Agrupar por",
    "Feature": "Función",
    "Model": "Modelo",
    "Pool": "Pool",
    "Small pool": "Pool pequeño",
    "Big pool": "Pool grande",
    "Other": "Otros",
    "Total": "Total",
    "Spent this cycle": "Gastado este ciclo",
    "of {budget}": "de {budget}",
    "Cycle {start} → {end}": "Ciclo {start} → {end}",
    "Projected by {date}": "Proyección al {date}",
    "On track": "Vas bien",
    "Close to the limit": "Cerca del límite",
    "Over budget by {amount}": "Se pasa del presupuesto por {amount}",
    "Would reach {budget} on {date}": "Llegaría a {budget} el {date}",
    "Today": "Hoy",
    "Reset today's limits": "Reiniciar los límites de hoy",
    "Reset today's AI token count? The bot starts counting from zero again until 00:00 UTC.": "¿Reiniciar el conteo de tokens de IA de hoy? El bot vuelve a contar desde cero hasta las 00:00 UTC.",
    "Reset": "Reiniciar",
    "Today's limits were reset.": "Se reiniciaron los límites de hoy.",
    "Reset today at {time} UTC": "Reiniciado hoy a las {time} UTC",
    "{tokens} tokens": "{tokens} tokens",
    "vs 7-day avg {amount}": "vs. promedio de 7 días {amount}",
    "Left per day": "Disponible por día",
    "for {count} more days": "por {count} días más",
    "for 1 more day": "por 1 día más",
    "Cycle ends today": "El ciclo termina hoy",
    "Cache savings": "Ahorro por caché",
    "{pct}% hit rate": "{pct}% de aciertos",
    "Cached prompt reads": "Lecturas de prompt en caché",
    "Web searches": "Búsquedas web",
    "Spent": "Gastado",
    "Projection": "Proyección",
    "Budget": "Presupuesto",
    "Even pace": "Ritmo parejo",
    "{amount} budget": "presupuesto de {amount}",
    "↑ {amount} budget is above this chart": "↑ el presupuesto de {amount} está por encima del gráfico",
    "Spent that day": "Gastado ese día",
    "Total so far": "Total acumulado",
    "Spending this cycle: {spent} of {budget}, projected {projected} by {end}.": "Gasto de este ciclo: {spent} de {budget}, proyección de {projected} al {end}.",
    "Daily breakdown": "Desglose diario",
    "Daily breakdown ({metric}) by {group} from {from} to {to}, total {total}.": "Desglose diario ({metric}) por {group} del {from} al {to}, total {total}.",
    "Hourly breakdown ({metric}) by {group} on {day}, total {total}.": "Desglose por hora ({metric}) por {group} el {day}, total {total}.",
    "Use the arrow keys to read each bar, Enter opens that day.": "Usa las flechas para leer cada barra; Enter abre ese día.",
    "Use the arrow keys to read each day.": "Usa las flechas para leer cada día.",
    "Use the arrow keys to read each bar.": "Usa las flechas para leer cada barra.",
    "small cap": "límite pequeño",
    "big cap": "límite grande",
    "Click a bar to see that day hour by hour.": "Haz clic en una barra para ver ese día hora por hora.",
    "{day}, hour by hour (UTC)": "{day}, hora por hora (UTC)",
    "Close": "Cerrar",
    "Loading…": "Cargando…",
    "No AI usage recorded in this range yet.": "Aún no hay uso de IA registrado en este periodo.",
    "No AI usage recorded on this day.": "No hay uso de IA registrado en este día.",
    "Breakdown": "Desglose",
    "Name": "Nombre",
    "Calls": "Llamadas",
    "Input": "Entrada",
    "Output": "Salida",
    "Cache read": "Lectura de caché",
    "Cache write": "Escritura de caché",
    "Searches": "Búsquedas",
    "% of total": "% del total",
    "Avg cost/call": "Costo prom./llamada",
    "Most expensive calls": "Llamadas más caras",
    "When": "Cuándo",
    "No calls recorded in this range yet.": "Aún no hay llamadas registradas en este periodo.",
    "Costs are estimates from list prices: {prices} per MTok · Web search {cost} per 1,000.": "Los costos son estimaciones con precios de lista: {prices} por MTok · Búsqueda web {cost} por cada 1,000.",
    "Data before {date} was rebuilt from support traces and is approximate.": "Los datos anteriores al {date} se reconstruyeron a partir de los registros de soporte y son aproximados.",
    "Every AI call is tracked since {date}.": "Cada llamada a la IA se registra desde el {date}.",
  });

  const SVG_NS = "http://www.w3.org/2000/svg";
  const STORE_KEY = "panel.usage";
  const TOP_SERIES = 7;
  const OTHER = "__other__";
  const DAY_MS = 86400000;
  const FIELDS = ["calls", "input", "output", "cache_read", "cache_write", "searches", "cost"];
  const RANGES = [["cycle", "This cycle"], ["7", "7 days"], ["30", "30 days"], ["90", "90 days"]];
  const METRICS = [["cost", "Cost"], ["tokens", "Tokens"]];
  const GROUPS = [["feature", "Feature"], ["model", "Model"], ["pool", "Pool"]];
  const COLUMNS = [
    { key: "name", label: "Name" },
    { key: "calls", label: "Calls", kind: "count" },
    { key: "input", label: "Input", kind: "tokens" },
    { key: "output", label: "Output", kind: "tokens" },
    { key: "cache_read", label: "Cache read", kind: "tokens" },
    { key: "cache_write", label: "Cache write", kind: "tokens" },
    { key: "searches", label: "Searches", kind: "count" },
    { key: "cost", label: "Cost", kind: "money" },
    { key: "share", label: "% of total", kind: "share" },
    { key: "avg", label: "Avg cost/call", kind: "money" },
  ];

  // ---- Remembered view state (range, toggles, hidden series, sort) --------------------------------
  const state = loadState();

  function loadState() {
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(STORE_KEY)) || {}; } catch { /* private mode or bad JSON */ }
    const pick = (value, options, fallback) => (options.some(([key]) => key === value) ? value : fallback);
    return {
      range: pick(saved.range, RANGES, "cycle"),
      metric: pick(saved.metric, METRICS, "cost"),
      group: pick(saved.group, GROUPS, "feature"),
      sort: COLUMNS.some((c) => c.key === saved.sort) ? saved.sort : "cost",
      desc: saved.desc !== false,
      hidden: saved.hidden && typeof saved.hidden === "object" ? saved.hidden : {},
    };
  }

  function save() {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(state)); } catch { /* private mode */ }
  }

  // ---- Formatting ---------------------------------------------------------------------------------
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const dayDate = (day) => new Date(`${day}T00:00:00Z`);
  const addDays = (day, n) => new Date(dayDate(day).getTime() + n * DAY_MS).toISOString().slice(0, 10);
  const shortDay = (day) => dayDate(day).toLocaleDateString(locale(), { month: "short", day: "numeric", timeZone: "UTC" });
  const longDay = (day) => dayDate(day).toLocaleDateString(locale(), { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" });
  const fullDate = (iso) => new Date(iso).toLocaleDateString(locale(), { year: "numeric", month: "short", day: "numeric" });
  const fixed = (v, digits) => v.toLocaleString(locale(), { minimumFractionDigits: digits, maximumFractionDigits: digits });

  function dayRange(from, to) {
    const days = [];
    for (let day = from; day <= to; day = addDays(day, 1)) days.push(day);
    return days;
  }

  function money(value) {
    const v = Math.abs(Number(value) || 0);
    return `${value < 0 ? "-" : ""}$${fixed(v, v > 0 && v < 0.01 ? 4 : 2)}`;
  }

  // Whole dollars stay short ("$100") on chart labels.
  const roundMoney = (v) => (Number.isInteger(v) ? `$${v.toLocaleString(locale())}` : money(v));

  function compact(value) {
    const v = Number(value) || 0;
    for (const [size, unit] of [[1e9, "B"], [1e6, "M"], [1e3, "k"]]) {
      if (Math.abs(v) >= size * 0.9995) return `${fixed(v / size, 1).replace(/[.,]0$/, "")}${unit}`;
    }
    return String(Math.round(v));
  }

  const tokens = (n) => h("span", { title: Number(n || 0).toLocaleString(locale()) }, compact(n));
  const count = (n) => Number(n || 0).toLocaleString(locale());
  const small = (...children) => h("div", { class: "muted small" }, ...children);

  function swatch(color) {
    const el = h("span", { class: "swatch", "aria-hidden": "true" });
    el.style.background = color;
    return el;
  }

  function modelName(id) {
    const m = /^claude-([a-z]+)-(\d+)-(\d+)/.exec(id || "");
    return m ? `${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2]}.${m[3]}` : String(id);
  }

  function featureName(feature, data) {
    if (data.feature_labels && data.feature_labels[feature]) return data.feature_labels[feature];
    const words = String(feature || "").replace(/[_-]+/g, " ").trim();
    return words ? words[0].toUpperCase() + words.slice(1) : "—";
  }

  function seriesName(key, data) {
    if (key === OTHER) return t("Other");
    if (state.group === "model") return modelName(key);
    if (state.group === "pool") return { small: t("Small pool"), big: t("Big pool") }[key] || key;
    return featureName(key, data);
  }

  // ---- Data shaping -------------------------------------------------------------------------------
  const tokenSum = (r) => (r.input || 0) + (r.output || 0) + (r.cache_read || 0) + (r.cache_write || 0);
  const metricOf = (r) => (state.metric === "cost" ? r.cost || 0 : tokenSum(r));
  const keyOf = (r) => String(r[state.group] ?? "");

  function aggregate(rows, keyFn) {
    const groups = new Map();
    for (const r of rows) {
      const key = keyFn(r);
      const sum = groups.get(key) || Object.fromEntries([["key", key], ...FIELDS.map((f) => [f, 0])]);
      for (const f of FIELDS) sum[f] += Number(r[f]) || 0;
      groups.set(key, sum);
    }
    return [...groups.values()];
  }

  // The top series by cost in the range keep their own color; the rest fold into "Other".
  // Ranked by cost whatever the metric, so toggling Cost/Tokens never repaints a series.
  function palette(rows) {
    const ranked = aggregate(rows, keyOf).sort((a, b) => b.cost - a.cost || tokenSum(b) - tokenSum(a));
    const slot = new Map(ranked.slice(0, TOP_SERIES).map((a, i) => [a.key, i + 1]));
    return {
      bucket: (key) => (slot.has(key) ? key : OTHER),
      color: (key) => (slot.has(key) ? `var(--series-${slot.get(key)})` : "var(--series-other)"),
    };
  }

  // Series bottom-up: biggest total first, "Other" always on top of the stack.
  function seriesFor(rows, colors, data) {
    const totals = new Map();
    for (const r of rows) {
      const key = colors.bucket(keyOf(r));
      totals.set(key, (totals.get(key) || 0) + metricOf(r));
    }
    const hidden = new Set(state.hidden[state.group] || []);
    return [...totals].sort((a, b) => (a[0] === OTHER) - (b[0] === OTHER) || b[1] - a[1])
      .map(([key]) => ({ key, name: seriesName(key, data), color: colors.color(key), hidden: hidden.has(key) }));
  }

  // One Map(series -> value) per slot (day or hour).
  function slotValues(rows, slotKeys, slotOf, colors) {
    const index = new Map(slotKeys.map((k, i) => [k, i]));
    const slots = slotKeys.map(() => new Map());
    for (const r of rows) {
      const i = index.get(slotOf(r));
      if (i === undefined) continue;
      const key = colors.bucket(keyOf(r));
      slots[i].set(key, (slots[i].get(key) || 0) + metricOf(r));
    }
    return slots;
  }

  function projection(data) {
    const b = data.budget;
    const today = String(data.now).slice(0, 10);
    const past = data.cycle_daily.filter((d) => d.day < today).slice(-7);
    const avg = past.length ? past.reduce((s, d) => s + d.cost, 0) / past.length
      : b.days_elapsed > 0 ? b.spent_usd / b.days_elapsed : 0;
    const remaining = Math.max(0, b.days_total - b.days_elapsed);
    const total = b.spent_usd + avg * remaining;
    const crossDay = total > b.monthly_usd && b.spent_usd < b.monthly_usd && avg > 0
      ? addDays(b.cycle_start, Math.floor(b.days_elapsed + (b.monthly_usd - b.spent_usd) / avg)) : null;
    return { today, avg, remaining, total, crossDay };
  }

  function cacheSavings(data) {
    let usd = 0;
    let read = 0;
    let fresh = 0;
    for (const r of data.rows) {
      const price = data.prices && data.prices[r.model];
      if (price) usd += ((r.cache_read || 0) * (price.input - price.cache_read)) / 1e6;
      read += r.cache_read || 0;
      fresh += (r.input || 0) + (r.cache_write || 0);
    }
    return { usd, rate: read + fresh ? read / (read + fresh) : 0 };
  }

  // ---- SVG chart plumbing -------------------------------------------------------------------------
  function svg(tag, attrs, ...children) {
    const el = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attrs || {})) if (value !== null && value !== undefined) el.setAttribute(key, value);
    for (const child of children) el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    return el;
  }

  function niceScale(max, ticks) {
    const raw = (max > 0 ? max : 1) / ticks;
    const mag = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw * 0.999);
    return { step, top: Math.ceil((max > 0 ? max : 1) / step - 1e-9) * step };
  }

  function axisFormat(metric, step) {
    if (metric === "tokens") return compact;
    const decimals = (String(Number(step.toPrecision(3))).split(".")[1] || "").length;
    const digits = Number.isInteger(step) ? 0 : Math.min(4, Math.max(2, decimals));
    return (v) => `$${fixed(v, v === 0 ? 0 : digits)}`;
  }

  // Plot box sized in real pixels (viewBox = rendered size), left margin fit to the y labels.
  function frame(width, scale, fmt) {
    const labels = [];
    for (let i = 0; i * scale.step <= scale.top + 1e-9; i++) labels.push([i * scale.step, fmt(i * scale.step)]);
    const H = width < 520 ? 220 : 280;
    const geo = { W: width, H, left: Math.ceil(Math.max(...labels.map(([, s]) => s.length)) * 6.6) + 14, right: width - 16, top: 16, bottom: H - 26, labels };
    geo.y = (v) => geo.bottom - (v / scale.top) * (geo.bottom - geo.top);
    return geo;
  }

  function chartSvg(geo) {
    return svg("svg", { viewBox: `0 0 ${geo.W} ${geo.H}`, width: geo.W, height: geo.H });
  }

  function yGrid(root, geo) {
    for (const [value, label] of geo.labels) {
      const y = Math.round(geo.y(value)) + 0.5;
      root.append(
        svg("line", { class: value ? "grid-line" : "axis-line", x1: geo.left, x2: geo.right, y1: y, y2: y }),
        svg("text", { x: geo.left - 8, y: y + 4, "text-anchor": "end" }, label));
    }
  }

  // Labels every few slots, counted back from the newest so the latest day is always named.
  function xLabels(root, geo, n, xAt, label) {
    const every = Math.ceil(n / Math.max(2, Math.floor((geo.right - geo.left) / 64)));
    for (let i = n - 1; i >= 0; i -= every) {
      const x = xAt(i);
      const anchor = x > geo.W - 24 ? "end" : x < geo.left + 12 ? "start" : "middle";
      root.append(svg("text", { x: anchor === "end" ? geo.W - 2 : x, y: geo.H - 8, "text-anchor": anchor }, label(i)));
    }
  }

  function tipContent(title, rows) {
    return [h("div", { class: "tip-title" }, title),
      rows.map(([name, value, color]) => h("div", { class: color ? "tip-row" : "tip-row strong" },
        color ? swatch(color) : null, h("span", {}, name), h("span", { class: "tip-value" }, value)))];
  }

  // Focusable chart box: draws at its own pixel width (redrawn on resize), and turns pointer, touch
  // and arrow keys into a slot index. draw(width) -> {root, count, index(x), mark(i) -> {x, y, content}|null}.
  function chartFrame(label, hint, draw, onPick) {
    const tip = h("div", { class: "chart-tip", hidden: true, "aria-live": "polite" });
    const box = h("div", { class: onPick ? "chart pickable" : "chart", tabindex: "0", role: "group", "aria-label": `${label} ${hint}` }, tip);
    let width = 0;
    let chart = null;
    let focus = -1;
    let timer = 0;

    const show = (i) => {
      focus = i;
      const at = chart && i >= 0 ? chart.mark(i) : null;
      if (!at) {
        if (chart) chart.mark(-1);
        tip.hidden = true;
        return;
      }
      tip.replaceChildren(...at.content.flat());
      tip.hidden = false;
      const ratio = box.clientWidth / width;
      const x = at.x * ratio;
      let left = x + 14;
      if (left + tip.offsetWidth > box.clientWidth) left = x - 14 - tip.offsetWidth;
      tip.style.left = `${Math.max(0, left)}px`;
      tip.style.top = `${clamp(at.y * ratio - tip.offsetHeight - 10, 0, Math.max(0, box.clientHeight - tip.offsetHeight))}px`;
    };

    const paint = () => {
      const w = Math.floor(box.clientWidth);
      if (!w || w === width) return;
      width = w;
      chart = draw(w);
      chart.root.setAttribute("role", "img");
      chart.root.setAttribute("aria-label", label);
      const old = box.querySelector("svg");
      if (old) old.replaceWith(chart.root); else box.prepend(chart.root);
      if (!tip.hidden) show(focus);
    };

    const observer = new ResizeObserver(() => {
      if (!box.isConnected) { if (width) observer.disconnect(); return; }
      clearTimeout(timer);
      if (width) timer = setTimeout(paint, 120); else paint();
    });
    observer.observe(box);

    const slotAt = (e) => {
      const rect = chart.root.getBoundingClientRect();
      return chart.index((e.clientX - rect.left) * (width / rect.width));
    };
    box.addEventListener("pointermove", (e) => { if (chart) show(slotAt(e)); });
    box.addEventListener("pointerdown", (e) => { if (chart) show(slotAt(e)); });
    box.addEventListener("pointerleave", (e) => { if (e.pointerType === "mouse") show(-1); });
    box.addEventListener("blur", () => show(-1));
    if (onPick) box.addEventListener("click", (e) => { if (chart && slotAt(e) >= 0) onPick(slotAt(e)); });
    box.addEventListener("keydown", (e) => {
      if (!chart || !chart.count) return;
      const last = chart.count - 1;
      const moves = { ArrowLeft: focus - 1, ArrowRight: focus + 1, Home: 0, End: last };
      if (e.key in moves) {
        e.preventDefault();
        show(focus < 0 ? last : clamp(moves[e.key], 0, last));
      } else if ((e.key === "Enter" || e.key === " ") && onPick && focus >= 0) {
        e.preventDefault();
        onPick(focus);
      } else if (e.key === "Escape") show(-1);
    });
    return box;
  }

  // ---- Charts -------------------------------------------------------------------------------------
  // slots: [{title, label, values: Map(series -> value)}]; series bottom-up from seriesFor().
  function stackedChart({ slots, series, caps, label, onPick }) {
    const shown = series.filter((s) => !s.hidden);
    const totals = slots.map((slot) => shown.reduce((sum, s) => sum + (slot.values.get(s.key) || 0), 0));
    const value = state.metric === "cost" ? money : compact;
    const hint = onPick ? t("Use the arrow keys to read each bar, Enter opens that day.") : t("Use the arrow keys to read each bar.");

    return chartFrame(label, hint, (width) => {
      const scale = niceScale(Math.max(0, ...totals, ...caps.map((c) => c.value)), width < 520 ? 4 : 5);
      const geo = frame(width, scale, axisFormat(state.metric, scale.step));
      const root = chartSvg(geo);
      const band = (geo.right - geo.left) / slots.length;
      const gap = band >= 6 ? Math.min(6, band * 0.2) : band >= 3 ? 1 : 0;
      const hover = svg("rect", { class: "hover-band", y: geo.top, width: band, height: geo.bottom - geo.top, visibility: "hidden" });
      root.append(hover);
      yGrid(root, geo);

      slots.forEach((slot, i) => {
        let base = 0;
        for (const s of shown) {
          const v = slot.values.get(s.key) || 0;
          if (v <= 0) continue;
          const y0 = geo.y(base);
          const y1 = geo.y(base + v);
          // 1px surface gap between stacked segments.
          const rect = svg("rect", { x: geo.left + i * band + gap / 2, y: y1, width: Math.max(0.5, band - gap), height: Math.max(0.5, y0 - y1 - (base > 0 ? 1 : 0)) });
          rect.style.fill = s.color;
          root.append(rect);
          base += v;
        }
      });

      for (const cap of caps) {
        const y = Math.round(geo.y(cap.value)) + 0.5;
        root.append(svg("line", { class: "cap-line", x1: geo.left, x2: geo.right, y1: y, y2: y }),
          svg("text", { class: "chart-note", x: geo.right, y: y - 5, "text-anchor": "end" }, cap.label));
      }
      xLabels(root, geo, slots.length, (i) => geo.left + (i + 0.5) * band, (i) => slots[i].label);

      return {
        root,
        count: slots.length,
        index: (x) => clamp(Math.floor((x - geo.left) / band), 0, slots.length - 1),
        mark(i) {
          hover.setAttribute("visibility", i < 0 ? "hidden" : "visible");
          if (i < 0) return null;
          hover.setAttribute("x", geo.left + i * band);
          const parts = shown.map((s) => [s.name, slots[i].values.get(s.key) || 0, s.color])
            .filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]);
          return {
            x: geo.left + (i + 0.5) * band,
            y: geo.y(totals[i]),
            content: tipContent(slots[i].title, [[t("Total"), value(totals[i])], ...parts.map(([n, v, c]) => [n, value(v), c])]),
          };
        },
      };
    }, onPick);
  }

  function cycleChart(data, proj) {
    const b = data.budget;
    const n = Math.max(1, b.days_total);
    let cum = 0;
    const points = data.cycle_daily.map((d, i) => ({ day: d.day, cost: d.cost, cum: (cum += d.cost), x: i + 1 }));
    const last = points[points.length - 1];
    if (last) last.x = clamp(b.days_elapsed, points.length - 1, points.length); // today is still running
    const end = shortDay(addDays(b.cycle_end, -1));
    const label = t("Spending this cycle: {spent} of {budget}, projected {projected} by {end}.",
      { spent: money(b.spent_usd), budget: money(b.monthly_usd), projected: money(proj.total), end });

    return chartFrame(label, t("Use the arrow keys to read each day."), (width) => {
      // Far under budget, a $0-to-budget axis flattens the spend into the floor: zoom to the spend instead.
      const peak = Math.max(proj.total, cum);
      const zoomed = b.monthly_usd > 0 && peak < b.monthly_usd * 0.4;
      const scale = niceScale(zoomed ? peak * 1.15 : Math.max(b.monthly_usd, peak), width < 520 ? 4 : 5);
      const geo = frame(width, scale, axisFormat("cost", scale.step));
      const x = (v) => geo.left + (v / n) * (geo.right - geo.left);
      const path = (pts, cls) => svg("path", { class: cls, d: pts.map(([px, py], i) => `${i ? "L" : "M"}${x(px).toFixed(1)},${geo.y(py).toFixed(1)}`).join("") });
      const root = chartSvg(geo);
      yGrid(root, geo);
      if (zoomed) {
        const paceTop = Math.min(scale.top, b.monthly_usd);
        root.append(
          path([[0, 0], [(n * paceTop) / b.monthly_usd, paceTop]], "pace-line"),
          svg("text", { class: "chart-note", x: geo.right, y: geo.top + 12, "text-anchor": "end" }, t("↑ {amount} budget is above this chart", { amount: roundMoney(b.monthly_usd) })));
      } else {
        const by = Math.round(geo.y(b.monthly_usd)) + 0.5;
        root.append(
          path([[0, 0], [n, b.monthly_usd]], "pace-line"),
          svg("line", { class: "budget-line", x1: geo.left, x2: geo.right, y1: by, y2: by }),
          svg("text", { class: "chart-note", x: geo.right, y: by - 5, "text-anchor": "end" }, t("{amount} budget", { amount: roundMoney(b.monthly_usd) })));
      }
      if (last && last.x < n) root.append(path([[last.x, last.cum], [n, proj.total]], "proj-line"));
      root.append(path([[0, 0], ...points.map((p) => [p.x, p.cum])], "cum-line"));
      xLabels(root, geo, n, (i) => x(i + 1), (i) => shortDay(addDays(b.cycle_start, i)));
      const cross = svg("line", { class: "crosshair", y1: geo.top, y2: geo.bottom, visibility: "hidden" });
      const dot = svg("circle", { class: "dot", r: 4, visibility: "hidden" });
      root.append(cross, dot);

      return {
        root,
        count: points.length,
        index: (px) => (points.length ? clamp(Math.ceil(((px - geo.left) / (geo.right - geo.left)) * n) - 1, 0, points.length - 1) : -1),
        mark(i) {
          for (const el of [cross, dot]) el.setAttribute("visibility", i < 0 ? "hidden" : "visible");
          if (i < 0) return null;
          const p = points[i];
          const px = Math.round(x(p.x)) + 0.5;
          cross.setAttribute("x1", px);
          cross.setAttribute("x2", px);
          dot.setAttribute("cx", x(p.x));
          dot.setAttribute("cy", geo.y(p.cum));
          return { x: x(p.x), y: geo.y(p.cum), content: tipContent(longDay(p.day), [[t("Spent that day"), money(p.cost)], [t("Total so far"), money(p.cum)]]) };
        },
      };
    });
  }

  // ---- Page pieces --------------------------------------------------------------------------------
  // Segmented buttons; keeps its own aria-pressed in sync and reports the new key.
  function segmented(label, options, value, onChange) {
    const group = h("div", { class: "seg", role: "group", "aria-label": label });
    for (const [key, text] of options) {
      group.append(h("button", {
        type: "button", "aria-pressed": String(key === value), "data-focus": `${label}:${key}`,
        onclick: () => {
          if (key === value) return;
          value = key;
          for (const btn of group.children) btn.setAttribute("aria-pressed", String(btn.dataset.focus === `${label}:${key}`));
          onChange(key);
        },
      }, t(text)));
    }
    return group;
  }

  // Rebuilding a section drops the focused control; put focus back on its replacement.
  function keepFocus(fn) {
    const key = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.focus : null;
    fn();
    const again = key && document.querySelector(`[data-focus="${CSS.escape(key)}"]`);
    if (again) again.focus();
  }

  function stat(label, value, ...extra) {
    return h("div", { class: "card stat" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value), extra);
  }

  function kpiCards(data, proj, onReset) {
    const b = data.budget;
    const lastDay = shortDay(addDays(b.cycle_end, -1));
    const [tone, verdict] = proj.total > b.monthly_usd ? ["danger", t("Over budget by {amount}", { amount: money(proj.total - b.monthly_usd) })]
      : proj.total > b.monthly_usd * 0.85 ? ["warn", t("Close to the limit")] : ["ok", t("On track")];
    const todayCost = (data.cycle_daily.find((d) => d.day === proj.today) || { cost: 0 }).cost;
    const todayTokens = (data.today_tokens.small || 0) + (data.today_tokens.big || 0);
    const [trend, arrow] = todayCost > proj.avg ? ["up", "▲"] : todayCost < proj.avg ? ["down", "▼"] : ["", "="];
    const daysLeft = Math.ceil(proj.remaining);
    const cache = cacheSavings(data);
    const searches = data.rows.reduce((sum, r) => sum + (r.searches || 0), 0);
    return [
      stat(t("Spent this cycle"), money(b.spent_usd),
        small(t("of {budget}", { budget: money(b.monthly_usd) })),
        h("meter", { min: "0", max: String(b.monthly_usd), value: String(Math.min(b.spent_usd, b.monthly_usd)), low: String(b.monthly_usd * 0.7), high: String(b.monthly_usd * 0.9), optimum: "0", "aria-label": t("Spent this cycle") }),
        small(t("Cycle {start} → {end}", { start: shortDay(b.cycle_start), end: lastDay }))),
      stat(t("Projected by {date}", { date: lastDay }), money(proj.total),
        h("div", {}, badge(verdict, tone)),
        proj.crossDay ? small(t("Would reach {budget} on {date}", { budget: roundMoney(b.monthly_usd), date: shortDay(proj.crossDay) })) : null),
      stat(t("Today"), money(todayCost),
        small(t("{tokens} tokens", { tokens: compact(todayTokens) })),
        data.limits_reset_at ? small(t("Reset today at {time} UTC", { time: new Date(data.limits_reset_at).toISOString().slice(11, 16) })) : null,
        can("settings.edit") ? h("button", { class: "btn small ghost", type: "button", onclick: (e) => onReset(e.currentTarget) }, t("Reset today's limits")) : null,
        small(h("span", { class: `trend ${trend}`, "aria-hidden": "true" }, arrow), " ", t("vs 7-day avg {amount}", { amount: money(proj.avg) }))),
      stat(t("Left per day"), daysLeft > 0 ? money(Math.max(0, b.monthly_usd - b.spent_usd) / proj.remaining) : "—",
        small(daysLeft > 1 ? t("for {count} more days", { count: daysLeft }) : daysLeft === 1 ? t("for 1 more day") : t("Cycle ends today"))),
      stat(t("Cache savings"), money(cache.usd),
        small(t("{pct}% hit rate", { pct: fixed(cache.rate * 100, 1) })),
        small(t("Cached prompt reads"))),
      stat(t("Web searches"), count(searches), small(money(searches * (data.search_usd || 0)))),
    ];
  }

  function lineKey(cls, text) {
    return h("span", { class: "legend-key" }, h("span", { class: `line-key ${cls}`, "aria-hidden": "true" }), text);
  }

  function cycleSection(data, proj) {
    if (!data.cycle_daily.some((d) => d.cost > 0)) return h("div", { class: "empty" }, t("No AI usage recorded in this range yet."));
    return [
      cycleChart(data, proj),
      h("div", { class: "chart-legend" },
        lineKey("cum", t("Spent")), lineKey("proj", t("Projection")), lineKey("budget", t("Budget")), lineKey("pace", t("Even pace"))),
    ];
  }

  function legend(series, onToggle) {
    return h("div", { class: "chart-legend" }, series.map((s) => h("button", {
      type: "button", "aria-pressed": String(!s.hidden), "data-focus": `legend:${s.key}`, onclick: () => onToggle(s.key),
    }, swatch(s.color), s.name)));
  }

  function shareCell(pct) {
    const fill = h("span");
    fill.style.width = `${clamp(pct, 0, 100)}%`;
    return h("div", { class: "share" }, h("span", { class: "share-bar", "aria-hidden": "true" }, fill), `${fixed(pct, 1)}%`);
  }

  function breakdownTable(data, colors, onSort) {
    const items = aggregate(data.rows, keyOf).map((a) => ({ ...a, name: seriesName(a.key, data), avg: a.calls ? a.cost / a.calls : 0 }));
    if (!items.length) return h("div", { class: "empty" }, t("No AI usage recorded in this range yet."));
    const total = Object.fromEntries(FIELDS.map((f) => [f, items.reduce((sum, r) => sum + r[f], 0)]));
    total.avg = total.calls ? total.cost / total.calls : 0;
    const sortValue = (r) => (state.sort === "name" ? r.name.toLowerCase() : state.sort === "share" ? r.cost : r[state.sort]);
    items.sort((a, b) => {
      const x = sortValue(a);
      const y = sortValue(b);
      const order = x < y ? -1 : x > y ? 1 : 0;
      return state.desc ? -order : order;
    });

    const cell = (col, r, isTotal) => {
      if (col.key === "name") return h("td", {}, h("span", { class: "name-cell" }, isTotal ? null : swatch(colors.color(r.key)), isTotal ? t("Total") : r.name));
      const v = r[col.key];
      const content = col.kind === "tokens" ? tokens(v) : col.kind === "money" ? money(v) : col.kind === "count" ? count(v)
        : shareCell(total.cost ? (r.cost / total.cost) * 100 : 0);
      return h("td", { class: "num" }, content);
    };
    const head = COLUMNS.map((col) => {
      const active = state.sort === col.key;
      return h("th", { class: col.key === "name" ? null : "num", "aria-sort": active ? (state.desc ? "descending" : "ascending") : null },
        h("button", { class: "sort-btn", type: "button", "data-focus": `sort:${col.key}`, onclick: () => onSort(col.key) },
          t(col.label), h("span", { class: "sort-mark", "aria-hidden": "true" }, active ? (state.desc ? " ▼" : " ▲") : "")));
    });
    return h("div", { class: "table-wrap" }, h("table", { class: "usage-table" },
      h("thead", {}, h("tr", {}, head)),
      h("tbody", {}, items.map((r) => h("tr", {}, COLUMNS.map((col) => cell(col, r, false))))),
      h("tfoot", {}, h("tr", {}, COLUMNS.map((col) => cell(col, total, true))))));
  }

  function callsTable(data) {
    const num = (content) => h("div", { class: "num" }, content);
    return table([
      { label: t("When"), render: (r) => time(r.at) },
      { label: t("Feature"), render: (r) => featureName(r.feature, data) },
      { label: t("Model"), render: (r) => modelName(r.model) },
      { label: t("Tokens"), render: (r) => num(tokens(tokenSum(r))) },
      { label: t("Searches"), render: (r) => num(count(r.searches)) },
      { label: t("Cost"), render: (r) => num(money(r.cost)) },
    ], data.top_calls || [], { empty: t("No calls recorded in this range yet.") });
  }

  function footnote(data) {
    const prices = Object.entries(data.prices || {}).map(([model, p]) => `${modelName(model)}: ${money(p.input)} / ${money(p.output)}`).join(" · ");
    const lines = [t("Costs are estimates from list prices: {prices} per MTok · Web search {cost} per 1,000.", { prices, cost: roundMoney((data.search_usd || 0) * 1000) })];
    if (data.backfilled_until) lines.push(t("Data before {date} was rebuilt from support traces and is approximate.", { date: fullDate(data.backfilled_until) }));
    if (data.tracking_since) lines.push(t("Every AI call is tracked since {date}.", { date: fullDate(data.tracking_since) }));
    return lines.join(" ");
  }

  const labelled = (text, control) => h("div", { class: "row" }, h("span", { class: "muted small" }, text), control);
  const choiceLabel = (options, key) => t(options.find(([k]) => k === key)[1]);

  Panel.page({
    id: "usage",
    title: "AI usage",
    perm: "status.view",
    async render(view) {
      let data = null;
      let seq = 0;
      let drillDay = null;
      let drillRows = null;
      let dailyChart = null;
      const cards = h("div", { class: "grid" });
      const cycle = h("div", { class: "card" });
      const daily = h("div", { class: "card" });
      const drill = h("div");
      const breakdown = h("div", { class: "card" });
      const calls = h("div", { class: "card usage-calls" });
      const note = h("p", { class: "muted small" });

      const toggleHidden = (key) => {
        const hidden = new Set(state.hidden[state.group] || []);
        if (hidden.has(key)) hidden.delete(key); else hidden.add(key);
        state.hidden[state.group] = [...hidden];
        save();
        keepFocus(paintDaily);
      };

      const sortBy = (key) => {
        state.desc = state.sort === key ? !state.desc : key !== "name";
        state.sort = key;
        save();
        keepFocus(paintTable);
      };

      const openDay = (day) => {
        drillDay = day;
        drillRows = null;
        paintDrill().then(() => drill.scrollIntoView({ block: "nearest" }));
      };

      async function paintDrill() {
        if (!drillDay || drillDay < data.from || drillDay > data.to) {
          drillDay = null;
          drill.replaceChildren();
          return;
        }
        const day = drillDay;
        const close = h("button", {
          class: "btn small ghost", type: "button", "data-focus": "drill-close",
          onclick: () => { drillDay = null; drillRows = null; drill.replaceChildren(); if (dailyChart) dailyChart.focus(); },
        }, t("Close"));
        const head = h("div", { class: "row spread" }, h("h3", {}, t("{day}, hour by hour (UTC)", { day: longDay(day) })), close);
        if (!drillRows) {
          drill.replaceChildren(h("div", { class: "drill" }, head, small(t("Loading…"))));
          let rows;
          try {
            rows = (await api(`/api/usage/day?date=${encodeURIComponent(day)}`)).rows || [];
          } catch (error) {
            if (drillDay === day) drill.replaceChildren(h("div", { class: "drill" }, head, h("p", { class: "error" }, error.message)));
            return;
          }
          if (drillDay !== day) return;
          drillRows = rows;
        }
        if (!drillRows.length) {
          drill.replaceChildren(h("div", { class: "drill" }, head, h("div", { class: "empty" }, t("No AI usage recorded on this day."))));
          return;
        }
        const colors = palette(data.rows);
        const hours = Array.from({ length: 24 }, (_, i) => i);
        const values = slotValues(drillRows, hours, (r) => Number(r.hour), colors);
        const series = seriesFor(drillRows, colors, data);
        const total = drillRows.reduce((sum, r) => sum + metricOf(r), 0);
        const pad = (i) => String(i).padStart(2, "0");
        const features = aggregate(drillRows, (r) => r.feature).sort((a, b) => b.cost - a.cost);
        drill.replaceChildren(h("div", { class: "drill" }, head,
          stackedChart({
            slots: hours.map((i) => ({ label: `${pad(i)}:00`, title: `${pad(i)}:00–${pad((i + 1) % 24)}:00 UTC`, values: values[i] })),
            series, caps: [],
            label: t("Hourly breakdown ({metric}) by {group} on {day}, total {total}.", {
              metric: choiceLabel(METRICS, state.metric).toLowerCase(), group: choiceLabel(GROUPS, state.group).toLowerCase(), day: longDay(day),
              total: state.metric === "cost" ? money(total) : compact(total),
            }),
          }),
          table([
            { label: t("Feature"), render: (r) => featureName(r.key, data) },
            { label: t("Calls"), render: (r) => h("div", { class: "num" }, count(r.calls)) },
            { label: t("Tokens"), render: (r) => h("div", { class: "num" }, tokens(tokenSum(r))) },
            { label: t("Cost"), render: (r) => h("div", { class: "num" }, money(r.cost)) },
          ], features)));
      }

      function paintDaily() {
        const colors = palette(data.rows);
        const days = dayRange(data.from, data.to);
        const series = seriesFor(data.rows, colors, data);
        const toggles = h("div", { class: "row chart-toggles" },
          labelled(t("Metric"), segmented(t("Metric"), METRICS, state.metric, (key) => {
            state.metric = key;
            save();
            keepFocus(paintDaily);
          })),
          labelled(t("Group by"), segmented(t("Group by"), GROUPS, state.group, (key) => {
            state.group = key;
            save();
            keepFocus(() => { paintDaily(); paintTable(); });
          })));
        dailyChart = null;
        let body = h("div", { class: "empty" }, t("No AI usage recorded in this range yet."));
        if (data.rows.length) {
          const values = slotValues(data.rows, days, (r) => r.day, colors);
          const visible = new Set(series.filter((s) => !s.hidden).map((s) => s.key));
          const caps = state.metric === "tokens" && state.group === "pool"
            ? [["small", t("small cap")], ["big", t("big cap")]]
              .filter(([pool]) => data.limits[pool] > 0 && visible.has(pool))
              .map(([pool, text]) => ({ value: data.limits[pool], label: text }))
            : [];
          const total = data.rows.reduce((sum, r) => sum + metricOf(r), 0);
          dailyChart = stackedChart({
            slots: days.map((day, i) => ({ label: shortDay(day), title: longDay(day), values: values[i] })),
            series, caps,
            label: t("Daily breakdown ({metric}) by {group} from {from} to {to}, total {total}.", {
              metric: choiceLabel(METRICS, state.metric).toLowerCase(), group: choiceLabel(GROUPS, state.group).toLowerCase(),
              from: shortDay(data.from), to: shortDay(data.to), total: state.metric === "cost" ? money(total) : compact(total),
            }),
            onPick: (i) => openDay(days[i]),
          });
          body = [small(t("Click a bar to see that day hour by hour.")), dailyChart, legend(series, toggleHidden)];
        }
        daily.replaceChildren(h("h2", {}, t("Daily breakdown")), toggles, ...[body].flat(), drill);
        paintDrill();
      }

      function paintTable() {
        breakdown.replaceChildren(h("h2", {}, t("Breakdown")), breakdownTable(data, palette(data.rows), sortBy));
      }

      const paint = () => keepFocus(() => {
        const proj = projection(data);
        cards.replaceChildren(...kpiCards(data, proj, resetLimits));
        cycle.replaceChildren(h("h2", {}, t("This cycle")), ...[cycleSection(data, proj)].flat());
        paintDaily();
        paintTable();
        calls.replaceChildren(h("h2", {}, t("Most expensive calls")), callsTable(data));
        note.textContent = footnote(data);
      });

      const load = async () => {
        const mine = ++seq;
        const next = await api(`/api/usage?range=${encodeURIComponent(state.range)}`);
        if (mine !== seq) return;
        data = next;
        drillRows = null;
        paint();
      };

      const resetLimits = async (button) => {
        const ok = await dialog(t("Reset today's limits"),
          h("p", {}, t("Reset today's AI token count? The bot starts counting from zero again until 00:00 UTC.")), { confirmLabel: t("Reset") });
        if (!ok) return;
        await run(button, async () => {
          await api("/api/usage/reset-limits", { method: "POST", body: {} });
          await load();
        }, t("Today's limits were reset."));
      };

      const refresh = h("button", { class: "btn small ghost", type: "button" }, t("Refresh"));
      refresh.addEventListener("click", () => run(refresh, load));
      const range = segmented(t("Range"), RANGES, state.range, (key) => {
        state.range = key;
        save();
        run(null, load);
      });
      view.append(h("div", { class: "row spread usage-head" }, h("h1", {}, t("AI usage")), h("div", { class: "row" }, range, refresh)));
      await load();
      view.append(cards, cycle, daily, breakdown, calls, note);
    },
  });
})();
