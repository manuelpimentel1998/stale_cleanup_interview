"""Two interviewer reference tracks for estimating stale-data cleanup.

Each track exposes ``calculate_kpi(repository, previous, current)``, the same
hook as ``candidate.storage_metrics.solution``, so the existing report and
plotting helpers can call it. Both are estimates, not proof of deletion.

- ``cohort``: 28-day month-over-month estimate that treats the previous 11-12
  month age bucket as the month's aging inflow. Cheap: aggregate scans only.
- ``inventory_diff``: retain the previous month's stale file IDs and diff them
  against the current inventory. Observable, but expensive at fleet scale; the
  module docstring states the retention scale.

Other approaches discussed in ``interviewer/GUIDE.md`` include cleanup-action
journals, near-threshold aggregate cohorts, sampled/Bloom-filter inventories,
filesystem change feeds, and periodic snapshot-metadata diffs.
"""
