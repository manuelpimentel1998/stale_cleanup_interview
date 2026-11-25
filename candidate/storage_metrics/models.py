"""Immutable scan, age-bin, file-detail, and candidate KPI records.

Storage quantities use GiB (2**30 bytes). File ages are integer days measured
at the scan date; age 365 is active and age 366 is stale. Record construction
does not perform validation; CSV values are validated by the repository.
"""

from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass(frozen=True)
class Snapshot:
    """Aggregate storage measurements for one disk on one scan date.

    Attributes:
        disk: Readable absolute path identifying the disk.
        owner: Team responsible for the disk.
        scanned_at: Date of the scan, also part of its identity.
        complete: Whether the scan covered the entire disk.
        total_gib: Total storage in GiB, including active and stale files.
        stale_gib: Exact storage in GiB with file age strictly greater than 365.
    """
    disk: str
    owner: str
    scanned_at: date
    complete: bool
    total_gib: float
    stale_gib: float


@dataclass(frozen=True)
class AgeBucket:
    """Storage in an inclusive integer-day age interval at a scan.

    Attributes:
        min_age_days: Inclusive lower age bound.
        max_age_days: Inclusive upper bound, or None for an unbounded interval.
        size_gib: Combined file size in GiB within the interval.

    The supplied intervals represent approximate 30-day months through day
    540, followed by overflow. They do not redefine the stale-age threshold.
    """
    min_age_days: int
    max_age_days: Optional[int]
    size_gib: float


@dataclass(frozen=True)
class FileRecord:
    """One file in the retained detailed inventory for a disk and scan date.

    Attributes:
        disk: Readable absolute disk path.
        scanned_at: Date at which the file's size and age were measured.
        file_id: Stable file identity, independent of its path.
        path: Full synthetic absolute file path.
        size_gib: File size in GiB.
        days_since_last_access: Nonnegative integer age at this scan, not an
            access date. An age strictly greater than 365 is stale.
    """
    disk: str
    scanned_at: date
    file_id: str
    path: str
    size_gib: float
    days_since_last_access: int


@dataclass(frozen=True)
class KPIResult:
    """A candidate-defined metric ready for display without unit conversion.

    Attributes:
        name: Human-readable name chosen by the candidate.
        value: Numeric result already expressed in the desired display units.
        unit: Display unit label; it need not be a storage unit.
    """
    name: str
    value: float
    unit: str
