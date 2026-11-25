"""Generate deterministic interviewer fixtures from transient synthetic inventories.

Daily active cohorts, aging, and scheduled removals produce 53 weekly scans
for each disk. All aggregate quantities use GiB; file details are exported
only for the latest date, with integer ages instead of access dates. Historical
inventories are never written. Private ground truth records the known synthetic
schedule, not cleanup attribution inferred from observations.

The CLI writes the three fixtures by default. With --check it compares their
expected content byte-for-byte and exits without writing or replacing files.
"""

import argparse
import csv
import io
import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Tuple

from candidate.storage_metrics.repository import AGE_COLUMNS

ROOT = Path(__file__).resolve().parents[1]
START = date(2025, 11, 23)
WEEKS = 52
BOUNDS = [(lower, upper) for _, lower, upper in AGE_COLUMNS]


@dataclass(frozen=True)
class SyntheticFile:
    """Private file state used only while deriving synthetic scan exports.

    Attributes:
        file_id: Stable opaque identity reused across transient inventories.
        path: Full synthetic path on the file's disk.
        size_gib: Exact integer GiB size, unchanged while the file is retained.
        last_accessed: Private access date used to derive integer age at each
            scan. This date is not included in the candidate file export.
    """
    file_id: str
    path: str
    size_gib: int
    last_accessed: date


def _csv(columns: List[str], rows: List[Tuple]) -> bytes:
    """Encode ordered CSV columns and rows as UTF-8 bytes with LF line endings."""
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def generate_contents() -> Dict[str, bytes]:
    """Build deterministic aggregate, latest-file, and private truth content.

    Transient inventories start with one active cohort at each age 0..365 and
    separate stale groups. Each intervening day adds an active cohort; weekly
    removals affect only groups already stale at the previous scan. Monthly
    bins and exact stale GiB are derived independently from those inventories.

    Returns:
        Mapping from project-relative fixture paths to encoded content for
        scans.csv, latest-only files.csv, and private ground_truth.json. All
        metric quantities use GiB. This function performs no filesystem writes.

    Raises:
        AssertionError: The synthetic schedule, cohort sizes, aggregate sums,
            active-age population, or stale-GiB conservation are inconsistent.
    """
    scans, files, truth, disk_summaries = [], [], [], []
    serial = 0
    for disk, owner, initial_groups, group_gib, sizes_gib, removed_groups in [
        ("/projects/atlas", "team-a", 80, 128, (8, 16, 32, 72), 4),
        ("/projects/birch", "team-b", 64, 64, (4, 8, 16, 36), 7),
    ]:
        inventory: Dict[str, SyntheticFile] = {}
        groups: List[List[str]] = []

        def add_groups(count: int, accessed: date) -> None:
            """Add count opaque file groups sharing the supplied private access date."""
            nonlocal serial
            for _ in range(count):
                group = []
                for size_gib in sizes_gib:
                    serial += 1
                    identity = "f{:08x}".format(serial)
                    inventory[identity] = SyntheticFile(
                        identity, "{}/objects/{}/{}.dat".format(disk, identity[-2:], identity),
                        size_gib, accessed
                    )
                    group.append(identity)
                groups.append(group)

        if sum(sizes_gib) != group_gib:
            raise AssertionError("Group sizes do not match daily cohort GiB")
        add_groups(initial_groups, START - timedelta(days=450))
        for age in range(366):
            add_groups(1, START - timedelta(days=age))
        previous_inventory = None
        previous_date = None
        initial_total = sum(item.size_gib for item in inventory.values())
        initial_stale = initial_groups * group_gib
        disk_intervals = []
        for index in range(WEEKS + 1):
            scanned_at = START + timedelta(days=7 * index)
            removed_files = []
            if index:
                eligible = [group for group in groups if all(
                    identity in inventory and (previous_date - inventory[identity].last_accessed).days > 365
                    for identity in group
                )]
                for group in eligible[:removed_groups]:
                    for identity in group:
                        removed_files.append(inventory.pop(identity))
                if sum(item.size_gib for item in removed_files) != removed_groups * group_gib:
                    raise AssertionError("Scheduled removal could not be completed")
                for day in range(1, 8):
                    add_groups(1, previous_date + timedelta(days=day))
            ordered = sorted(inventory.values(), key=lambda item: item.file_id)
            total = sum(item.size_gib for item in ordered)
            stale = sum(item.size_gib for item in ordered if (scanned_at - item.last_accessed).days > 365)
            bucket_gib = []
            for lower, upper in BOUNDS:
                members = [item for item in ordered if lower <= (scanned_at - item.last_accessed).days
                           and (upper is None or (scanned_at - item.last_accessed).days <= upper)]
                bucket_gib.append(sum(item.size_gib for item in members))
            if sum(bucket_gib) != total:
                raise AssertionError("Inventory and aggregate buckets disagree")
            # The active-age population must retain exactly one daily cohort at every age.
            active_by_age = {}
            for item in ordered:
                age = (scanned_at - item.last_accessed).days
                if age <= 365:
                    active_by_age[age] = active_by_age.get(age, 0) + item.size_gib
            if active_by_age != {age: group_gib for age in range(366)}:
                raise AssertionError("Daily active cohorts are not uniform")
            scans.append((disk, owner, scanned_at.isoformat(), "true", total, stale, *bucket_gib))
            if index == WEEKS:
                files.extend((disk, scanned_at.isoformat(), item.file_id, item.path,
                              item.size_gib, (scanned_at - item.last_accessed).days) for item in ordered)
            if index:
                aged_in = sum(item.size_gib for item in ordered if item.file_id in previous_inventory
                              and (previous_date - item.last_accessed).days <= 365
                              and (scanned_at - item.last_accessed).days > 365)
                interval = {
                    "disk": disk, "previous_scanned_at": previous_date.isoformat(),
                    "current_scanned_at": scanned_at.isoformat(), "elapsed_days": 7,
                    "scheduled_stale_removals_gib": sum(item.size_gib for item in removed_files),
                    "scheduled_removed_file_ids": sorted(item.file_id for item in removed_files),
                    "aged_in_gib": aged_in, "new_active_gib": 7 * group_gib,
                    "previous_stale_gib": sum(item.size_gib for item in previous_inventory.values()
                                                if (previous_date - item.last_accessed).days > 365),
                    "current_stale_gib": stale,
                }
                if interval["previous_stale_gib"] - interval["scheduled_stale_removals_gib"] + aged_in != stale:
                    raise AssertionError("Synthetic event schedule does not conserve stale GiB")
                if aged_in != 7 * group_gib:
                    raise AssertionError("Unexpected weekly aging volume")
                truth.append(interval)
                disk_intervals.append(interval)
            previous_inventory = dict(inventory)
            previous_date = scanned_at
        disk_summaries.append({
            "disk": disk, "daily_active_cohort_gib": group_gib,
            "group_file_sizes_gib": list(sizes_gib),
            "weekly_removed_groups": removed_groups,
            "initial_total_gib": initial_total, "final_total_gib": total,
            "initial_stale_gib": initial_stale, "final_stale_gib": stale,
            "scheduled_stale_removals_gib": sum(item["scheduled_stale_removals_gib"] for item in disk_intervals),
            "aged_in_gib": sum(item["aged_in_gib"] for item in disk_intervals),
        })
    ground_truth = {
        "stale_definition": "Integer age in days > 365; age 365 is active and age 366 is stale.",
        "metric_unit": "GiB (2**30 bytes)",
        "age_bins": "18 approximate 30-day months through age 540 inclusive, then 541+; stale remains strictly age >365.",
        "first_scan": START.isoformat(), "last_scan": (START + timedelta(days=7 * WEEKS)).isoformat(),
        "scan_count_per_disk": WEEKS + 1, "interval_days": 7,
        "schedule": "Initially seed one active cohort at each integer age 0..365 and stale cohorts at age 450. "
                    "Add one new active cohort on each intervening day; each week remove only groups already "
                    "stale at the previous scan. Atlas adds 128 GiB/day and removes 512 GiB/week; "
                    "birch adds 64 GiB/day and removes 448 GiB/week.",
        "retention": "Only the latest weekly file inventories are exported; historical inventories exist transiently during generation only.",
        "assumptions": ["Complete scans", "Stable disk and file identities", "No reaccess, size changes or migration",
                        "Scheduled synthetic removals are known only to the generator, not inferred actor attribution"],
        "disks": disk_summaries, "intervals": truth,
        "fleet": {key: sum(item[key] for item in disk_summaries) for key in (
            "initial_stale_gib", "final_stale_gib", "scheduled_stale_removals_gib", "aged_in_gib")},
    }
    return {
        "candidate/data/scans.csv": _csv(
            ["disk", "owner", "scanned_at", "complete", "total_gib", "stale_gib"]
            + [column for column, _, _ in AGE_COLUMNS], scans),
        "candidate/data/files.csv": _csv(
            ["disk", "scanned_at", "file_id", "path", "size_gib", "days_since_last_access"], files),
        "interviewer/ground_truth.json": (json.dumps(ground_truth, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    }


def main() -> None:
    """Write deterministic fixtures, or verify them read-only with --check.

    Returns:
        None after successful generation or a successful byte-for-byte check.

    Raises:
        SystemExit: Argument parsing requests help, options are invalid, or
            --check finds a missing or different fixture; mismatches exit 1.
        OSError: A fixture cannot be read or written.
        AssertionError: The synthetic schedule fails an internal consistency check.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Compare fixtures without writing any files")
    args = parser.parse_args()
    mismatches = []
    for relative, expected in generate_contents().items():
        path = ROOT / relative
        if args.check:
            if not path.exists() or path.read_bytes() != expected:
                mismatches.append(relative)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(expected)
            print("Wrote " + relative)
    if mismatches:
        parser.exit(1, "Fixture mismatch: {}\n".format(", ".join(mismatches)))
    if args.check:
        print("All 3 generated fixtures match byte-for-byte; no files written.")


if __name__ == "__main__":
    main()
