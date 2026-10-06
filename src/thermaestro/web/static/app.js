// Thermaestro's own browser code: the history chart. Everything else is plain HTML
// and htmx. Loaded as a file, never inline, so the page's CSP can forbid inline code.
"use strict";

(function () {
  const chart = document.getElementById("chart");
  if (!chart || typeof uPlot === "undefined") {
    return;
  }
  const decimal = chart.dataset.decimal || ".";
  let plot = null;

  // Numbers as the page's language writes them (the server passes its decimal sign,
  // since the browser's own formatting can differ from the rest of the page).
  function format(value) {
    if (value === null || value === undefined) {
      return "–";
    }
    const text = (Math.round(value * 10) / 10).toFixed(1);
    return text.replace(".", decimal);
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
        {},
        {
          label: chart.dataset.label || "",
          value: (u, v) => format(v),
          stroke: getComputedStyle(document.body).getPropertyValue("--accent").trim() || "#b5462a",
          width: 2,
          spanGaps: false,
        },
      ],
      axes: [{}, { values: (u, ticks) => ticks.map(format) }],
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
