# Stale-data cleanup interview

A self-contained, synthetic exercise for a **20-minute senior-developer interview**. The candidate explains why the current stale-data metric hides cleanup effort, proposes different KPIs and acquisition strategies at production scale, and implements and demonstrates one chosen KPI.

## What to share

Give the candidate **only the `candidate/` folder**. It contains the task, runnable Python starter, and synthetic data. AI assistance is allowed.

Share a clean starter without generated graph images or Python cache files.

Do **not** share `interviewer/`, `tests/`, or this README: they contain reference approaches, fixture generation, and evaluation evidence.

## Run the starter

From `candidate/`:

```bash
python3 -m pip install -r requirements.txt
python3 -m storage_metrics.report
python3 -m storage_metrics.plotting --output-dir graphs
```

The existing stale-data report works. The proposed KPI is initially shown as not implemented. The intended edit point is `storage_metrics/solution.py`, returning a named `KPIResult` with a value and unit. The candidate can choose another implementation approach if they explain it. A generic Matplotlib helper supports plotting both the existing history and a proposed metric.

The plotting command saves `graphs/stale.png` and, when the candidate hook returns results, one `graphs/kpi-N.png` per distinct metric name/unit. It prints the files produced in that run and needs no interactive display. The unchanged starter produces only the stale-data graph; no reference answer is injected.

The production constraint is explicit: thousands of disks with millions of files each. The fixture retains a year of weekly aggregated history but only the latest weekly detailed file scan per disk. Aggregate totals and wide age-bin columns are combined in `candidate/data/scans.csv`.

Stored size values use GiB. File details contain `days_since_last_access` rather than an access date. The wide age columns use bucket edges `0, 30, 60, …, 330, 365`, so each bounded bin is `[lower, upper)` and there is one `365+` bin. Stale means age 365 or more, which is exactly that bin.

## Prepare as the interviewer

Read `interviewer/GUIDE.md`. From this project directory:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 python3 -m interviewer.generate_dataset --check
PYTHONDONTWRITEBYTECODE=1 python3 -m interviewer.solutions.cohort --verify
PYTHONDONTWRITEBYTECODE=1 python3 -m interviewer.solutions.inventory_diff --demo
```

Python 3.9 or newer is required. Matplotlib is the only added runtime dependency and is used for plotting. Data fetching and the text report remain local; there are no credentials, network calls, or production-data access. Installing a missing dependency may require package-index access.

## Relationship to the storage-monitoring repo

This exercise models the age-distribution snapshots produced by `disks/per_disk_acquisition/acquisitions/fsmon_details.py`. That acquisition aggregates file sizes into age buckets. The interview deliberately uses a small independent data-access layer rather than importing production acquisition code or requiring FSMON, Jenkins, or a database.

The exercise supplies stable synthetic file identities in the latest detailed scan. This is **not** a claim that historical identities are retained: the starter explicitly refuses older file-detail requests. File-by-file comparisons across scans require a proposal for new acquisition or retention, not an assumption that history is already available.

The supplied year of aggregate history and latest-only weekly details follow the availability described for this interview. This does not change any production table or retention configuration.

This is an interview fixture, not a production cleanup tool. It never deletes or scans real files.
