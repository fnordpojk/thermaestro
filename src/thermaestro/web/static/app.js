// Thermaestro's own browser code: the history chart. Everything else is plain HTML
// and htmx. Loaded as a file, never inline, so the page's CSP can forbid inline code.
"use strict";

// A time zone not chosen yet: preselect the browser's own, if the list has it.
(function () {
  const select = document.querySelector("select[data-detect-zone]");
  if (!select) {
    return;
  }
  let zone = "";
  try {
    zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  } catch (e) {
    return;
  }
  for (const option of select.options) {
    if (option.value === zone) {
      option.selected = true;
      return;
    }
  }
})();

(function () {
  const chart = document.getElementById("chart");
  if (!chart || (chart.dataset.kind === "number" && typeof uPlot === "undefined")) {
    return;
  }
  const decimal = chart.dataset.decimal || ".";
  const digits = Number(chart.dataset.digits || 1);
  let plot = null;

  // Numbers as the page's language writes them (the server passes its decimal sign,
  // since the browser's own formatting can differ from the rest of the page).
  function format(value) {
    if (value === null || value === undefined) {
      return "–";
    }
    const text = value.toFixed(digits);
    return text.replace(".", decimal);
  }

  // Times as the page writes them: the house's time zone, the user's clock and dates.
  const zone = chart.dataset.zone || "UTC";
  const locale = chart.dataset.locale || undefined;
  const clock = chart.dataset.clock;
  const timeFormat = new Intl.DateTimeFormat(locale, {
    timeZone: zone,
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: clock === "12" ? "h12" : clock === "24" ? "h23" : undefined,
  });
  const dateFormat = new Intl.DateTimeFormat(locale, {
    timeZone: zone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });

  function day(seconds) {
    const date = new Date(seconds * 1000);
    if (chart.dataset.dates !== "iso") {
      return dateFormat.format(date);
    }
    const parts = {};
    for (const part of dateFormat.formatToParts(date)) {
      parts[part.type] = part.value;
    }
    return `${parts.year}-${parts.month}-${parts.day}`;
  }

  function moment(seconds) {
    return `${day(seconds)} ${timeFormat.format(new Date(seconds * 1000))}`;
  }

  function ticks(u, values) {
    const span = values.length > 1 ? values[values.length - 1] - values[0] : 0;
    return values.map((v) =>
      span > 2 * 86400 ? day(v) : timeFormat.format(new Date(v * 1000)),
    );
  }

  // Axes, ticks and grid in the page's text and line colors, so the chart reads in
  // light and dark mode alike.
  function axisStyle() {
    const style = getComputedStyle(document.body);
    const fg = style.getPropertyValue("--fg").trim() || "#1d1f21";
    const line = style.getPropertyValue("--line").trim() || "#d9dcde";
    return { stroke: fg, grid: { stroke: line, width: 1 }, ticks: { stroke: line, width: 1 } };
  }

  function message(text) {
    if (plot) {
      plot.destroy();
      plot = null;
    }
    chart.textContent = text;
  }

  async function load(hours) {
    const end = Date.now() / 1000;
    const start = end - hours * 3600;
    const url = `${chart.dataset.source}?start=${start}&end=${end}`;
    let samples;
    try {
      const response = await fetch(url, { credentials: "same-origin" });
      if (!response.ok) {
        throw new Error(String(response.status));
      }
      samples = await response.json();
    } catch (e) {
      message(chart.dataset.failed);
      return;
    }
    if (chart.dataset.kind !== "number") {
      bands(samples, start, end);
      return;
    }
    if (!samples.some((s) => s.quality === "good" && s.value !== null)) {
      message(chart.dataset.empty);
      return;
    }
    // Where a value wasn't valid, the last valid one is drawn on, in grey: the line goes
    // on, and shows it wasn't measured. The grey line starts at the last valid point.
    const times = samples.map((s) => s.t);
    const values = [];
    const held = [];
    let last = null;
    samples.forEach((s, i) => {
      const good = s.quality === "good" && s.value !== null;
      values.push(good ? s.value : null);
      if (good) {
        last = s.value;
        const next = samples[i + 1];
        const endsHere = next && !(next.quality === "good" && next.value !== null);
        held.push(endsHere ? s.value : null);
      } else {
        held.push(last);
        if (last !== null && i > 0 && values[i - 1] !== null) {
          held[i - 1] = values[i - 1];
        }
      }
    });
    const options = {
      width: chart.clientWidth || 800,
      height: 320,
      series: [
        { value: (u, v) => (v === null ? "–" : moment(v)) },
        {
          label: chart.dataset.label || "",
          value: (u, v) => format(v),
          stroke: getComputedStyle(document.body).getPropertyValue("--accent").trim() || "#b5462a",
          width: 2,
          spanGaps: false,
        },
        {
          label: chart.dataset.held || "",
          value: (u, v) => format(v),
          stroke: getComputedStyle(document.body).getPropertyValue("--muted").trim() || "#5f6368",
          width: 2,
          dash: [4, 4],
          spanGaps: false,
        },
      ],
      axes: [
        { values: ticks, ...axisStyle() },
        { values: (u, values) => values.map(format), ...axisStyle() },
      ],
    };
    if (plot) {
      plot.destroy();
    }
    chart.textContent = "";
    plot = new uPlot(options, [times, values, held], chart);
  }

  // Named values (a demand, a pump's state, a switch) can't be a line: each is a band over
  // the time it held, as Home Assistant draws such entities, with its total time below.
  const named = JSON.parse(chart.dataset.labels || "{}");
  const REST = new Set(["off", "idle", "stopped", "no"]);

  function name(text) {
    const shown = named[text] || text;
    return shown.charAt(0).toUpperCase() + shown.slice(1);
  }

  function state(s) {
    if (s.quality !== "good") {
      return null;
    }
    if (chart.dataset.kind === "switch") {
      return s.value === null ? null : s.value ? "on" : "off";
    }
    return s.text !== null ? s.text : s.value === null ? null : String(s.value);
  }

  function span(seconds) {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.round((seconds % 3600) / 60);
    const parts = [];
    if (hours) {
      parts.push(chart.dataset.hourText.replace("%(n)s", hours));
    }
    if (minutes || !hours) {
      parts.push(chart.dataset.minuteText.replace("%(n)s", minutes));
    }
    return parts.join(" ");
  }

  function bands(samples, start, end) {
    if (plot) {
      plot.destroy();
      plot = null;
    }
    // Each sample holds until the next; consecutive ones with the same value are one band.
    const runs = [];
    samples.forEach((s, i) => {
      const from = Math.max(s.t, start);
      const to = Math.min(i + 1 < samples.length ? samples[i + 1].t : end, end);
      if (to <= from) {
        return;
      }
      const value = state(s);
      const last = runs[runs.length - 1];
      if (last && last.value === value && last.to === from) {
        last.to = to;
      } else {
        runs.push({ value, from, to });
      }
    });
    if (!runs.some((r) => r.value !== null)) {
      message(chart.dataset.empty);
      return;
    }
    const order = [];
    for (const r of runs) {
      if (r.value !== null && !order.includes(r.value)) {
        order.push(r.value);
      }
    }
    let color = 0;
    const classes = {};
    for (const value of order) {
      classes[value] = REST.has(value) ? "band-rest" : `band-${color++ % 6}`;
    }
    chart.textContent = "";
    const band = document.createElement("div");
    band.className = "bands";
    const total = end - start;
    for (const r of runs) {
      const part = document.createElement("span");
      part.className = r.value === null ? "band-invalid" : classes[r.value];
      part.style.left = `${((r.from - start) / total) * 100}%`;
      part.style.width = `${((r.to - r.from) / total) * 100}%`;
      const label = name(r.value === null ? chart.dataset.invalid : r.value);
      part.title = `${label}: ${moment(r.from)} – ${moment(r.to)}`;
      if ((r.to - r.from) / total > 0.08) {
        part.textContent = label;
      }
      band.append(part);
    }
    const axis = document.createElement("div");
    axis.className = "band-axis";
    const marks = [];
    for (let i = 0; i <= 4; i++) {
      marks.push(start + (total * i) / 4);
    }
    for (const text of ticks(null, marks)) {
      const tick = document.createElement("span");
      tick.textContent = text;
      axis.append(tick);
    }
    const legend = document.createElement("ul");
    legend.className = "band-legend";
    for (const value of [...order, null]) {
      const held = runs.filter((r) => r.value === value).reduce((n, r) => n + r.to - r.from, 0);
      if (!held) {
        continue;
      }
      const item = document.createElement("li");
      const swatch = document.createElement("span");
      swatch.className = value === null ? "band-invalid" : classes[value];
      item.append(swatch, `${name(value === null ? chart.dataset.invalid : value)}: ${span(held)}`);
      legend.append(item);
    }
    chart.append(band, axis, legend);
  }

  for (const button of document.querySelectorAll(".ranges button")) {
    button.addEventListener("click", () => {
      for (const other of document.querySelectorAll(".ranges button")) {
        other.setAttribute("aria-pressed", String(other === button));
      }
      load(Number(button.dataset.hours));
    });
  }
  window.addEventListener("resize", () => {
    if (plot) {
      plot.setSize({ width: chart.clientWidth, height: 320 });
    }
  });
  load(24);
})();

// A chart or its table: a pair of buttons, the choice remembered in this browser. Without
// this code, the table shows and the chart stays hidden.
(function () {
  for (const nav of document.querySelectorAll("nav.view-switch[data-switch]")) {
    const name = nav.dataset.switch;
    const key = `view.${name}`;
    let chosen = "chart";
    try {
      chosen = localStorage.getItem(key) || "chart";
    } catch (e) {
      // no storage: the chart, as by default
    }
    const show = (view) => {
      for (const part of document.querySelectorAll(`[data-view-of="${name}"]`)) {
        part.hidden = part.dataset.show !== view;
      }
      for (const button of nav.querySelectorAll("button[data-view]")) {
        button.setAttribute("aria-pressed", String(button.dataset.view === view));
      }
      document.dispatchEvent(new CustomEvent("view-shown", { detail: { name, view } }));
    };
    for (const button of nav.querySelectorAll("button[data-view]")) {
      button.addEventListener("click", () => {
        try {
          localStorage.setItem(key, button.dataset.view);
        } catch (e) {
          // not remembered, but shown
        }
        show(button.dataset.view);
      });
    }
    nav.hidden = false;
    show(chosen);
  }
})();

// Times and numbers as the page writes them: the house's time zone, the user's clock.
function pageFormats(element) {
  const zone = element.dataset.zone || "UTC";
  const locale = element.dataset.locale || undefined;
  const clock = element.dataset.clock;
  const cycle = clock === "12" ? "h12" : clock === "24" ? "h23" : undefined;
  const hourFormat = new Intl.DateTimeFormat(locale, { timeZone: zone, hour: "2-digit", hourCycle: cycle });
  const timeFormat = new Intl.DateTimeFormat(locale, {
    timeZone: zone,
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: cycle,
  });
  const decimal = element.dataset.decimal || ".";
  return {
    hour: (s) => hourFormat.format(new Date(s * 1000)),
    time: (s) => timeFormat.format(new Date(s * 1000)),
    hourOf: (s) => Number(new Intl.DateTimeFormat("en", { timeZone: zone, hour: "numeric", hourCycle: "h23" }).format(new Date(s * 1000))),
    number: (v, digits) => (v === null || v === undefined ? "–" : v.toFixed(digits).replace(".", decimal).replace("-", "−")),
  };
}

function cssColor(name, fallback) {
  return getComputedStyle(document.body).getPropertyValue(name).trim() || fallback;
}

// What one more kWh costs, today and tomorrow: each layer of the stack a band, bottom to
// top, so the top edge is the total; VAT the last band. Negative parts (a spot price below
// zero) stack downward from zero, so the total is drawn as its own line too.
(function () {
  const box = document.getElementById("price-chart");
  if (!box) {
    return;
  }
  const config = JSON.parse(box.dataset.chart || "{}");
  const f = pageFormats(box);
  const COLORS = ["#0b5c99", "#b8500b", "#00785a", "#9c4f80", "#a86a00", "#2a7fb0", "#6b6b6b"];
  let data = null;

  async function day(iso) {
    const response = await fetch(`${box.dataset.source}?day=${iso}`, { credentials: "same-origin" });
    if (!response.ok) {
      throw new Error(String(response.status));
    }
    return response.json();
  }

  async function load() {
    try {
      const days = await Promise.all(config.days.map(day));
      const slots = [];
      days.forEach((d, i) => {
        for (const s of d.slots || []) {
          if (s.total !== null) {
            slots.push({ ...s, day: i, t0: Date.parse(s.start) / 1000, t1: Date.parse(s.end) / 1000 });
          }
        }
      });
      data = { slots, unit: days[0].unit || days[1].unit || "" };
    } catch (e) {
      data = null;
    }
    draw();
  }

  // The layers in the order the first slot has them, then VAT, which is on all of them.
  function layers() {
    const first = data.slots[0];
    const ids = first.parts.map((p) => p.layer);
    const vat = data.slots.some((s) => s.parts.some((p) => p.vat_added));
    return vat ? [...ids, "vat"] : ids;
  }

  function pieces(slot, ids) {
    // Each layer's own value, VAT taken out where it was added; VAT summed on its own.
    const out = {};
    let vat = 0;
    for (const p of slot.parts) {
      out[p.layer] = { value: (p.value || 0) - (p.vat_added || 0), fallback: p.fallback };
      vat += p.vat_added || 0;
    }
    if (ids.includes("vat")) {
      out.vat = { value: vat, fallback: null };
    }
    return out;
  }

  function niceStep(span) {
    const raw = span / 5;
    const power = Math.pow(10, Math.floor(Math.log10(raw)));
    const n = raw / power;
    return (n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10) * power;
  }

  function draw() {
    box.textContent = "";
    if (!data || !data.slots.length) {
      const p = document.createElement("p");
      p.className = "hint";
      p.textContent = config.empty || "";
      box.append(p);
      return;
    }
    const ids = layers();
    const width = box.clientWidth || 800;
    const height = 300;
    const pad = { left: 62, right: 12, top: 22, bottom: 26 };
    const canvas = document.createElement("canvas");
    const ratio = window.devicePixelRatio || 1;
    canvas.width = width * ratio;
    canvas.height = height * ratio;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", `${config.total} (${data.unit})`);
    const ctx = canvas.getContext("2d");
    ctx.scale(ratio, ratio);

    const t0 = data.slots[0].t0;
    const t1 = data.slots[data.slots.length - 1].t1;
    let low = 0;
    let high = 0;
    const stacks = data.slots.map((slot) => {
      const parts = pieces(slot, ids);
      let up = 0;
      let down = 0;
      const bands = ids.map((id) => {
        const v = parts[id] ? parts[id].value : 0;
        const band = v >= 0 ? [up, up + v] : [down + v, down];
        if (v >= 0) {
          up += v;
        } else {
          down += v;
        }
        return { id, from: band[0], to: band[1], fallback: parts[id] && parts[id].fallback };
      });
      low = Math.min(low, down, slot.total);
      high = Math.max(high, up, slot.total);
      return bands;
    });
    const step = niceStep((high - low) || 1);
    low = Math.floor(low / step) * step;
    high = Math.ceil((high * 1.04) / step) * step;
    const x = (t) => pad.left + ((t - t0) / (t1 - t0)) * (width - pad.left - pad.right);
    const y = (v) => pad.top + ((high - v) / (high - low)) * (height - pad.top - pad.bottom);
    const fg = cssColor("--fg", "#1d1f21");
    const line = cssColor("--line", "#d9dcde");
    const muted = cssColor("--muted", "#5f6368");

    // The grid, and the price axis.
    ctx.font = "12px system-ui, sans-serif";
    ctx.fillStyle = muted;
    ctx.strokeStyle = line;
    ctx.lineWidth = 1;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    const digits = step < 0.1 ? 2 : 1;
    for (let v = low; v <= high + step / 2; v += step) {
      ctx.beginPath();
      ctx.moveTo(pad.left, Math.round(y(v)) + 0.5);
      ctx.lineTo(width - pad.right, Math.round(y(v)) + 0.5);
      ctx.stroke();
      ctx.fillText(f.number(v, digits), pad.left - 6, y(v));
    }
    ctx.textBaseline = "top";
    ctx.fillText(data.unit, pad.left - 6, 2);

    // The bands, slot by slot; a fallback's price hatched.
    const hatch = document.createElement("canvas");
    hatch.width = 6;
    hatch.height = 6;
    const h = hatch.getContext("2d");
    h.strokeStyle = "rgba(255,255,255,0.7)";
    h.beginPath();
    h.moveTo(0, 6);
    h.lineTo(6, 0);
    h.stroke();
    const pattern = ctx.createPattern(hatch, "repeat");
    data.slots.forEach((slot, i) => {
      // Whole pixels, so neighbouring slots meet without a seam.
      const left = Math.round(x(slot.t0));
      const right = Math.round(x(slot.t1));
      stacks[i].forEach((band, k) => {
        if (band.to === band.from) {
          return;
        }
        ctx.fillStyle = band.id === "vat" ? "#8c8c8c" : COLORS[k % COLORS.length];
        ctx.fillRect(left, y(band.to), right - left, y(band.from) - y(band.to));
        if (band.fallback && pattern) {
          ctx.fillStyle = pattern;
          ctx.fillRect(left, y(band.to), right - left, y(band.from) - y(band.to));
        }
      });
    });

    // The total as a line, and zero where prices go below it.
    ctx.strokeStyle = fg;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    data.slots.forEach((slot, i) => {
      if (i === 0) {
        ctx.moveTo(x(slot.t0), y(slot.total));
      } else {
        ctx.lineTo(x(slot.t0), y(slot.total));
      }
      ctx.lineTo(x(slot.t1), y(slot.total));
    });
    ctx.stroke();
    if (low < 0) {
      ctx.strokeStyle = fg;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(pad.left, y(0));
      ctx.lineTo(width - pad.right, y(0));
      ctx.stroke();
    }

    // Hours along the bottom; the days named at their midnight.
    ctx.fillStyle = muted;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    for (const slot of data.slots) {
      const hour = f.hourOf(slot.t0);
      const whole = Math.round(slot.t0) % 3600 === 0;
      if (whole && hour % 3 === 0) {
        ctx.fillText(f.hour(slot.t0), x(slot.t0), height - pad.bottom + 6);
      }
      if (whole && hour === 0) {
        ctx.strokeStyle = muted;
        ctx.beginPath();
        ctx.moveTo(Math.round(x(slot.t0)) + 0.5, pad.top - 4);
        ctx.lineTo(Math.round(x(slot.t0)) + 0.5, height - pad.bottom);
        ctx.stroke();
      }
    }
    ctx.textAlign = "left";
    const named = new Set();
    for (const slot of data.slots) {
      if (!named.has(slot.day)) {
        named.add(slot.day);
        ctx.fillStyle = fg;
        ctx.fillText(config.day_names[slot.day], x(slot.t0) + 6, 4);
      }
    }

    // Now.
    const now = Date.now() / 1000;
    if (now > t0 && now < t1) {
      ctx.strokeStyle = cssColor("--accent", "#b5462a");
      ctx.lineWidth = 2;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(x(now), pad.top - 4);
      ctx.lineTo(x(now), height - pad.bottom);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // Pointing at a slot: its time, each layer and the total.
    const tip = document.createElement("div");
    tip.className = "chart-tip";
    tip.hidden = true;
    canvas.addEventListener("mousemove", (event) => {
      const at = t0 + ((event.offsetX - pad.left) / (width - pad.left - pad.right)) * (t1 - t0);
      const i = data.slots.findIndex((s) => s.t0 <= at && at < s.t1);
      if (i < 0) {
        tip.hidden = true;
        return;
      }
      const slot = data.slots[i];
      const parts = pieces(slot, ids);
      tip.textContent = "";
      const head = document.createElement("strong");
      head.textContent = `${config.day_names[slot.day]} ${f.time(slot.t0)}–${f.time(slot.t1)}`;
      tip.append(head);
      const rows = document.createElement("table");
      ids.forEach((id, k) => {
        const row = rows.insertRow();
        const swatch = document.createElement("span");
        swatch.className = "swatch";
        swatch.style.background = id === "vat" ? "#8c8c8c" : COLORS[k % COLORS.length];
        const name = row.insertCell();
        name.append(swatch, ` ${config.names[id] || id}${parts[id] && parts[id].fallback ? " *" : ""}`);
        const value = row.insertCell();
        value.className = "num";
        value.textContent = f.number(parts[id] ? parts[id].value : null, 3);
      });
      const total = rows.insertRow();
      total.className = "total";
      total.insertCell().textContent = config.total;
      const sum = total.insertCell();
      sum.className = "num";
      sum.textContent = f.number(slot.total, 3);
      tip.append(rows);
      tip.hidden = false;
      const left = Math.min(event.offsetX + 14, width - tip.offsetWidth - 4);
      tip.style.left = `${Math.max(0, left)}px`;
      tip.style.top = `${pad.top}px`;
    });
    canvas.addEventListener("mouseleave", () => {
      tip.hidden = true;
    });

    const legend = document.createElement("ul");
    legend.className = "band-legend";
    ids.forEach((id, k) => {
      const item = document.createElement("li");
      const swatch = document.createElement("span");
      swatch.style.background = id === "vat" ? "#8c8c8c" : COLORS[k % COLORS.length];
      item.append(swatch, config.names[id] || id);
      legend.append(item);
    });
    const total = document.createElement("li");
    const mark = document.createElement("span");
    mark.className = "line-swatch";
    total.append(mark, config.total);
    legend.append(total);

    box.append(canvas, tip, legend);
    if (data.slots.some((s) => s.parts.some((p) => p.fallback))) {
      const note = document.createElement("p");
      note.className = "hint";
      note.textContent = config.fallback;
      box.append(note);
    }
  }

  let resized = null;
  window.addEventListener("resize", () => {
    clearTimeout(resized);
    resized = setTimeout(() => {
      if (!box.hidden) {
        draw();
      }
    }, 150);
  });
  document.addEventListener("view-shown", (event) => {
    if (event.detail.name === "prices" && event.detail.view === "chart") {
      draw();
    }
  });
  load();
})();
