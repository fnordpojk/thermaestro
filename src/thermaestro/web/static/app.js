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
  if (!chart || typeof uPlot === "undefined") {
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
    const numeric = samples.filter((s) => s.value !== null);
    if (numeric.length === 0) {
      message(chart.dataset.empty);
      return;
    }
    // A value that wasn't good leaves a gap rather than a misleading line.
    const times = numeric.map((s) => s.t);
    const values = numeric.map((s) => (s.quality === "good" ? s.value : null));
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
      ],
      axes: [{ values: ticks }, { values: (u, values) => values.map(format) }],
    };
    if (plot) {
      plot.destroy();
    }
    chart.textContent = "";
    plot = new uPlot(options, [times, values], chart);
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
