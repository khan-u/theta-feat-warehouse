# Tableau dashboard

The pipeline writes flat CSV extracts to `warehouse/exports/`. Connect Tableau to
that folder and build the four worksheets below.

For a zero-install version of the same four panels, run
`python dashboard/build_dashboard.py` (or `make dashboard`) to generate
`dashboard/index.html`, a self-contained page that reads the same extracts and
opens in any browser. This document describes the Tableau build; the two report
the same numbers.

## Why CSV rather than a live connection

Tableau Public cannot connect to a local database at all, and the DuckDB
JDBC/ODBC driver is not present in every Tableau install. The marts are already
aggregated - `channel_paired_asym` is one row per channel, not per cycle - so the
extracts are small and a live connection adds no benefit. Each extract also
carries `exported_for_run_id`, so a refresh can be traced to the pipeline run
that produced it.

## Extracts

| File | Grain | Used by |
| --- | --- | --- |
| `channel_paired_asym.csv` | one row per channel, both conditions | Sheets 1, 2 |
| `channel_load_asym.csv` | one row per channel x load | Sheet 2 |
| `cycle_qc_distribution.csv` | pre-binned histogram counts | Sheet 3 |
| `subject_coverage.csv` | one row per subject x region | Sheet 4 |
| `run_health.csv` | one row per pipeline run | Sheet 4 |
| `test_results.csv` | one row per metric x test | Sheet 1 |
| `dq_results.csv` | one row per check per run | Sheet 4 |

No joins are needed for sheets 1-3; each reads a single extract. Sheet 4 needs
`run_health` and `dq_results` related on `run_id` (use a *relationship*, not a
join - the grains differ and a join would fan out the run rows).

## Sheet 1 - Paired Asymmetry (Peak-to-Trough)

The main result. One mark per channel, both load conditions on the axes. The
columns show the two load conditions: `*_baseline` is load 1,
`*_comparison` is load 3.

- Source: `channel_paired_asym.csv`, filtered to `included = True`
- Columns: `ptsym_baseline` (fixed axis 0.4-0.6); rename the axis title to `Load 1`
- Rows: `ptsym_comparison` (same fixed axis); rename the axis title to `Load 3`
- Marks: circle, `Detail` = `channel_label`, `Color` = `subject_id`
- Reference line: a 45 deg `y = x` line via a calculated field `[ptsym_baseline]`
  on a dual axis, formatted as a line, no marks

Points on the diagonal mean no load difference; systematic displacement to
one side is the effect. Fix both axes to the same range, because auto-scaling can
make small differences appear large.

Duplicate the sheet for `rdsym_baseline` / `rdsym_comparison` as Rise-to-Decay.

Caption: pull `p_value_adjusted`, `effect_size_dz`, `ci_lower` and `ci_upper`
from `test_results.csv` so the plot and the statistic can't drift apart.

## Sheet 2 - Difference distribution

- Source: `channel_paired_asym.csv`, `included = True`
- Columns: `ptsym_diff`, binned at 0.005
- Rows: `COUNT(channel_label)`
- Reference line at 0 (the null), plus a second at `AVG([ptsym_diff])`

Consider the spread as well as the centre: a mean near zero with a wide
distribution differs from a mean near zero with a tight one.

## Sheet 3 - Cycle-Level Symmetry Distribution

- Source: `cycle_qc_distribution.csv`
- Columns: `bin_start` (continuous)
- Rows: `SUM(n_cycles)`
- Color: `load_condition`
- Rows shelf (second): `metric` to get a small-multiple per measure
- Filter: `is_burst` - expose it as a control rather than hard-coding, since the
  analysis restricts to burst cycles by default and the reader should be able to
  see what that choice does

A reference line at 0.5 marks perfect symmetry. Both distributions should sit on
it; a shift means asymmetric waveforms.

## Sheet 4 - Run Health

Two panes side by side.

Left, coverage:
- Source: `subject_coverage.csv`
- Rows: `subject_id`; Columns: `n_channels_included` and `n_channels`
- Bar chart, so channels lost to the inclusion rule are visible per subject

Right, run history:
- Source: `run_health.csv` related to `dq_results.csv` on `run_id`
- Columns: `started_at` (exact date, so runs on the same day stay separate); rename the axis title to `Run`
- Rows: `rows_loaded`, renamed to `θ Cycles Loaded`
- Color: `status` as a discrete field - assign `success` green, `failed` red
  (Edit Colors), plus a shape or label on `dq_errors > 0`

A failed run is expected in the history: when a subject's cycle features have not
landed yet but its trial log has, the data-quality gate blocks the run
(`trial_coverage_gaps`) before anything reaches the marts. Caption it as such,
e.g. "run blocked by the DQ gate - trial logs present without cycle features",
so the red point reads as the gate working, not a broken pipeline.

Put this on the same dashboard as the result, not on a separate tab, so data
quality is shown next to the number it qualifies.

## Refresh

```bash
make pipeline      # rewrites every CSV in warehouse/exports/
```

Then refresh the data source in Tableau. `manifest.json` in the same directory
records the run id, the analysis parameters and the signal parameters used, so a
dashboard screenshot can always be tied back to the configuration behind it.
