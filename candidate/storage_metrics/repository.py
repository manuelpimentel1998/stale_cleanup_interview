"""Fetch local aggregate history and latest-only weekly file details from CSV.

Scans and inclusive monthly age bins are keyed by readable disk path and date.
Storage quantities are finite nonnegative GiB. The stale threshold is strictly
greater than 365 days, including within an age bin that crosses that threshold.
Only each disk's latest detailed inventory is retained; older aggregates do
not make historical file records available. No live disk scanning is performed.
"""

import csv
import math
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple, Union

from .models import AgeBucket, FileRecord, Snapshot

AGE_COLUMNS = [
    ("age_{}_{}_gib".format(lower, upper if upper is not None else "plus"), lower, upper)
    for lower, upper in [(0, 30)] + [(end - 29, end) for end in range(60, 541, 30)] + [(541, None)]
]


class FileHistoryUnavailable(ValueError):
    """Requested file history is unretained, missing, or not the latest scan.

    This is distinct from an empty inventory and is never filled in from
    aggregate history.
    """


def _rows(path: Path, columns: List[str]) -> Iterator[Dict[str, str]]:
    """Stream UTF-8 CSV rows, rejecting unexpected columns or malformed rows."""
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != columns:
            raise ValueError("{}: expected columns {}".format(path, columns))
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("{}: malformed CSV row {}".format(path, reader.line_num))
            yield row


def _nonnegative_int(value: str) -> int:
    """Parse an integer-day age and reject negative or non-integer values."""
    number = int(value)
    if number < 0:
        raise ValueError("Expected a nonnegative integer, got {!r}".format(value))
    return number


def _gib(value: str) -> float:
    """Parse finite nonnegative GiB, preserving valid fractional quantities."""
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("Expected finite nonnegative GiB, got {!r}".format(value))
    return number


def _date(value: str) -> date:
    """Parse a calendar date and require canonical YYYY-MM-DD spelling."""
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError("Expected ISO date YYYY-MM-DD, got {!r}".format(value))
    return result


class SnapshotRepository:
    """Access aggregate scan exports and lazily stream retained file records.

    Aggregate rows and age bins are loaded at construction. Detailed file
    records are read on demand, without caching complete file inventories.
    Historical aggregate scans remain accessible even though only the latest
    weekly file details for each disk are available.
    """
    def __init__(self, data_dir: Optional[Union[str, Path]] = None):
        """Load and validate the aggregate export.

        Args:
            data_dir: Directory containing scans.csv and files.csv. None uses
                the candidate data directory located relative to this module.

        Raises:
            ValueError: The aggregate schema, disk/date keys, dates, quantities,
                total GiB, or monthly-bin stale bounds are invalid. Quantity
                comparisons allow small floating-point differences.
            OSError: The aggregate CSV cannot be read.
        """
        self.data_dir = Path(data_dir) if data_dir is not None else Path(__file__).resolve().parents[1] / "data"
        self._snapshots: Dict[Tuple[str, date], Snapshot] = {}
        self._buckets: Dict[Tuple[str, date], List[AgeBucket]] = {}
        self._latest: Dict[str, Snapshot] = {}
        with closing(_rows(self.data_dir / "scans.csv", [
            "disk", "owner", "scanned_at", "complete", "total_gib", "stale_gib"
        ] + [column for column, _, _ in AGE_COLUMNS])) as rows:
            for row in rows:
                if not row["disk"].startswith("/") or not row["owner"]:
                    raise ValueError("Readable absolute disk path and owner are required")
                if row["complete"] not in ("true", "false"):
                    raise ValueError("complete must be true or false")
                snapshot = Snapshot(
                    row["disk"], row["owner"], _date(row["scanned_at"]),
                    row["complete"] == "true", _gib(row["total_gib"]), _gib(row["stale_gib"])
                )
                key = (snapshot.disk, snapshot.scanned_at)
                if key in self._snapshots:
                    raise ValueError("Duplicate disk/date scan: {} {}".format(*key))
                buckets = [AgeBucket(lower, upper, _gib(row[column]))
                           for column, lower, upper in AGE_COLUMNS]
                if not math.isclose(math.fsum(bucket.size_gib for bucket in buckets), snapshot.total_gib,
                                    rel_tol=1e-9, abs_tol=1e-9):
                    raise ValueError("Age bucket GiB do not sum to total GiB: {} {}".format(*key))
                wholly_stale = math.fsum(bucket.size_gib for bucket in buckets if bucket.min_age_days > 365)
                crossing = math.fsum(bucket.size_gib for bucket in buckets if bucket.min_age_days <= 365
                                     and (bucket.max_age_days is None or bucket.max_age_days > 365))
                upper = wholly_stale + crossing
                if ((snapshot.stale_gib < wholly_stale and not math.isclose(snapshot.stale_gib, wholly_stale, rel_tol=1e-9, abs_tol=1e-9))
                        or (snapshot.stale_gib > upper and not math.isclose(snapshot.stale_gib, upper, rel_tol=1e-9, abs_tol=1e-9))):
                    raise ValueError("Stale GiB outside monthly-bin bounds: {} {}".format(*key))
                self._snapshots[key] = snapshot
                self._buckets[key] = buckets
                if snapshot.disk not in self._latest or snapshot.scanned_at > self._latest[snapshot.disk].scanned_at:
                    self._latest[snapshot.disk] = snapshot

    def _snapshot(self, disk: str, scanned_at: date) -> Snapshot:
        """Look up an exact disk/date scan, raising ValueError if unknown."""
        if (disk, scanned_at) not in self._snapshots:
            raise ValueError("Unknown disk/date scan: {} {}".format(disk, scanned_at))
        return self._snapshots[(disk, scanned_at)]

    def list_snapshots(self, disk: Optional[str] = None) -> List[Snapshot]:
        """Return aggregate scans ordered by disk path and then scan date.

        Args:
            disk: Exact disk path to select, or None to include every disk.

        Returns:
            A new list of matching Snapshot records, empty if none match.
        """
        return sorted(
            (item for item in self._snapshots.values() if disk is None or item.disk == disk),
            key=lambda item: (item.disk, item.scanned_at)
        )

    def get_age_buckets(self, disk: str, scanned_at: date) -> List[AgeBucket]:
        """Return the supplied inclusive monthly age bins for an exact scan.

        Args:
            disk: Readable disk path.
            scanned_at: Date identifying an aggregate scan, including older scans.

        Returns:
            A new list of AgeBucket records in increasing age order. Sizes
            are in GiB; the final upper bound is None for overflow.

        Raises:
            ValueError: No aggregate scan matches the disk and date.
        """
        self._snapshot(disk, scanned_at)
        return list(self._buckets[(disk, scanned_at)])

    def iter_files(self, disk: str, scanned_at: Optional[date] = None) -> Iterator[FileRecord]:
        """Return a lazy iterator over the disk's latest weekly file details.

        Args:
            disk: Readable disk path.
            scanned_at: Latest aggregate scan date, or None to select it.
                Explicit historical dates are not available.

        Returns:
            An iterator yielding FileRecord values with GiB sizes and integer
            days_since_last_access measured at the retained scan date.

        Raises:
            FileHistoryUnavailable: A non-latest date is requested; raised
                immediately. During iteration, also raised if detail dates
                mismatch the latest aggregate or expected details are missing.
            ValueError: The disk is unknown or a streamed record is invalid.
            OSError: The detailed CSV cannot be read during iteration.

        Close the returned iterator if abandoning it before exhaustion so its
        CSV stream is released promptly.
        """
        if disk not in self._latest:
            raise ValueError("Unknown disk: " + disk)
        latest = self._latest[disk]
        if scanned_at is not None and scanned_at != latest.scanned_at:
            raise FileHistoryUnavailable(
                "Only latest weekly detail is retained for {} ({}); requested {}.".format(
                    disk, latest.scanned_at, scanned_at))
        return self._iter_latest_files(latest)

    def _iter_latest_files(self, latest: Snapshot) -> Iterator[FileRecord]:
        """Yield validated latest-scan records, closing the CSV when finished."""
        disk = latest.disk
        seen = set()
        found = False
        with closing(_rows(self.data_dir / "files.csv", [
            "disk", "scanned_at", "file_id", "path", "size_gib", "days_since_last_access"
        ])) as rows:
            for row in rows:
                detail_date = _date(row["scanned_at"])
                if row["disk"] not in self._latest:
                    raise ValueError("Unknown disk: " + row["disk"])
                snapshot = self._latest[row["disk"]]
                if detail_date != snapshot.scanned_at:
                    raise FileHistoryUnavailable(
                        "Only latest weekly detail is retained; {} detail date {} does not match latest aggregate date {}.".format(
                            snapshot.disk, detail_date, snapshot.scanned_at))
                age = _nonnegative_int(row["days_since_last_access"])
                size = _gib(row["size_gib"])
                if not row["file_id"] or not row["path"].startswith("/"):
                    raise ValueError("File identity and absolute path are required")
                if snapshot.disk == disk:
                    if row["file_id"] in seen:
                        raise ValueError("Duplicate file ID within snapshot: " + row["file_id"])
                    seen.add(row["file_id"])
                    found = True
                    yield FileRecord(disk, detail_date, row["file_id"], row["path"], size, age)
        if not found and latest.total_gib:
            raise FileHistoryUnavailable(
                "Only latest weekly detail is retained, but no detail matches {} {}.".format(disk, latest.scanned_at))
