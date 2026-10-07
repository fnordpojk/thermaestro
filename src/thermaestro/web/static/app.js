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
