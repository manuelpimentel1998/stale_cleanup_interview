"""Shared scan-pair validation and monthly-baseline lookup for solution tracks.

A month is assumed to be exactly 28 days. The supplied weekly scans therefore
provide four-scan-apart samples; this is an estimate, not a calendar-month rule.
"""

from typing import Optional, Tuple

from candidate.storage_metrics.models import Snapshot
from candidate.storage_metrics.repository import SnapshotRepository

MONTH_DAYS = 28


def validate_pair(previous: Snapshot, current: Snapshot) -> int:
    """Return elapsed days, rejecting mixed disks, incomplete scans, or non-increasing dates."""
    if previous.disk != current.disk:
        raise ValueError("Cannot compare different disks")
    if not previous.complete or not current.complete:
        raise ValueError("Complete scans are required")
    elapsed = (current.scanned_at - previous.scanned_at).days
    if elapsed <= 0:
        raise ValueError("Scan dates must be strictly increasing")
    return elapsed


def month_baseline(repository: SnapshotRepository, current: Snapshot) -> Optional[Snapshot]:
    """Return the same-disk scan exactly 28 days before ``current``, if retained.

    Returns:
        The baseline scan, or None when the history does not contain it. Used to
        turn a weekly KPI-hook call into a 28-day comparison.
    """
    for snapshot in repository.list_snapshots(current.disk):
        if (current.scanned_at - snapshot.scanned_at).days == MONTH_DAYS:
            return snapshot
    return None


def month_pair(repository: SnapshotRepository, previous: Snapshot,
               current: Snapshot) -> Optional[Tuple[Snapshot, Snapshot]]:
    """Return the 28-day pair a monthly KPI hook should compare, or None.

    Consecutive weekly calls use the supplied pair when it is already 28 days;
    otherwise they resolve the scan exactly 28 days before ``current``. Returns
    None when that baseline is not retained.
    """
    elapsed = validate_pair(previous, current)
    if elapsed == MONTH_DAYS:
        return previous, current
    baseline = month_baseline(repository, current)
    return None if baseline is None else (baseline, current)
