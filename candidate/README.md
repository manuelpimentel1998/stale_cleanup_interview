# Make cleanup visible

## The situation

We monitor disk usage and recommend improvements to disk owners. One metric is **stale data**: the size of files that have not been accessed for over a year.

The team is actively cleaning up stale data. However, the stale-data total keeps growing, so our current report does not show leadership how much cleanup is happening.

The production system covers **thousands of disks, each containing millions of files**. Acquisition, processing, and historical storage costs must be part of your proposal; the small synthetic dataset does not remove those constraints.

## Your objectives

1. **Explain the current metric's limitation.** Why can the stale-data total grow even while the team is cleaning up? What can leadership conclude from the existing report, and what can they not conclude?
2. **Propose different KPIs and a path to collect them.** Describe two or more ways to make cleanup effort visible. For each, explain what it measures, the data needed, any new acquisition or retention you would introduce, your assumptions, and the cost at production scale. Choose one approach for your prototype.
3. **Implement and demonstrate your chosen KPI.** Use or extend the starter, display the result, and show a check that gives you confidence in it. Explain its meaning and limitations.

You may propose new data acquisition and make assumptions, provided you state them. If information is not available, you may prototype with clearly labeled assumed or synthetic inputs; do not present those as measured facts.

There is no single required KPI or algorithm. AI assistance is allowed; you should be able to explain and verify the result you use.

## Time box: 20 minutes total

You do not need to build a service, database, UI, or complete production system. If your preferred production approach needs additional information, explain that and implement what this dataset can support.

## Start here

Requires Python 3.9+. Install the plotting dependency if needed:

```bash
python3 -m pip install -r requirements.txt
```

```bash
python3 -m storage_metrics.report
```

Run this command from the folder containing this README. You can select one disk:

```bash
python3 -m storage_metrics.report --disk /projects/atlas
```

Relevant files:

| File                              | Purpose                                                                                    |
| --------------------------------- | ------------------------------------------------------------------------------------------ |
| `storage_metrics/report.py`     | Existing stale-data report; displays your KPI for consecutive scans of the same disk. |
| `storage_metrics/solution.py`   | Implement `calculate_kpi`; return a `KPIResult` with your metric's name, value, and unit. |
| `storage_metrics/repository.py` | Working data-fetching layer.                                                               |
| `storage_metrics/models.py`     | Data fields and types.                                                                     |
| `storage_metrics/plotting.py`   | Reusable Matplotlib helper for stale history or your chosen KPI. |

## Available data

The fixture contains two disks and **one year of weekly aggregated disk information**: 53 scan dates from 2025-11-23 to 2026-11-22. Historical scans have consistent coverage and are marked complete. The `disk` column contains readable paths such as `/projects/atlas`.

**File details are different:** we retain only the latest detailed scan for each disk, refreshed weekly. Older file inventories are not available because keeping them is expensive. This models the information available from FSMON for this exercise; do not assume you can fetch historical file details.

| Dataset                  | Contents                                                                                                     |
| ------------------------ | ------------------------------------------------------------------------------------------------------------ |
| `data/scans.csv`       | One row per disk and date: owner, coverage status, `total_gib`, `stale_gib`, and one GiB-size column per monthly age bin. Aggregate history is retained for the past year. |
| `data/files.csv`       | Only the latest weekly detailed scan: disk, scan date, file ID, path, `size_gib`, and integer `days_since_last_access`. No historical file snapshots or last-access date column. |

The repository provides:

- `list_snapshots(disk=None)`
- `get_age_buckets(disk, scanned_at)`
- `iter_files(disk, scanned_at=None)` — defaults to the latest scan; requesting older detail raises an explicit history-unavailable error.

For this exercise:

- `days_since_last_access` is the whole number of days since a file was last accessed, measured at the scan date.
- "Over a year" means **365 days or more**: age 364 is not stale; age 365 is stale.
- Age bins use the edges `0, 30, 60, …, 300, 330, 365`: each bounded bin covers `[lower, upper)`, so `age_0_30_gib` covers ages 0–29 and `age_330_365_gib` covers 330–364. The final `age_365_plus_gib` bin starts at age 365.
- The `age_365_plus_gib` bin is exactly the stale bucket. `stale_gib` is its total; there is no crossing-bin split to infer.
- All stored size values and the existing stale-data report use **GiB** (1 GiB = 1,073,741,824 bytes). Sizes are logical file sizes; fractional GiB are supported. Your KPI can use a different unit.

Ask the interviewer about other operational constraints or information you need before choosing your approach.

## Plotting helper

### Plot from the command line

From this candidate folder:

```bash
python3 -m storage_metrics.plotting --output-dir graphs
```

This saves `graphs/stale.png` for the selected disks. Once `calculate_kpi` returns results, it also saves `graphs/kpi-1.png`, `graphs/kpi-2.png`, and so on. Each distinct KPI name/unit gets its own graph; values are not converted or mixed across units.

To select one disk:

```bash
python3 -m storage_metrics.plotting --disk /projects/atlas --output-dir graphs
```

The output directory defaults to `graphs`, and `--data-dir` can select another data folder. No display or desktop window is needed; open the saved PNGs in your editor or browser environment.

The command prints the files generated in the current run. The starter reports that no candidate KPI results are available until you implement the hook. Missing results remain gaps, not zeros. Existing files in the output directory are not deleted.

### Reuse the helper in your code

The helper accepts dates and a mapping of series names to values. For example, to plot the existing stale-data history:

```python
from storage_metrics.plotting import plot_metrics
from storage_metrics.repository import SnapshotRepository

history = SnapshotRepository().list_snapshots("/projects/atlas")
plot_metrics(
    [scan.scanned_at for scan in history],
    {"Stale data": [scan.stale_gib for scan in history]},
    title="Stale-data history: /projects/atlas",
    ylabel="GiB",
    output_path="stale.png",
)
```

Use the same helper with your KPI values, a descriptive series name, and the appropriate `ylabel`. Supply one value per date; use `None` where a value is unavailable, such as the initial scan. The returned Matplotlib figure can be customized. Set `show=True` for an interactive display, or save an image with `output_path`.

Plot metrics with different meanings or units separately unless you justify comparing them on the same axis.
