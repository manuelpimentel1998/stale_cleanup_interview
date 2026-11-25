"""Rolling stale-inventory comparison and its retention scale.

Keep the previous month's files at least 365 days old, keyed by stable file ID,
then diff today's inventory against that list:

    disappeared   in the old stale list, absent today        -> cleanup proxy
    reaccessed    present today but now at age < 365          -> left stale stock
    retained      stale in both; size delta tracked           -> stays stale
    first seen    stale today with no old stale record        -> aging-in or new

``disappeared`` is the requested cleanup measure: the GiB of previously stale
files that no longer appear. It is an upper bound on team cleanup, not proof:
migration, lost mounts, identity churn and rename-with-new-ID look identical.
Keeping only stale IDs also means aging-in cannot be separated from files first
observed stale; retain a near-threshold active set or all IDs to split them.

Retention scale, computed once (this module does not compute it):
    one record = full path (~100 B) + size (8 B) + age (4 B) = 112 B
    1 disk x 1 billion files x 25% stale = 250M records x 112 B ~= 28 GB/month
    3,000 disks x 1 billion files x 25%  = 750B records x 112 B ~= 84 TB/month
    twelve monthly copies ~= 1 PB

That is why full file-level history is hard to scale. Sorted-path compression,
sampling or a Bloom filter cut the byte count substantially, but do not change
the order of magnitude of the retention decision. Historical inventories are not
in the supplied fixture, so the CLI reports the acquisition gap instead of
faking a result.

``calculate_kpi`` implements the candidate hook over a trailing 28-day month and
returns None when the required inventories are not retained, so the report and
plotting helpers can call it without crashing.
"""

import argparse
import math
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from typing import Dict, Mapping, Optional, Tuple

from candidate.storage_metrics.models import FileRecord, KPIResult, Snapshot
from candidate.storage_metrics.repository import FileHistoryUnavailable, SnapshotRepository
from interviewer.solutions.common import month_pair, validate_pair

STALE_AFTER_DAYS = 365
KPI_NAME = "Trailing-month stale disappearance (file diff)"


@dataclass(frozen=True)
class InventoryDiff:
    """Identity-matched stale accounting between two inventories.

    Attributes:
        previous_stale_gib: Exact stale GiB in the earlier inventory.
        current_stale_gib: Exact stale GiB in the later inventory.
        disappeared_gib: Earlier size of previously stale IDs absent later.
        reaccessed_gib: Earlier size of previously stale IDs now at age < 365.
        retained_stale_gib: Later size of IDs stale in both inventories.
        retained_size_delta_gib: Signed size change for retained stale IDs.
        first_seen_stale_gib: Later size of stale IDs absent from the earlier
            inventory (aging-in cannot be isolated without active IDs).
        caveats: Attribution and identity limitations of the comparison.
    """
    previous_stale_gib: Fraction
    current_stale_gib: Fraction
    disappeared_gib: Fraction
    reaccessed_gib: Fraction
    retained_stale_gib: Fraction
    retained_size_delta_gib: Fraction
    first_seen_stale_gib: Fraction
    caveats: Tuple[str, ...]

    @property
    def conservation_residual_gib(self) -> Fraction:
        """Return predicted minus observed current stale GiB; zero balances."""
        predicted = (self.previous_stale_gib - self.disappeared_gib - self.reaccessed_gib
                     + self.retained_size_delta_gib + self.first_seen_stale_gib)
        return predicted - self.current_stale_gib


def _is_stale(record: FileRecord) -> bool:
    return record.days_since_last_access >= STALE_AFTER_DAYS


def compare_stale_inventories(previous: Mapping[str, FileRecord],
                              current: Mapping[str, FileRecord]) -> InventoryDiff:
    """Diff two inventories keyed by stable file ID.

    Args:
        previous: Earlier inventory, matched by ``file_id``. For the rolling
            design this is only the previous month's stale IDs; in that case
            ``first_seen_stale_gib`` includes aging-in.
        current: Later inventory. Active records participate only as evidence
            that a previously stale ID was reaccessed.

    Returns:
        InventoryDiff in GiB. Matching uses IDs, not paths. Missing records and
        a zero conservation residual do not establish deletion or attribution.

    Both sides are materialized here for clarity. A production job merges two
    ID-sorted streams instead, so memory is bounded by the merge batch.
    """
    previous_stale = {identity: record for identity, record in previous.items() if _is_stale(record)}
    disappeared = reaccessed = retained = delta = Fraction(0)
    for identity, old in previous_stale.items():
        new = current.get(identity)
        if new is None:
            disappeared += Fraction(str(old.size_gib))
        elif not _is_stale(new):
            reaccessed += Fraction(str(old.size_gib))
        else:
            retained += Fraction(str(new.size_gib))
            delta += Fraction(str(new.size_gib)) - Fraction(str(old.size_gib))
    first_seen = sum((Fraction(str(record.size_gib)) for identity, record in current.items()
                      if _is_stale(record) and identity not in previous), Fraction(0))
    previous_stale_gib = sum((Fraction(str(record.size_gib)) for record in previous_stale.values()), Fraction(0))
    current_stale_gib = sum((Fraction(str(record.size_gib)) for record in current.values() if _is_stale(record)),
                            Fraction(0))
    caveats = (
        "Disappearance from two complete comparable inventories is an upper bound on cleanup; migration, "
        "lost mounts, identity churn and rename-with-new-ID are indistinguishable. Attribution needs action evidence.",
        "Only stale IDs are retained, so first-seen stale GiB mixes aging-in with genuinely new files. "
        "Retain a near-threshold active set or all IDs to separate them.",
        "Logical GiB, not physical space; snapshots, deduplication, links and compression can differ.",
    )
    return InventoryDiff(previous_stale_gib, current_stale_gib, disappeared, reaccessed,
                         retained, delta, first_seen, caveats)


def _materialize(repository: SnapshotRepository, snapshot: Snapshot) -> Dict[str, FileRecord]:
    """Load one inventory, rejecting identity mismatches and duplicate IDs."""
    records: Dict[str, FileRecord] = {}
    for record in repository.iter_files(snapshot.disk, snapshot.scanned_at):
        if record.disk != snapshot.disk or record.scanned_at != snapshot.scanned_at:
            raise ValueError("Inventory identity mismatch")
        if record.file_id in records:
            raise ValueError("Duplicate file ID: " + record.file_id)
        records[record.file_id] = record
    if not math.isclose(math.fsum(record.size_gib for record in records.values()),
                        snapshot.total_gib, rel_tol=1e-9, abs_tol=1e-9):
        raise ValueError("Inventory does not match scan totals")
    return records


def compare_repository(repository: SnapshotRepository, previous: Snapshot, current: Snapshot) -> InventoryDiff:
    """Compare two repository inventories, requiring retained history.

    Raises:
        FileHistoryUnavailable: Either dated inventory is not retained. The
            supplied latest-only repository always raises for older scans.
        ValueError: Scans are incompatible, identities are duplicated, or file
            totals disagree with the aggregate scan.
        OSError: An acquired inventory cannot be read.
    """
    validate_pair(previous, current)
    return compare_stale_inventories(_materialize(repository, previous), _materialize(repository, current))


def calculate_kpi(repository: SnapshotRepository, previous: Snapshot,
                  current: Snapshot) -> Optional[KPIResult]:
    """Implement the candidate KPI hook for consecutive scans.

    Uses the scan exactly 28 days before ``current`` as the baseline, because a
    weekly pair cannot show a month of disappearances. Returns None when the
    baseline or either inventory is not retained. The supplied latest-only
    repository therefore yields gaps; its CLI reports the acquisition gap
    instead of treating missing history as an empty inventory.

    The value is the trailing month's GiB of previously stale files that no
    longer appear. That is a cleanup proxy, not proven deletion or attribution.
    """
    pair = month_pair(repository, previous, current)
    if pair is None:
        return None
    try:
        result = compare_repository(repository, *pair)
    except FileHistoryUnavailable:
        return None
    return KPIResult(KPI_NAME, float(result.disappeared_gib), "GiB")


def _demo() -> InventoryDiff:
    """Return a four-record verification case with a known balance.

    Earlier stale IDs: gone (10 GiB), reaccessed (20 GiB), retained and grown
    (30 -> 45 GiB). Later additionally has a first-seen stale file (50 GiB).
    Expected current stale = 45 + 50 = 95 GiB and zero residual.
    """
    start, end = date(2026, 6, 1), date(2026, 7, 1)

    def record(identity: str, size: int, scanned_at: date, age: int) -> FileRecord:
        return FileRecord("/synthetic/disk", scanned_at, identity, "/synthetic/{}.dat".format(identity), size, age)

    before = {name: record(name, size, start, 400)
              for name, size in [("gone", 10), ("reacc", 20), ("keep", 30)]}
    after = {name: record(name, size, end, age)
             for name, size, age in [("reacc", 20, 0), ("keep", 45, 430), ("new", 50, 500)]}
    result = compare_stale_inventories(before, after)
    if (result.disappeared_gib, result.reaccessed_gib, result.retained_size_delta_gib,
            result.first_seen_stale_gib, result.conservation_residual_gib) != (10, 20, 15, 50, 0):
        raise AssertionError("Inventory demo no longer balances: {}".format(result))
    return result


def main() -> None:
    """Report the acquisition gap and retention scale, run a demo, or plot.

    ``--plot`` calls the plotting helper with this module's ``calculate_kpi``.
    On the supplied latest-only fixture that produces only the stale graph and
    empty KPI gaps, which is the truthful result.

    Raises:
        SystemExit: Argument parsing requests help or rejects invalid options.
        ValueError: Repository data is invalid or scans are incompatible.
        OSError: A fixture or graph file cannot be read or written.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir")
    parser.add_argument("--demo", action="store_true", help="Run the checked constructed inventory diff")
    parser.add_argument("--plot", metavar="DIR", help="Save the KPI graph with calculate_kpi")
    args = parser.parse_args()
    if args.plot:
        from candidate.storage_metrics.plotting import save_graphs
        for path in save_graphs(SnapshotRepository(args.data_dir), args.plot, calculator=calculate_kpi):
            print("Saved " + str(path))
        return
    if args.demo:
        result = _demo()
        print("Checked demo: disappeared {:.0f} GiB, reaccessed {:.0f} GiB, retained delta {:+.0f} GiB, "
              "first seen {:.0f} GiB, conservation residual {:.0f} GiB.".format(
                  float(result.disappeared_gib), float(result.reaccessed_gib),
                  float(result.retained_size_delta_gib), float(result.first_seen_stale_gib),
                  float(result.conservation_residual_gib)))
        for caveat in result.caveats:
            print("  Caveat: " + caveat)
    repository = SnapshotRepository(args.data_dir)
    for disk in sorted({snapshot.disk for snapshot in repository.list_snapshots()}):
        history = repository.list_snapshots(disk)
        try:
            compare_repository(repository, history[-5], history[-1])
            print("{}: historical inventory comparison available.".format(disk))
        except FileHistoryUnavailable as error:
            print("{}: file comparison unavailable: {}".format(disk, error))
    print("\nRequires new acquisition: retain the previous month's stale IDs (or an equivalent change feed).")
    print("Retention scale (path ~100 B + size 8 B + age 4 B): about 28 GB per disk per month at "
          "1 billion files x 25% stale; 3,000 disks is about 84 TB per month, or about 1 PB for a year.")


if __name__ == "__main__":
    main()
