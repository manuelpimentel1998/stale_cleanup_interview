"""Render stale-GiB history and the candidate's optional, unit-flexible KPI.

The report fetches aggregate scans and calls the candidate extension for
consecutive scans of the same disk. Initial scans have no comparison result;
an unimplemented extension is displayed explicitly rather than as zero.
"""

import argparse
from typing import Callable, Optional

from .models import KPIResult, Snapshot
from .repository import SnapshotRepository
from .solution import calculate_kpi

def render_report(
    repository: SnapshotRepository,
    disk: Optional[str] = None,
    calculator: Callable[[SnapshotRepository, Snapshot, Snapshot], Optional[KPIResult]] = calculate_kpi,
) -> str:
    """Build a text report without printing or modifying repository data.

    Args:
        repository: Source of aggregate scan history.
        disk: Exact disk path to select, or None for all disks.
        calculator: Callable accepting the repository and a previous/current
            same-disk scan pair, returning KPIResult or None. By default this
            is the candidate extension.

    Returns:
        Human-readable history with scan dates and stale GiB. Candidate KPI
        names, values, and units are displayed without storage-unit conversion.
        The result includes an empty-history message when no scans match.

    Raises:
        Exception: Repository or calculator errors propagate to the caller.
    """
    lines = ["Stale: last accessed at least 365 days before the scan.",
             "Candidate KPI: interpretation and units defined by the candidate."]
    snapshots = repository.list_snapshots(disk)
    if not snapshots:
        return "\n".join(lines + ["No snapshots found."])
    previous = None
    for current in snapshots:
        if previous is None or previous.disk != current.disk:
            lines.extend(["", "Disk {} (owner {})".format(current.disk, current.owner),
                          "Scan date     Stale GiB    Candidate KPI (name: value unit)"])
            kpi = "N/A (initial scan)"
        else:
            value = calculator(repository, previous, current)
            kpi = "not implemented" if value is None else "{}: {:.2f} {}".format(value.name, value.value, value.unit)
        lines.append("{}    {:>9.2f}    {}".format(current.scanned_at.isoformat(), current.stale_gib, kpi))
        previous = current
    return "\n".join(lines)


def main() -> None:
    """Parse optional --disk and --data-dir arguments and print the report.

    Raises:
        SystemExit: Argument parsing requests help or encounters invalid options.
        ValueError: Repository data is invalid.
        OSError: A required CSV file cannot be read.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disk")
    parser.add_argument("--data-dir")
    args = parser.parse_args()
    print(render_report(SnapshotRepository(args.data_dir), args.disk))


if __name__ == "__main__":
    main()
