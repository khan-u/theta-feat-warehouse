"""This module builds a standalone HTML dashboard from the pipeline's CSV extracts.

The Tableau workbook described in TABLEAU.md is the reporting target for a
Tableau install. This script produces an equivalent, fully offline dashboard
that opens in any browser with no server and no external libraries: it reads the
CSV extracts written to ``warehouse/exports/`` and bakes them, together with a
small amount of hand-written SVG charting, into a single ``index.html``.

The four panels mirror the four Tableau worksheets:

    1. Paired asymmetry per channel   (Load 1 vs Load 3 scatter)
    2. Difference distribution         (per-channel paired differences)
    3. Cycle-Level Symmetry Distribution (symmetry metric histograms by load)
    4. Run Health and coverage         (run history, DQ checks, per subject)

Run it after the pipeline has produced extracts::

    python dashboard/build_dashboard.py
    python dashboard/build_dashboard.py --exports warehouse/exports --out dashboard/index.html
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXPORTS = PROJECT_ROOT / "warehouse" / "exports"
DEFAULT_OUTPUT = PROJECT_ROOT / "dashboard" / "index.html"

# Extract name -> whether the dashboard requires it to render.
EXTRACTS = {
    "channel_paired_asym": True,
    "channel_load_asym": False,
    "cycle_qc_distribution": True,
    "subject_coverage": True,
    "run_health": True,
    "test_results": True,
    "dq_results": True,
}


def read_extract(exports_dir: Path, name: str) -> list[dict[str, str]]:
    """Read one CSV extract into a list of string-keyed dictionaries."""
    path = exports_dir / f"{name}.csv"
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = []
        for row in reader:
            rows.append(dict(row))
        return rows


def to_float(value: str | None) -> float | None:
    """Parse a CSV cell as a float, treating blanks and NULLs as missing."""
    if value is None:
        return None
    text = value.strip()
    if text == "" or text.lower() in ("na", "nan", "none", "null"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def to_bool(value: str | None) -> bool:
    """Parse the boolean spellings DuckDB and pandas emit."""
    if value is None:
        return False
    return value.strip().lower() in ("true", "t", "1")


def load_manifest(exports_dir: Path) -> dict[str, object]:
    path = exports_dir / "manifest.json"
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    # The dashboard is meant to be shareable, so drop the absolute warehouse path
    # to its filename rather than baking a local directory layout into the HTML.
    if isinstance(manifest, dict) and "duckdb_path" in manifest:
        manifest["duckdb_path"] = Path(str(manifest["duckdb_path"])).name
    return manifest


def build_paired_points(paired_rows: list[dict[str, str]], metric_prefix: str) -> list[dict[str, object]]:
    """Extract included-channel (baseline, comparison) pairs for one metric."""
    points = []
    for row in paired_rows:
        if not to_bool(row.get("included")):
            continue
        baseline = to_float(row.get(f"{metric_prefix}_baseline"))
        comparison = to_float(row.get(f"{metric_prefix}_comparison"))
        if baseline is None or comparison is None:
            continue
        points.append(
            {
                "subject_id": row.get("subject_id", ""),
                "channel_label": row.get("channel_label", ""),
                "baseline": baseline,
                "comparison": comparison,
            }
        )
    return points


def build_difference_values(paired_rows: list[dict[str, str]], metric_prefix: str) -> list[float]:
    """Per-channel paired differences (comparison - baseline) for included channels."""
    values = []
    for row in paired_rows:
        if not to_bool(row.get("included")):
            continue
        diff = to_float(row.get(f"{metric_prefix}_diff"))
        if diff is not None:
            values.append(diff)
    return values


def build_qc_series(qc_rows: list[dict[str, str]], metric: str) -> dict[str, list[dict[str, float]]]:
    """Histogram counts per load condition for one symmetry metric.

    Burst membership is collapsed here: the analysis restricts to burst cycles by
    default, but the QC panel shows the full distribution so the reader can see
    the shape the metric takes overall.
    """
    per_load: dict[str, dict[float, float]] = {}
    for row in qc_rows:
        if row.get("metric") != metric:
            continue
        load = row.get("load_condition", "")
        bin_start = to_float(row.get("bin_start"))
        count = to_float(row.get("n_cycles"))
        if bin_start is None or count is None:
            continue
        bucket = per_load.setdefault(load, {})
        bucket[bin_start] = bucket.get(bin_start, 0.0) + count

    series: dict[str, list[dict[str, float]]] = {}
    for load, counts in per_load.items():
        ordered_bins = sorted(counts.keys())
        points = []
        for bin_start in ordered_bins:
            points.append({"bin_start": bin_start, "count": counts[bin_start]})
        series[load] = points
    return series


def summarise_coverage(coverage_rows: list[dict[str, str]]) -> list[dict[str, object]]:
    """One entry per subject with total and included channel counts."""
    per_subject: dict[str, dict[str, float]] = {}
    for row in coverage_rows:
        subject = row.get("subject_id", "")
        totals = per_subject.setdefault(subject, {"n_channels": 0.0, "n_channels_included": 0.0, "n_cycles": 0.0})
        n_channels = to_float(row.get("n_channels")) or 0.0
        n_included = to_float(row.get("n_channels_included")) or 0.0
        n_cycles = to_float(row.get("n_cycles")) or 0.0
        totals["n_channels"] += n_channels
        totals["n_channels_included"] += n_included
        totals["n_cycles"] += n_cycles

    summary = []
    for subject in sorted(per_subject.keys()):
        totals = per_subject[subject]
        summary.append(
            {
                "subject_id": subject,
                "n_channels": int(totals["n_channels"]),
                "n_channels_included": int(totals["n_channels_included"]),
                "n_cycles": int(totals["n_cycles"]),
            }
        )
    return summary


def compute_headline(
    manifest: dict[str, object],
    coverage_rows: list[dict[str, str]],
    paired_rows: list[dict[str, str]],
    run_health_rows: list[dict[str, str]],
    dq_rows: list[dict[str, str]],
) -> dict[str, object]:
    """Top-of-page KPIs."""
    subjects = set()
    total_channels = 0
    total_cycles = 0.0
    for row in coverage_rows:
        subjects.add(row.get("subject_id", ""))
        total_channels += int(to_float(row.get("n_channels")) or 0.0)
        total_cycles += to_float(row.get("n_cycles")) or 0.0

    included_channels = 0
    for row in paired_rows:
        if to_bool(row.get("included")):
            included_channels += 1

    latest_run = run_health_rows[0] if run_health_rows else {}

    dq_errors = 0
    dq_warnings = 0
    dq_total = 0
    for row in dq_rows:
        dq_total += 1
        if not to_bool(row.get("passed")):
            if row.get("severity") == "error":
                dq_errors += 1
            else:
                dq_warnings += 1

    row_counts = manifest.get("row_counts", {}) if isinstance(manifest, dict) else {}

    return {
        "n_subjects": len(subjects),
        "n_channels": total_channels,
        "n_cycles": int(total_cycles),
        "n_channels_included": included_channels,
        "run_id": latest_run.get("run_id", manifest.get("run_id", "n/a")),
        "run_status": latest_run.get("status", "n/a"),
        "dq_total": dq_total,
        "dq_errors": dq_errors,
        "dq_warnings": dq_warnings,
        "row_counts": row_counts,
    }


def collect(exports_dir: Path) -> dict[str, object]:
    """Read every extract and shape it into the payload the page renders."""
    manifest = load_manifest(exports_dir)
    paired_rows = read_extract(exports_dir, "channel_paired_asym")
    qc_rows = read_extract(exports_dir, "cycle_qc_distribution")
    coverage_rows = read_extract(exports_dir, "subject_coverage")
    run_health_rows = read_extract(exports_dir, "run_health")
    test_rows = read_extract(exports_dir, "test_results")
    dq_rows = read_extract(exports_dir, "dq_results")

    # ops.dq_result and ops.test_result accumulate across runs; the check and
    # analysis panels show the latest run only, while run_health keeps the full
    # history. Marts (paired, qc, coverage) are full rebuilds, so they already
    # reflect the current state.
    latest_run_id = None
    if run_health_rows:
        latest_run_id = max(run_health_rows, key=lambda r: r.get("started_at", "")).get("run_id")
    if latest_run_id:
        if test_rows and "run_id" in test_rows[0]:
            test_rows = [r for r in test_rows if r.get("run_id") == latest_run_id]
        if dq_rows and "run_id" in dq_rows[0]:
            dq_rows = [r for r in dq_rows if r.get("run_id") == latest_run_id]

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "manifest": manifest,
        "headline": compute_headline(manifest, coverage_rows, paired_rows, run_health_rows, dq_rows),
        "paired": {
            "ptsym": build_paired_points(paired_rows, "ptsym"),
            "rdsym": build_paired_points(paired_rows, "rdsym"),
        },
        "differences": {
            "ptsym": build_difference_values(paired_rows, "ptsym"),
            "rdsym": build_difference_values(paired_rows, "rdsym"),
        },
        "qc": {
            "time_ptsym": build_qc_series(qc_rows, "time_ptsym"),
            "time_rdsym": build_qc_series(qc_rows, "time_rdsym"),
        },
        "coverage": summarise_coverage(coverage_rows),
        "run_health": run_health_rows,
        "tests": test_rows,
        "dq": dq_rows,
    }
    return payload


def render_html(payload: dict[str, object]) -> str:
    """Wrap the payload and the client-side rendering code in one HTML file."""
    data_json = json.dumps(payload, indent=2)
    return _PAGE_TEMPLATE.replace("__PAYLOAD__", data_json)


def missing_required(exports_dir: Path) -> list[str]:
    """Return the names of required extracts that are absent."""
    missing = []
    for name, required in EXTRACTS.items():
        if required and not (exports_dir / f"{name}.csv").is_file():
            missing.append(name)
    return missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the offline dashboard from CSV extracts.")
    parser.add_argument("--exports", default=str(DEFAULT_EXPORTS), help="directory holding the CSV extracts")
    parser.add_argument("--out", default=str(DEFAULT_OUTPUT), help="path to write the dashboard HTML")
    args = parser.parse_args(argv)

    exports_dir = Path(args.exports).expanduser().resolve()
    output_path = Path(args.out).expanduser().resolve()

    if not exports_dir.is_dir():
        parser.error(
            f"exports directory not found: {exports_dir}. "
            "Run the pipeline first (make demo, or python -m theta_warehouse.cli run-all)."
        )

    missing = missing_required(exports_dir)
    if missing:
        parser.error(
            "missing required extracts: "
            + ", ".join(missing)
            + f". Looked in {exports_dir}."
        )

    payload = collect(exports_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_html(payload), encoding="utf-8")

    headline = payload["headline"]
    print(f"wrote {output_path}")
    print(
        "  subjects={n_subjects} channels={n_channels} "
        "included={n_channels_included} cycles={n_cycles} "
        "dq_errors={dq_errors} dq_warnings={dq_warnings}".format(**headline)
    )
    return 0


_PAGE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Theta Feature Warehouse Dashboard</title>
<style>
  :root {
    --bg: #0f1420;
    --panel: #171d2b;
    --panel-2: #1e2740;
    --ink: #e8edf7;
    --muted: #93a0b8;
    --line: #2a3550;
    --accent: #5aa9ff;
    --good: #46c98b;
    --warn: #f2b84b;
    --bad: #ef6b73;
    --load1: #5aa9ff;
    --load3: #f2b84b;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }
  header {
    padding: 28px 32px 18px;
    border-bottom: 1px solid var(--line);
  }
  header h1 { margin: 0 0 4px; font-size: 22px; letter-spacing: 0.2px; }
  header p { margin: 0; color: var(--muted); }
  .wrap { padding: 24px 32px 64px; max-width: 1280px; margin: 0 auto; }
  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 14px; margin-bottom: 28px; }
  .kpi { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 16px 18px; }
  .kpi .v { font-size: 26px; font-weight: 650; }
  .kpi .l { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.6px; margin-top: 4px; }
  .kpi.good .v { color: var(--good); }
  .kpi.bad .v { color: var(--bad); }
  .kpi.warn .v { color: var(--warn); }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
  @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 18px 20px; margin-bottom: 20px; }
  .panel h2 { margin: 0 0 2px; font-size: 15px; }
  .panel .sub { color: var(--muted); font-size: 12px; margin: 0 0 14px; }
  svg { width: 100%; height: auto; display: block; }
  .axis { stroke: var(--line); stroke-width: 1; }
  .grid-line { stroke: var(--line); stroke-width: 1; stroke-dasharray: 3 4; opacity: 0.5; }
  .ref-line { stroke: var(--muted); stroke-width: 1.2; stroke-dasharray: 5 5; }
  .tick { fill: var(--muted); font-size: 10px; }
  .axis-label { fill: var(--muted); font-size: 11px; }
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--line); }
  th { color: var(--muted); font-weight: 600; text-transform: uppercase; font-size: 11px; letter-spacing: 0.5px; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  .pill { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 11px; font-weight: 600; }
  .pill.pass { background: rgba(70,201,139,0.16); color: var(--good); }
  .pill.error { background: rgba(239,107,115,0.16); color: var(--bad); }
  .pill.warn { background: rgba(242,184,75,0.16); color: var(--warn); }
  .legend { display: flex; gap: 16px; margin-bottom: 8px; color: var(--muted); font-size: 12px; }
  .legend span { display: inline-flex; align-items: center; gap: 6px; }
  .swatch { width: 11px; height: 11px; border-radius: 3px; display: inline-block; }
  .foot { color: var(--muted); font-size: 12px; margin-top: 28px; }
  code { background: var(--panel-2); padding: 1px 6px; border-radius: 5px; }
</style>
</head>
<body>
<header>
  <h1>Theta Feature Warehouse Dashboard</h1>
  <p id="subtitle"></p>
</header>
<div class="wrap">
  <div class="kpis" id="kpis"></div>

  <div class="grid">
    <div class="panel">
      <h2>Channel-Level Paired Asymmetry (Peak-to-Trough)</h2>
      <p class="sub">One mark per included channel. On the diagonal means no load difference.</p>
      <div id="scatter-ptsym"></div>
    </div>
    <div class="panel">
      <h2>Channel-Level Paired Asymmetry (Rise-to-Decay)</h2>
      <p class="sub">Same axes fixed to 0.4-0.6. Systematic displacement is the effect.</p>
      <div id="scatter-rdsym"></div>
    </div>
  </div>

  <div class="grid">
    <div class="panel">
      <h2>Paired difference distribution</h2>
      <p class="sub">Per-channel (Load 3 minus Load 1). Solid line = 0 (null), dashed = mean.</p>
      <div id="hist-diff"></div>
    </div>
    <div class="panel">
      <h2>θ Cycle-Level Symmetry Distribution</h2>
      <p class="sub">Symmetry metric histograms by load. Reference at 0.5 = symmetric.</p>
      <div id="qc-dist"></div>
    </div>
  </div>

  <div class="panel">
    <h2>Permutation test results</h2>
    <p class="sub">Paired test (does asymmetry differ by load?) and one-sample tests against 0.5 (is the waveform symmetric?).</p>
    <div id="tests"></div>
  </div>

  <div class="grid">
    <div class="panel">
      <h2>Per-subject channel coverage</h2>
      <p class="sub">Total channels vs channels that pass the paired-inclusion rule.</p>
      <div id="coverage"></div>
    </div>
    <div class="panel">
      <h2>Data-quality checks</h2>
      <div id="dq"></div>
    </div>
  </div>

  <div class="panel">
    <h2>Run Health</h2>
    <p class="sub">One row per run. Data quality is shown next to the volume it qualifies.</p>
    <div id="run-health"></div>
  </div>

  <p class="foot" id="foot"></p>
</div>

<script>
var DATA = __PAYLOAD__;

function svgEl(name, attrs) {
  var el = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (var key in attrs) {
    if (Object.prototype.hasOwnProperty.call(attrs, key)) {
      el.setAttribute(key, attrs[key]);
    }
  }
  return el;
}

function formatNumber(value) {
  if (value === null || value === undefined) { return "-"; }
  var n = Number(value);
  if (Math.abs(n) >= 1000) { return n.toLocaleString("en-US"); }
  return String(n);
}

function formatFixed(value, digits) {
  if (value === null || value === undefined || value === "") { return "-"; }
  var n = Number(value);
  if (isNaN(n)) { return "-"; }
  return n.toFixed(digits);
}

function subjectColor(subjectId, palette, assigned) {
  if (!(subjectId in assigned)) {
    var index = Object.keys(assigned).length % palette.length;
    assigned[subjectId] = palette[index];
  }
  return assigned[subjectId];
}

function renderSubtitle() {
  var m = DATA.manifest || {};
  var signal = m.signal || {};
  var parts = [];
  parts.push("run " + (DATA.headline.run_id || "n/a"));
  if (signal.fs) { parts.push("fs " + signal.fs + " Hz"); }
  if (signal.f_theta) { parts.push("theta " + signal.f_theta[0] + "-" + signal.f_theta[1] + " Hz"); }
  var analysis = m.analysis || {};
  if (analysis.n_permutations) { parts.push(formatNumber(analysis.n_permutations) + " permutations"); }
  document.getElementById("subtitle").textContent = parts.join("  |  ");
  document.getElementById("foot").textContent =
    "Generated " + DATA.generated_at + " from CSV extracts in warehouse/exports/.";
}

function renderKpis() {
  var h = DATA.headline;
  var container = document.getElementById("kpis");
  var dqClass = h.dq_errors > 0 ? "bad" : (h.dq_warnings > 0 ? "warn" : "good");
  var dqValue = h.dq_errors > 0 ? (h.dq_errors + " errors") : (h.dq_warnings + " warn");
  var statusClass = h.run_status === "success" ? "good" : (h.run_status === "failed" ? "bad" : "");
  var cards = [
    { v: formatNumber(h.n_subjects), l: "Subjects", cls: "" },
    { v: formatNumber(h.n_channels), l: "Channels", cls: "" },
    { v: formatNumber(h.n_channels_included), l: "Channels included", cls: "" },
    { v: formatNumber(h.n_cycles), l: "Theta cycles", cls: "" },
    { v: h.run_status, l: "Run status", cls: statusClass },
    { v: dqValue, l: h.dq_total + " DQ checks", cls: dqClass }
  ];
  for (var i = 0; i < cards.length; i++) {
    var card = cards[i];
    var div = document.createElement("div");
    div.className = "kpi " + card.cls;
    var v = document.createElement("div");
    v.className = "v";
    v.textContent = card.v;
    var l = document.createElement("div");
    l.className = "l";
    l.textContent = card.l;
    div.appendChild(v);
    div.appendChild(l);
    container.appendChild(div);
  }
}

function renderScatter(elementId, points, axisMin, axisMax) {
  var W = 560, H = 420, pad = 52;
  var svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H });
  var plotW = W - pad * 2, plotH = H - pad * 2;

  function sx(value) { return pad + (value - axisMin) / (axisMax - axisMin) * plotW; }
  function sy(value) { return H - pad - (value - axisMin) / (axisMax - axisMin) * plotH; }

  var ticks = 5;
  for (var t = 0; t <= ticks; t++) {
    var value = axisMin + (axisMax - axisMin) * t / ticks;
    var gx = sx(value), gy = sy(value);
    svg.appendChild(svgEl("line", { class: "grid-line", x1: sx(axisMin), y1: gy, x2: sx(axisMax), y2: gy }));
    svg.appendChild(svgEl("line", { class: "grid-line", x1: gx, y1: sy(axisMin), x2: gx, y2: sy(axisMax) }));
    var xLabel = svgEl("text", { class: "tick", x: gx, y: H - pad + 16, "text-anchor": "middle" });
    xLabel.textContent = value.toFixed(2);
    svg.appendChild(xLabel);
    var yLabel = svgEl("text", { class: "tick", x: pad - 8, y: gy + 3, "text-anchor": "end" });
    yLabel.textContent = value.toFixed(2);
    svg.appendChild(yLabel);
  }

  // y = x reference diagonal.
  svg.appendChild(svgEl("line", { class: "ref-line", x1: sx(axisMin), y1: sy(axisMin), x2: sx(axisMax), y2: sy(axisMax) }));

  var palette = ["#4e79a7", "#f28e2b", "#e15759", "#b48cff", "#4fd0d0", "#e78bc4", "#9fd356", "#59a14f"];
  var assigned = {};
  for (var i = 0; i < points.length; i++) {
    var p = points[i];
    var color = subjectColor(p.subject_id, palette, assigned);
    var dot = svgEl("circle", { cx: sx(p.baseline), cy: sy(p.comparison), r: 3.4, fill: color, "fill-opacity": 0.72, stroke: color, "stroke-opacity": 0.9 });
    var title = svgEl("title", {});
    title.textContent = p.subject_id + " / " + p.channel_label + "  Load 1=" + p.baseline.toFixed(3) + "  Load 3=" + p.comparison.toFixed(3);
    dot.appendChild(title);
    svg.appendChild(dot);
  }

  var xAxisLabel = svgEl("text", { class: "axis-label", x: W / 2, y: H - 8, "text-anchor": "middle" });
  xAxisLabel.textContent = "Load 1";
  svg.appendChild(xAxisLabel);
  var yAxisLabel = svgEl("text", { class: "axis-label", x: 14, y: H / 2, "text-anchor": "middle", transform: "rotate(-90 14 " + (H / 2) + ")" });
  yAxisLabel.textContent = "Load 3";
  svg.appendChild(yAxisLabel);

  if (points.length === 0) { appendEmpty(svg, W, H); }
  document.getElementById(elementId).appendChild(svg);
}

function histogram(values, binWidth) {
  if (values.length === 0) { return { bins: [], min: -0.05, max: 0.05, maxCount: 1, mean: 0 }; }
  var lo = Math.min.apply(null, values);
  var hi = Math.max.apply(null, values);
  var span = Math.max(Math.abs(lo), Math.abs(hi), binWidth * 2);
  var min = -span, max = span;
  var nBins = Math.max(6, Math.round((max - min) / binWidth));
  var counts = new Array(nBins).fill(0);
  var sum = 0;
  for (var i = 0; i < values.length; i++) {
    sum += values[i];
    var idx = Math.floor((values[i] - min) / (max - min) * nBins);
    if (idx < 0) { idx = 0; }
    if (idx >= nBins) { idx = nBins - 1; }
    counts[idx] += 1;
  }
  var bins = [];
  var maxCount = 1;
  for (var b = 0; b < nBins; b++) {
    var start = min + (max - min) * b / nBins;
    bins.push({ start: start, end: start + (max - min) / nBins, count: counts[b] });
    if (counts[b] > maxCount) { maxCount = counts[b]; }
  }
  return { bins: bins, min: min, max: max, maxCount: maxCount, mean: sum / values.length };
}

function renderDiffHistogram() {
  var W = 560, H = 380, pad = 48;
  var ptsym = DATA.differences.ptsym || [];
  var rdsym = DATA.differences.rdsym || [];
  var legend = document.createElement("div");
  legend.className = "legend";
  legend.innerHTML =
    '<span><i class="swatch" style="background:#5aa9ff"></i>ptsym diff</span>' +
    '<span><i class="swatch" style="background:#46c98b"></i>rdsym diff</span>';
  document.getElementById("hist-diff").appendChild(legend);

  var svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H });
  var plotW = W - pad * 2, plotH = H - pad * 2;
  var hp = histogram(ptsym, 0.004);
  var hr = histogram(rdsym, 0.004);
  var min = Math.min(hp.min, hr.min), max = Math.max(hp.max, hr.max);
  var maxCount = Math.max(hp.maxCount, hr.maxCount);

  function sx(value) { return pad + (value - min) / (max - min) * plotW; }
  function sy(count) { return H - pad - count / maxCount * plotH; }

  for (var t = 0; t <= 4; t++) {
    var cy = H - pad - plotH * t / 4;
    svg.appendChild(svgEl("line", { class: "grid-line", x1: pad, y1: cy, x2: W - pad, y2: cy }));
    var lab = svgEl("text", { class: "tick", x: pad - 6, y: cy + 3, "text-anchor": "end" });
    lab.textContent = String(Math.round(maxCount * t / 4));
    svg.appendChild(lab);
  }

  drawHistBars(svg, hp, sx, sy, H, pad, "#5aa9ff");
  drawHistBars(svg, hr, sx, sy, H, pad, "#46c98b");

  // Reference at zero (null) and at each mean.
  svg.appendChild(svgEl("line", { class: "ref-line", x1: sx(0), y1: pad, x2: sx(0), y2: H - pad, stroke: "#93a0b8", "stroke-dasharray": "0" }));
  drawMeanLine(svg, sx(hp.mean), pad, H - pad, "#5aa9ff");
  drawMeanLine(svg, sx(hr.mean), pad, H - pad, "#46c98b");

  for (var g = 0; g <= 4; g++) {
    var value = min + (max - min) * g / 4;
    var gx = sx(value);
    var xlab = svgEl("text", { class: "tick", x: gx, y: H - pad + 15, "text-anchor": "middle" });
    xlab.textContent = value.toFixed(3);
    svg.appendChild(xlab);
  }
  document.getElementById("hist-diff").appendChild(svg);
}

function drawHistBars(svg, hist, sx, sy, H, pad, color) {
  for (var i = 0; i < hist.bins.length; i++) {
    var bin = hist.bins[i];
    if (bin.count === 0) { continue; }
    var x1 = sx(bin.start), x2 = sx(bin.end);
    var rect = svgEl("rect", {
      x: x1 + 1, y: sy(bin.count), width: Math.max(1, x2 - x1 - 2), height: (H - pad) - sy(bin.count),
      fill: color, "fill-opacity": 0.5
    });
    svg.appendChild(rect);
  }
}

function drawMeanLine(svg, x, top, bottom, color) {
  svg.appendChild(svgEl("line", { x1: x, y1: top, x2: x, y2: bottom, stroke: color, "stroke-width": 1.6, "stroke-dasharray": "4 4" }));
}

function renderQcDistribution() {
  var W = 560, H = 380, pad = 48;
  var container = document.getElementById("qc-dist");
  var legend = document.createElement("div");
  legend.className = "legend";
  legend.innerHTML =
    '<span><i class="swatch" style="background:#b3941c"></i>Load 1</span>' +
    '<span><i class="swatch" style="background:#b07aa1"></i>Load 3</span>' +
    '<span style="margin-left:auto">solid = ptsym, dashed = rdsym</span>';
  container.appendChild(legend);

  var series = [];
  collectQc(series, DATA.qc.time_ptsym, false);
  collectQc(series, DATA.qc.time_rdsym, true);

  var maxCount = 1;
  for (var s = 0; s < series.length; s++) {
    for (var p = 0; p < series[s].points.length; p++) {
      if (series[s].points[p].count > maxCount) { maxCount = series[s].points[p].count; }
    }
  }

  var svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H });
  var plotW = W - pad * 2, plotH = H - pad * 2;
  var min = 0, max = 1;
  function sx(value) { return pad + (value - min) / (max - min) * plotW; }
  function sy(count) { return H - pad - count / maxCount * plotH; }

  for (var t = 0; t <= 5; t++) {
    var gx = pad + plotW * t / 5;
    svg.appendChild(svgEl("line", { class: "grid-line", x1: gx, y1: pad, x2: gx, y2: H - pad }));
    var xlab = svgEl("text", { class: "tick", x: gx, y: H - pad + 15, "text-anchor": "middle" });
    xlab.textContent = (t / 5).toFixed(1);
    svg.appendChild(xlab);
  }

  for (var i = 0; i < series.length; i++) {
    drawQcLine(svg, series[i], sx, sy);
  }

  // Reference at 0.5 (perfectly symmetric).
  svg.appendChild(svgEl("line", { class: "ref-line", x1: sx(0.5), y1: pad, x2: sx(0.5), y2: H - pad }));
  var refLabel = svgEl("text", { class: "axis-label", x: sx(0.5), y: pad - 4, "text-anchor": "middle" });
  refLabel.textContent = "0.5";
  svg.appendChild(refLabel);

  var xAxisLabel = svgEl("text", { class: "axis-label", x: W / 2, y: H - 6, "text-anchor": "middle" });
  xAxisLabel.textContent = "symmetry value";
  svg.appendChild(xAxisLabel);

  if (series.length === 0) { appendEmpty(svg, W, H); }
  container.appendChild(svg);
}

function collectQc(series, byLoad, dashed) {
  if (!byLoad) { return; }
  var loads = Object.keys(byLoad).sort();
  for (var i = 0; i < loads.length; i++) {
    var load = loads[i];
    var color = load === "1" ? "#b3941c" : "#b07aa1";
    series.push({ load: load, dashed: dashed, color: color, points: byLoad[load] });
  }
}

function drawQcLine(svg, serie, sx, sy) {
  var points = serie.points;
  if (!points || points.length === 0) { return; }
  var d = "";
  for (var i = 0; i < points.length; i++) {
    var command = i === 0 ? "M" : "L";
    d += command + sx(points[i].bin_start).toFixed(1) + " " + sy(points[i].count).toFixed(1) + " ";
  }
  var attrs = { d: d, fill: "none", stroke: serie.color, "stroke-width": 1.8 };
  if (serie.dashed) { attrs["stroke-dasharray"] = "5 4"; }
  svg.appendChild(svgEl("path", attrs));
}

function renderCoverage() {
  var coverage = DATA.coverage || [];
  var container = document.getElementById("coverage");
  if (coverage.length === 0) { container.textContent = "No coverage data."; return; }

  var rowHeight = 22;
  var W = 560, pad = 130;
  var H = Math.max(120, coverage.length * rowHeight + 40);
  var maxChannels = 1;
  for (var i = 0; i < coverage.length; i++) {
    if (coverage[i].n_channels > maxChannels) { maxChannels = coverage[i].n_channels; }
  }
  var plotW = W - pad - 20;
  var svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H });

  for (var r = 0; r < coverage.length; r++) {
    var item = coverage[r];
    var y = 20 + r * rowHeight;
    var label = svgEl("text", { class: "tick", x: pad - 8, y: y + rowHeight / 2 + 3, "text-anchor": "end" });
    label.textContent = item.subject_id;
    svg.appendChild(label);

    var totalW = item.n_channels / maxChannels * plotW;
    var inclW = item.n_channels_included / maxChannels * plotW;
    svg.appendChild(svgEl("rect", { x: pad, y: y + 3, width: Math.max(1, totalW), height: rowHeight - 8, fill: "#2a3550", rx: 3 }));
    svg.appendChild(svgEl("rect", { x: pad, y: y + 3, width: Math.max(1, inclW), height: rowHeight - 8, fill: "#46c98b", rx: 3 }));
    var count = svgEl("text", { class: "tick", x: pad + Math.max(totalW, 1) + 6, y: y + rowHeight / 2 + 3 });
    count.textContent = item.n_channels_included + " / " + item.n_channels;
    svg.appendChild(count);
  }

  var legend = document.createElement("div");
  legend.className = "legend";
  legend.innerHTML =
    '<span><i class="swatch" style="background:#46c98b"></i>included</span>' +
    '<span><i class="swatch" style="background:#2a3550"></i>total</span>';
  container.appendChild(legend);
  container.appendChild(svg);
}

function renderTests() {
  var tests = DATA.tests || [];
  var container = document.getElementById("tests");
  if (tests.length === 0) { container.textContent = "No test results."; return; }
  var columns = [
    { key: "metric", label: "Metric", num: false },
    { key: "test", label: "Test", num: false },
    { key: "n_units", label: "n", num: true },
    { key: "observed", label: "Statistic", num: true, digits: 4 },
    { key: "p_value", label: "p", num: true, digits: 4 },
    { key: "p_value_adjusted", label: "p (Holm)", num: true, digits: 4 },
    { key: "effect_size_dz", label: "dz", num: true, digits: 3 },
    { key: "ci_lower", label: "CI low", num: true, digits: 4 },
    { key: "ci_upper", label: "CI high", num: true, digits: 4 }
  ];
  container.appendChild(buildTable(columns, tests));
}

function renderDq() {
  var dq = DATA.dq || [];
  var container = document.getElementById("dq");
  if (dq.length === 0) { container.textContent = "No data-quality results."; return; }
  var table = document.createElement("table");
  var thead = document.createElement("thead");
  thead.innerHTML = "<tr><th>Check</th><th>Severity</th><th>Status</th><th class='num'>Observed</th><th class='num'>Threshold</th></tr>";
  table.appendChild(thead);
  var tbody = document.createElement("tbody");
  for (var i = 0; i < dq.length; i++) {
    var row = dq[i];
    var passed = String(row.passed).toLowerCase() === "true";
    var tr = document.createElement("tr");
    var statusClass = passed ? "pass" : (row.severity === "error" ? "error" : "warn");
    var statusText = passed ? "pass" : row.severity;
    tr.innerHTML =
      "<td>" + escapeHtml(row.check_name) + "</td>" +
      "<td>" + escapeHtml(row.severity) + "</td>" +
      "<td><span class='pill " + statusClass + "'>" + statusText + "</span></td>" +
      "<td class='num'>" + formatFixed(row.observed, 4) + "</td>" +
      "<td class='num'>" + formatFixed(row.threshold, 4) + "</td>";
    var title = row.detail || "";
    tr.setAttribute("title", title);
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  container.appendChild(table);
}

function renderRunHealth() {
  var runs = DATA.run_health || [];
  var container = document.getElementById("run-health");
  if (runs.length === 0) { container.textContent = "No run history."; return; }
  var columns = [
    { key: "run_id", label: "Run", num: false },
    { key: "triggered_by", label: "By", num: false },
    { key: "status", label: "Status", num: false },
    { key: "duration_s", label: "Seconds", num: true },
    { key: "files_loaded", label: "Files", num: true },
    { key: "rows_loaded", label: "θ Cycles", num: true },
    { key: "checks_run", label: "Checks", num: true },
    { key: "dq_errors", label: "Errors", num: true },
    { key: "dq_warnings", label: "Warnings", num: true }
  ];
  container.appendChild(buildTable(columns, runs));
}

function buildTable(columns, rows) {
  var table = document.createElement("table");
  var thead = document.createElement("thead");
  var headRow = document.createElement("tr");
  for (var c = 0; c < columns.length; c++) {
    var th = document.createElement("th");
    if (columns[c].num) { th.className = "num"; }
    th.textContent = columns[c].label;
    headRow.appendChild(th);
  }
  thead.appendChild(headRow);
  table.appendChild(thead);

  var tbody = document.createElement("tbody");
  for (var r = 0; r < rows.length; r++) {
    var tr = document.createElement("tr");
    for (var k = 0; k < columns.length; k++) {
      var col = columns[k];
      var td = document.createElement("td");
      if (col.num) { td.className = "num"; }
      var value = rows[r][col.key];
      if (col.digits !== undefined) {
        td.textContent = formatFixed(value, col.digits);
      } else if (col.num) {
        td.textContent = formatNumber(value);
      } else {
        td.textContent = value === undefined || value === null ? "-" : value;
      }
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  return table;
}

function appendEmpty(svg, W, H) {
  var text = svgEl("text", { class: "axis-label", x: W / 2, y: H / 2, "text-anchor": "middle" });
  text.textContent = "no data";
  svg.appendChild(text);
}

function escapeHtml(value) {
  if (value === undefined || value === null) { return ""; }
  return String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

renderSubtitle();
renderKpis();
renderScatter("scatter-ptsym", DATA.paired.ptsym, 0.4, 0.6);
renderScatter("scatter-rdsym", DATA.paired.rdsym, 0.4, 0.6);
renderDiffHistogram();
renderQcDistribution();
renderTests();
renderCoverage();
renderDq();
renderRunHealth();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
