"use strict";
// AI quality page: answers, handoffs, ratings and tickets per day, what waits on staff, and the eval runs.
// Sibling of usage.js: the same hand-built SVG charts (the CSP allows no chart library) and panel.css tokens.

(() => {
  const { h, api, run, table, t, locale } = Panel;

  Panel.i18n({
    "AI quality": "Calidad de IA",
    "Refresh": "Actualizar",
    "Range": "Periodo",
    "14 days": "14 días",
    "30 days": "30 días",
    "90 days": "90 días",
    "Answers": "Respuestas",
    "today {count}": "hoy {count}",
    "today {count} ({delta} vs 7-day avg)": "hoy {count} ({delta} vs. promedio de 7 días)",
    "Handed off": "Pasadas al staff",
    "Second opinion": "Segunda opinión",
    "{count} of {total}": "{count} de {total}",
    "Helpful": "Útiles",
    "{count} ratings": "{count} calificaciones",
    "1 rating": "1 calificación",
    "No ratings yet": "Aún sin calificaciones",
    "Tickets resolved": "Tickets resueltos",
    "{closed} closed, {opened} opened": "{closed} cerrados, {opened} abiertos",
    "Waiting on staff": "Esperando al staff",
    "{suggestions} suggestions, {answers} 👎 answers": "{suggestions} sugerencias, {answers} respuestas con 👎",
    "Answers per day": "Respuestas por día",
    "Answer": "Respuesta",
    "Clarify": "Aclaración",
    "Handoff": "Pase al staff",
    "Other": "Otros",
    "Total": "Total",
    "Tickets opened and closed": "Tickets abiertos y cerrados",
    "Opened": "Abiertos",
    "Closed": "Cerrados",
    "Answer ratings per day": "Calificaciones de respuestas por día",
    "Not helpful": "No útiles",
    "Eval runs": "Evaluaciones",
    "Pass rate": "Tasa de aprobación",
    "Pass rate over the eval runs": "Tasa de aprobación en las evaluaciones",
    "Date": "Fecha",
    "Cases": "Casos",
    "Pass": "Aprobadas",
    "Score": "Puntaje",
    "Fully correct": "Totalmente correctas",
    "Grounded": "Con sustento",
    "Handoff ok": "Pase correcto",
    "Handoff unneeded": "Pase innecesario",
    "Handoff missing": "Pase faltante",
    "Language": "Idioma",
    "Cost": "Costo",
    "Model": "Modelo",
    "No eval runs on this server yet.": "Aún no hay evaluaciones en este servidor.",
    "No answers in this range yet.": "Aún no hay respuestas en este periodo.",
    "No tickets opened or closed in this range yet.": "Aún no hay tickets abiertos o cerrados en este periodo.",
    "No ratings in this range yet.": "Aún no hay calificaciones en este periodo.",
    "{title} from {from} to {to}.": "{title} del {from} al {to}.",
    "Use the arrow keys to read each day.": "Usa las flechas para leer cada día.",
    "Use the arrow keys to read each run.": "Usa las flechas para leer cada evaluación.",
  });

  const SVG_NS = "http://www.w3.org/2000/svg";
  const STORE_KEY = "panel.ai";
  const RANGES = [["14", "14 days"], ["30", "30 days"], ["90", "90 days"]];
  const MAX_BAR = 24;
  const SEGMENT_GAP = 2;
  // usage.js's series tokens, in its order; "other" is its neutral.
  const COLORS = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)"];
  const NEUTRAL = "var(--series-other)";
  const ANSWER_SERIES = [
    { key: "answer", name: "Answer", color: COLORS[0] },
    { key: "clarify", name: "Clarify", color: COLORS[1] },
    { key: "handoff", name: "Handoff", color: COLORS[2] },
    { key: "other", name: "Other", color: NEUTRAL },
  ];
  const TICKET_SERIES = [
    { key: "tickets_opened", name: "Opened", color: COLORS[0] },
    { key: "tickets_closed", name: "Closed", color: COLORS[1] },
  ];
  const RATING_SERIES = [
    { key: "helpful", name: "Helpful", color: COLORS[0] },
    { key: "not_helpful", name: "Not helpful", color: COLORS[1] },
  ];
  const PASS_SERIES = [{ key: "pass", name: "Pass rate", color: COLORS[0] }];

  // ---- Remembered view state (range) --------------------------------------------------------------
  const state = loadState();

  function loadState() {
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(STORE_KEY)) || {}; } catch { /* private mode or bad JSON */ }
    return { days: RANGES.some(([key]) => key === saved.days) ? saved.days : "14" };
  }

  function save() {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(state)); } catch { /* private mode */ }
  }

  // ---- Formatting (as usage.js) -------------------------------------------------------------------
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const dayDate = (day) => new Date(`${day}T00:00:00Z`);
  const shortDay = (day) => dayDate(day).toLocaleDateString(locale(), { month: "short", day: "numeric", timeZone: "UTC" });
  const longDay = (day) => dayDate(day).toLocaleDateString(locale(), { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" });
  const runDay = (iso) => new Date(iso).toLocaleDateString(locale(), { month: "short", day: "numeric" });
  const runTime = (iso) => new Date(iso).toLocaleString(locale(), { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  const fixed = (v, digits) => v.toLocaleString(locale(), { minimumFractionDigits: digits, maximumFractionDigits: digits });
  const count = (n) => Number(n || 0).toLocaleString(locale());
  const percent = (ratio) => `${Math.round((Number(ratio) || 0) * 100)}%`;
  const share = (part, whole) => (whole > 0 ? percent(part / whole) : "–");
  const small = (...children) => h("div", { class: "muted small" }, ...children);

  function money(value) {
    const v = Math.abs(Number(value) || 0);
    return `${value < 0 ? "-" : ""}$${fixed(v, v > 0 && v < 0.01 ? 4 : 2)}`;
  }

  function modelName(id) {
    const m = /^claude-([a-z]+)-(\d+)-(\d+)/.exec(id || "");
    return m ? `${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2]}.${m[3]}` : String(id);
  }

  function swatch(color) {
    const el = h("span", { class: "swatch", "aria-hidden": "true" });
    el.style.background = color;
    return el;
  }

  // ---- SVG chart plumbing (the parts of usage.js this page needs) ---------------------------------
  function svg(tag, attrs, ...children) {
    const el = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attrs || {})) if (value !== null && value !== undefined) el.setAttribute(key, value);
    for (const child of children) el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    return el;
  }

  // Whole-number steps (1, 2, 5, 10, …): these axes count answers, tickets and ratings.
  function countScale(max, ticks) {
    const peak = Math.max(1, max);
    const raw = peak / ticks;
    const mag = 10 ** Math.floor(Math.log10(Math.max(1, raw)));
    const step = raw <= 1 ? 1 : [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw * 0.999);
    return { step, top: Math.ceil(peak / step - 1e-9) * step };
  }

  const PERCENT_SCALE = { step: 25, top: 100 };

  // Plot box sized in real pixels (viewBox = rendered size), left margin fit to the y labels.
  function frame(width, scale, fmt) {
    const labels = [];
    for (let i = 0; i * scale.step <= scale.top + 1e-9; i++) labels.push([i * scale.step, fmt(i * scale.step)]);
    const H = width < 520 ? 220 : 280;
    const geo = { W: width, H, left: Math.ceil(Math.max(...labels.map(([, s]) => s.length)) * 6.6) + 14, right: width - 16, top: 16, bottom: H - 26, labels };
    geo.y = (v) => geo.bottom - (v / scale.top) * (geo.bottom - geo.top);
    return geo;
  }

  function yGrid(root, geo) {
    for (const [value, label] of geo.labels) {
      const y = Math.round(geo.y(value)) + 0.5;
      root.append(
        svg("line", { class: value ? "grid-line" : "axis-line", x1: geo.left, x2: geo.right, y1: y, y2: y }),
        svg("text", { x: geo.left - 8, y: y + 4, "text-anchor": "end" }, label));
    }
  }

  // Labels every few slots, counted back from the newest so the latest one is always named.
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
  function chartFrame(label, hint, draw) {
    const tip = h("div", { class: "chart-tip", hidden: true, "aria-live": "polite" });
    const box = h("div", { class: "chart", tabindex: "0", role: "group", "aria-label": `${label} ${hint}` }, tip);
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
    box.addEventListener("keydown", (e) => {
      if (!chart || !chart.count) return;
      const last = chart.count - 1;
      const moves = { ArrowLeft: focus - 1, ArrowRight: focus + 1, Home: 0, End: last };
      if (e.key in moves) {
        e.preventDefault();
        show(focus < 0 ? last : clamp(moves[e.key], 0, last));
      } else if (e.key === "Escape") show(-1);
    });
    return box;
  }

  // ---- Charts -------------------------------------------------------------------------------------
  // A column segment; the top one of a stack gets a small rounded top.
  function segment(x, top, width, height, rounded) {
    const r = rounded ? Math.min(3, width / 2, height) : 0;
    const bottom = top + height;
    const d = r > 0
      ? `M${x},${bottom}V${top + r}Q${x},${top} ${x + r},${top}H${x + width - r}Q${x + width},${top} ${x + width},${top + r}V${bottom}Z`
      : `M${x},${bottom}V${top}H${x + width}V${bottom}Z`;
    return svg("path", { d });
  }

  // slots: [{label, title, values: [one number per series]}]; series bottom-up: [{name, color}].
  function columnChart({ slots, series, label }) {
    const totals = slots.map((slot) => slot.values.reduce((sum, v) => sum + v, 0));
    return chartFrame(label, t("Use the arrow keys to read each day."), (width) => {
      const scale = countScale(Math.max(0, ...totals), width < 520 ? 4 : 5);
      const geo = frame(width, scale, count);
      const root = svg("svg", { viewBox: `0 0 ${geo.W} ${geo.H}`, width: geo.W, height: geo.H });
      const band = (geo.right - geo.left) / slots.length;
      const gap = band >= 6 ? Math.min(6, band * 0.2) : band >= 3 ? 1 : 0;
      const barWidth = Math.min(MAX_BAR, Math.max(0.5, band - gap));
      const hover = svg("rect", { class: "hover-band", y: geo.top, width: band, height: geo.bottom - geo.top, visibility: "hidden" });
      root.append(hover);
      yGrid(root, geo);

      slots.forEach((slot, i) => {
        const x = geo.left + i * band + (band - barWidth) / 2;
        const last = slot.values.reduce((found, v, index) => (v > 0 ? index : found), -1);
        let base = 0;
        slot.values.forEach((v, index) => {
          if (v <= 0) return;
          const y0 = geo.y(base) - (base > 0 ? SEGMENT_GAP : 0);
          const y1 = Math.min(geo.y(base + v), y0 - 0.5);
          const path = segment(x, y1, barWidth, y0 - y1, index === last);
          path.style.fill = series[index].color;
          root.append(path);
          base += v;
        });
      });
      xLabels(root, geo, slots.length, (i) => geo.left + (i + 0.5) * band, (i) => slots[i].label);

      return {
        root,
        count: slots.length,
        index: (x) => clamp(Math.floor((x - geo.left) / band), 0, slots.length - 1),
        mark(i) {
          hover.setAttribute("visibility", i < 0 ? "hidden" : "visible");
          if (i < 0) return null;
          hover.setAttribute("x", geo.left + i * band);
          return {
            x: geo.left + (i + 0.5) * band,
            y: geo.y(totals[i]),
            content: tipContent(slots[i].title, [[t("Total"), count(totals[i])],
              ...series.map((s, index) => [t(s.name), count(slots[i].values[index]), s.color])]),
          };
        },
      };
    });
  }

  // Same slot shape as columnChart. asPercent: values are 0-100 on a fixed 0-100% axis.
  function lineChart({ slots, series, label, hint, asPercent, extra }) {
    const value = asPercent ? (v) => `${Math.round(v)}%` : count;
    return chartFrame(label, hint, (width) => {
      const peak = Math.max(0, ...slots.flatMap((slot) => slot.values));
      const scale = asPercent ? PERCENT_SCALE : countScale(peak, width < 520 ? 4 : 5);
      const geo = frame(width, scale, value);
      const root = svg("svg", { viewBox: `0 0 ${geo.W} ${geo.H}`, width: geo.W, height: geo.H });
      const band = (geo.right - geo.left) / slots.length;
      const x = (i) => geo.left + (i + 0.5) * band;
      yGrid(root, geo);
      const cross = svg("line", { class: "crosshair", y1: geo.top, y2: geo.bottom, visibility: "hidden" });
      root.append(cross);
      const dots = series.map((s, index) => {
        const line = svg("path", { class: "series-line", d: slots.map((slot, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${geo.y(slot.values[index]).toFixed(1)}`).join("") });
        line.style.stroke = s.color;
        root.append(line);
        // Few points (eval runs): mark each one, so a single run is still visible.
        if (slots.length <= 20) {
          for (let i = 0; i < slots.length; i++) {
            const point = svg("circle", { class: "series-dot", r: 3, cx: x(i), cy: geo.y(slots[i].values[index]) });
            point.style.fill = s.color;
            root.append(point);
          }
        }
        const dot = svg("circle", { class: "series-dot", r: 4, visibility: "hidden" });
        dot.style.fill = s.color;
        return dot;
      });
      root.append(...dots);
      xLabels(root, geo, slots.length, x, (i) => slots[i].label);

      return {
        root,
        count: slots.length,
        index: (px) => clamp(Math.floor((px - geo.left) / band), 0, slots.length - 1),
        mark(i) {
          for (const el of [cross, ...dots]) el.setAttribute("visibility", i < 0 ? "hidden" : "visible");
          if (i < 0) return null;
          const px = Math.round(x(i)) + 0.5;
          cross.setAttribute("x1", px);
          cross.setAttribute("x2", px);
          dots.forEach((dot, index) => {
            dot.setAttribute("cx", x(i));
            dot.setAttribute("cy", geo.y(slots[i].values[index]));
          });
          return {
            x: x(i),
            y: geo.y(Math.max(...slots[i].values)),
            content: tipContent(slots[i].title, [
              ...series.map((s, index) => [t(s.name), value(slots[i].values[index]), s.color]),
              ...(extra ? extra(i) : []),
            ]),
          };
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

  function stat(label, value, ...extra) {
    return h("div", { class: "card stat" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value), extra);
  }

  function legend(series) {
    return h("div", { class: "chart-legend" }, series.map((s) => h("span", { class: "legend-key series-key" }, swatch(s.color), t(s.name))));
  }

  const sum = (points, pick) => points.reduce((total, p) => total + (Number(pick(p)) || 0), 0);
  const answersOf = (p) => ANSWER_SERIES.reduce((total, s) => total + (Number(p.answers[s.key]) || 0), 0);

  function statCards(data) {
    const points = data.points;
    const answers = sum(points, answersOf);
    const handoffs = sum(points, (p) => p.answers.handoff);
    const escalated = sum(points, (p) => p.escalated);
    const helpful = sum(points, (p) => p.helpful);
    const ratings = helpful + sum(points, (p) => p.not_helpful);
    const opened = sum(points, (p) => p.tickets_opened);
    const closed = sum(points, (p) => p.tickets_closed);
    const resolved = sum(points, (p) => p.tickets_resolved);
    const today = data.today || {};
    const delta = today.delta === null || today.delta === undefined ? null
      : `${today.delta >= 0 ? "+" : "−"}${Math.round(Math.abs(today.delta) * 100)}%`;
    const waiting = data.waiting || {};
    return [
      stat(t("Answers"), count(answers), small(delta === null
        ? t("today {count}", { count: count(today.answers) })
        : t("today {count} ({delta} vs 7-day avg)", { count: count(today.answers), delta }))),
      stat(t("Handed off"), share(handoffs, answers), small(t("{count} of {total}", { count: count(handoffs), total: count(answers) }))),
      stat(t("Second opinion"), share(escalated, answers), small(t("{count} of {total}", { count: count(escalated), total: count(answers) }))),
      stat(t("Helpful"), share(helpful, ratings), small(!ratings ? t("No ratings yet")
        : ratings === 1 ? t("1 rating") : t("{count} ratings", { count: count(ratings) }))),
      stat(t("Tickets resolved"), share(resolved, closed), small(t("{closed} closed, {opened} opened", { closed: count(closed), opened: count(opened) }))),
      stat(t("Waiting on staff"), count((waiting.suggestions || 0) + (waiting.answers_unreviewed || 0)),
        small(t("{suggestions} suggestions, {answers} 👎 answers", { suggestions: count(waiting.suggestions), answers: count(waiting.answers_unreviewed) }))),
    ];
  }

  // One chart card over the days: legend above, chart below, or the empty state.
  function dayCard(data, title, series, pick, chart, empty) {
    const slots = data.points.map((p) => ({ label: shortDay(p.day), title: longDay(p.day), values: series.map((s) => Number(pick(p, s.key)) || 0) }));
    const any = slots.some((slot) => slot.values.some((v) => v > 0));
    const label = t("{title} from {from} to {to}.", { title: t(title), from: shortDay(data.from), to: shortDay(data.to) });
    return h("div", { class: "card" }, h("h2", {}, t(title)), any
      ? [legend(series), chart({ slots, series, label, hint: t("Use the arrow keys to read each day.") })]
      : h("div", { class: "empty" }, t(empty)));
  }

  function evalCard(data) {
    const runs = data.evals || [];
    const card = h("div", { class: "card ai-evals" }, h("h2", {}, t("Eval runs")));
    if (!runs.length) {
      card.append(h("div", { class: "empty" }, t("No eval runs on this server yet.")));
      return card;
    }
    const oldestFirst = [...runs].reverse();
    const num = (content) => h("div", { class: "num" }, content);
    const rate = (key) => (r) => num(percent(r[key]));
    card.append(
      lineChart({
        slots: oldestFirst.map((r) => ({ label: runDay(r.at), title: runTime(r.at), values: [(Number(r.pass) || 0) * 100] })),
        series: PASS_SERIES,
        label: t("Pass rate over the eval runs"),
        hint: t("Use the arrow keys to read each run."),
        asPercent: true,
        extra: (i) => [[t("Cases"), count(oldestFirst[i].n)]],
      }),
      table([
        { label: t("Date"), render: (r) => h("span", { class: "nowrap" }, runTime(r.at)) },
        { label: t("Cases"), render: (r) => num(count(r.n)) },
        { label: t("Pass"), render: rate("pass") },
        { label: t("Score"), render: rate("score") },
        { label: t("Fully correct"), render: rate("correct_full") },
        { label: t("Grounded"), render: rate("grounded") },
        { label: t("Handoff ok"), render: rate("handoff_ok") },
        { label: t("Handoff unneeded"), render: rate("handoff_unneeded") },
        { label: t("Handoff missing"), render: rate("handoff_missing") },
        { label: t("Language"), render: rate("language_ok") },
        { label: t("Cost"), render: (r) => num(r.cost_usd === null || r.cost_usd === undefined ? "—" : money(r.cost_usd)) },
        { label: t("Model"), render: (r) => h("span", { class: "nowrap" }, r.model ? modelName(r.model) : "—") },
      ], runs));
    return card;
  }

  Panel.page({
    id: "ai",
    title: "AI quality",
    perm: "status.view",
    async render(view) {
      let seq = 0;
      const body = h("div");

      const paint = (data) => body.replaceChildren(
        h("div", { class: "grid" }, statCards(data)),
        dayCard(data, "Answers per day", ANSWER_SERIES, (p, key) => p.answers[key], columnChart, "No answers in this range yet."),
        dayCard(data, "Tickets opened and closed", TICKET_SERIES, (p, key) => p[key], lineChart, "No tickets opened or closed in this range yet."),
        dayCard(data, "Answer ratings per day", RATING_SERIES, (p, key) => p[key], columnChart, "No ratings in this range yet."),
        evalCard(data));

      const load = async () => {
        const mine = ++seq;
        const next = await api(`/api/ai/quality?days=${encodeURIComponent(state.days)}`);
        if (mine === seq) paint(next);
      };

      const refresh = h("button", { class: "btn small ghost", type: "button" }, t("Refresh"));
      refresh.addEventListener("click", () => run(refresh, load));
      const range = segmented(t("Range"), RANGES, state.days, (key) => {
        state.days = key;
        save();
        run(null, load);
      });
      view.append(h("div", { class: "row spread usage-head" }, h("h1", {}, t("AI quality")), h("div", { class: "row" }, range, refresh)));
      await load();
      view.append(body);
    },
  });
})();
