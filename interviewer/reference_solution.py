"""Interviewer-only examples of estimated and observable stale exits in GiB.

Aggregate age-bin estimates depend on stated assumptions; a signed residual is
not verified team cleanup. File comparisons distinguish disappearance from
reaccess and require additional historical inventory acquisition. Missing
files do not establish deletion or actor attribution. The supplied latest-only
repository cannot perform historical file comparisons, and the CLI reports
that limitation rather than reconstructing unavailable records.
"""

import argparse
import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Dict, Tuple

from candidate.storage_metrics.models import FileRecord, Snapshot
from candidate.storage_metrics.repository import FileHistoryUnavailable, SnapshotRepository

def _validate_pair(previous: Snapshot, current: Snapshot) -> int:
    """Return elapsed days, rejecting mixed disks, incomplete scans, or non-increasing dates."""
    if previous.disk != current.disk:
        raise ValueError("Cannot compare different disks")
    if not previous.complete or not current.complete:
        raise ValueError("Complete scans are required")
    elapsed = (current.scanned_at - previous.scanned_at).days
    if elapsed <= 0:
        raise ValueError("Scan dates must be strictly increasing")
    return elapsed


@dataclass(frozen=True)
class AggregateEstimate:
    """Signed aggregate estimate with exact decimal-derived Fraction quantities.

    Attributes:
        estimated_stale_exits_gib: Previous stale GiB plus estimated age-in,
            minus current stale GiB. Negative values are retained, not clamped.
        expected_inflow_gib: Estimated GiB of previously active storage that
            crosses the strictly-greater-than-365-day threshold in the interval.
        caveats: Limitations, inferred crossing-bin split, and any uniform-age
            assumptions. The estimate does not prove deletion or team cleanup.
    """
    estimated_stale_exits_gib: Fraction
    expected_inflow_gib: Fraction
    caveats: Tuple[str, ...]


def estimate_stale_exits(
    repository: SnapshotRepository, previous: Snapshot, current: Snapshot
) -> AggregateEstimate:
    """Estimate signed stale exits from two compatible aggregate scans.

    Args:
        repository: Source of the previous scan's inclusive monthly age bins.
        previous: Earlier complete scan for a disk.
        current: Later complete scan for the same disk.

    Returns:
        AggregateEstimate in GiB. Exact previous stale GiB minus wholly-stale
        bins identifies the stale portion of the single crossing bin; its
        remaining active portion is allocated uniformly across active ages.
        Other partial overlaps assume uniform GiB per integer age day. Full
        overlaps contribute their entire quantity. Arithmetic uses Fractions
        constructed from decimal value strings and preserves signed residuals.
        All inference and uniform-allocation assumptions are stated in caveats.

    Raises:
        ValueError: Scans differ in disk, are incomplete or out of order, the
            crossing-bin split is incompatible with exact stale GiB, or a
            partial unbounded bin cannot be estimated.

    Reaccess, size changes, and incoming stale data can affect the residual;
    this calculation does not establish who removed data or prove deletion.
    """
    elapsed = _validate_pair(previous, current)
    lower = max(0, 366 - elapsed)
    inflow = Fraction(0)
    caveats = ["Estimated stale exits, not proof of deletion or team attribution; incoming stale data, "
               "reaccess and size changes can affect the residual."]
    buckets = repository.get_age_buckets(previous.disk, previous.scanned_at)
    wholly_stale = sum((Fraction(str(bucket.size_gib)) for bucket in buckets if bucket.min_age_days > 365), Fraction(0))
    for bucket in buckets:
        overlap_lower = max(lower, bucket.min_age_days)
        overlap_upper = min(365, bucket.max_age_days) if bucket.max_age_days is not None else 365
        if overlap_lower > overlap_upper:
            continue
        size = Fraction(str(bucket.size_gib))
        if bucket.min_age_days <= 365 and (bucket.max_age_days is None or bucket.max_age_days > 365):
            stale_portion = Fraction(str(previous.stale_gib)) - wholly_stale
            if not 0 <= stale_portion <= size:
                raise ValueError("Exact stale GiB are incompatible with the crossing age bin")
            active_portion = size - stale_portion
            inflow += active_portion * Fraction(overlap_upper - overlap_lower + 1, 366 - bucket.min_age_days)
            caveats.append("Inferred split of crossing bin {}..{} from exact stale GiB minus wholly-stale bins: "
                           "{} GiB stale, {} GiB active. Age-in assumes uniform GiB per day within its active ages {}..365.".format(
                               bucket.min_age_days, bucket.max_age_days, stale_portion, active_portion, bucket.min_age_days))
        elif overlap_lower == bucket.min_age_days and overlap_upper == bucket.max_age_days:
            inflow += size
        else:
            if bucket.max_age_days is None:
                raise ValueError("Cannot uniformly estimate a partially overlapped unbounded age bucket")
            inflow += size * Fraction(overlap_upper - overlap_lower + 1, bucket.max_age_days - bucket.min_age_days + 1)
            caveats.append("Partial overlap of age bucket {}..{}: uniform GiB-per-age-day estimate.".format(
                bucket.min_age_days, bucket.max_age_days))
    return AggregateEstimate(Fraction(str(previous.stale_gib)) + inflow - Fraction(str(current.stale_gib)),
                             inflow, tuple(caveats))


@dataclass(frozen=True)
class FileComparison:
    """Identity-matched observations from two acquired complete inventories.

    All quantities are GiB Fractions. Missing records indicate observable
    disappearance, not proof of deletion or team attribution.

    Attributes:
        previous_stale_gib: Stale GiB measured in the earlier file inventory.
        current_stale_gib: Stale GiB measured in the later file inventory.
        missing_previously_stale_gib: Earlier size of stale files whose stable
            IDs are absent from the later inventory.
        reaccessed_previously_stale_gib: Earlier size of stale files retained
            with a current age at or below 365, separate from missing files.
        aged_in_gib: Current size of retained files that changed from active
            to stale between scans.
        first_seen_stale_gib: Current size of stale files with previously unseen IDs.
        retained_stale_size_delta_gib: Signed size change for files stale in
            both inventories, allowing either growth or shrinkage.
    """
    previous_stale_gib: Fraction
    current_stale_gib: Fraction
    missing_previously_stale_gib: Fraction
    reaccessed_previously_stale_gib: Fraction
    aged_in_gib: Fraction
    first_seen_stale_gib: Fraction
    retained_stale_size_delta_gib: Fraction

    @property
    def conservation_residual_gib(self) -> Fraction:
        """Return predicted minus observed current stale GiB; zero means the accounting balances."""
        predicted = (self.previous_stale_gib - self.missing_previously_stale_gib
                     - self.reaccessed_previously_stale_gib + self.aged_in_gib
                     + self.first_seen_stale_gib + self.retained_stale_size_delta_gib)
        return predicted - self.current_stale_gib


def _is_stale(record: FileRecord) -> bool:
    """Classify the record's measured integer age using the strict >365-day threshold."""
    return record.days_since_last_access > 365


def _inventory(repository: SnapshotRepository, snapshot: Snapshot) -> Dict[str, FileRecord]:
    """Materialize one inventory, rejecting identity errors or mismatched aggregate GiB totals."""
    records = {}
    for record in repository.iter_files(snapshot.disk, snapshot.scanned_at):
        if (record.disk != snapshot.disk or record.scanned_at != snapshot.scanned_at
                or record.file_id in records):
            raise ValueError("Inventory identity mismatch or duplicate file ID")
        records[record.file_id] = record
    if (not math.isclose(math.fsum(record.size_gib for record in records.values()), snapshot.total_gib, rel_tol=1e-9, abs_tol=1e-9)
            or not math.isclose(math.fsum(record.size_gib for record in records.values() if _is_stale(record)), snapshot.stale_gib,
                                rel_tol=1e-9, abs_tol=1e-9)):
        raise ValueError("Inventory does not match scan totals")
    return records


def compare_files(
    repository: SnapshotRepository, previous: Snapshot, current: Snapshot
) -> FileComparison:
    """Compare stable file identities without equating disappearance to deletion.

    Args:
        repository: Source capable of supplying both dated complete inventories.
            The supplied latest-only repository cannot provide this history.
        previous: Earlier complete scan for a disk.
        current: Later complete scan for that same disk.

    Returns:
        FileComparison with separate missing, reaccessed, aged-in, first-seen
        stale, and retained-stale size-change quantities in GiB. Matching uses
        stable IDs, not file paths. Ages are measured at each record's own scan
        date; older ages are not reused as current ages. Neither disappearance
        nor a balanced conservation check establishes cleanup attribution.

    Raises:
        FileHistoryUnavailable: Either requested inventory is not retained.
        ValueError: Scans are incompatible, file identities do not match the
            requested scan, IDs are duplicated, or file totals disagree with
            aggregates beyond the allowed floating-point tolerance.
        OSError: An acquired inventory cannot be read.

    Both inventories are materialized for this small interviewer example;
    historical data must be acquired separately, never inferred as raw records.
    """
    _validate_pair(previous, current)
    before, after = _inventory(repository, previous), _inventory(repository, current)
    missing = reaccessed = aged_in = first_seen = retained_delta = Fraction(0)
    for identity, old in before.items():
        if not _is_stale(old):
            continue
        new = after.get(identity)
        if new is None:
            missing += Fraction(str(old.size_gib))
        elif not _is_stale(new):
            reaccessed += Fraction(str(old.size_gib))
        else:
            retained_delta += Fraction(str(new.size_gib)) - Fraction(str(old.size_gib))
    for identity, new in after.items():
        if not _is_stale(new):
            continue
        old = before.get(identity)
        if old is None:
            first_seen += Fraction(str(new.size_gib))
        elif not _is_stale(old):
            aged_in += Fraction(str(new.size_gib))
    previous_stale = sum((Fraction(str(record.size_gib)) for record in before.values() if _is_stale(record)), Fraction(0))
    current_stale = sum((Fraction(str(record.size_gib)) for record in after.values() if _is_stale(record)), Fraction(0))
    return FileComparison(previous_stale, current_stale, missing, reaccessed,
                          aged_in, first_seen, retained_delta)


def main() -> None:
    """Print aggregate estimates and/or the historical-file acquisition limitation.

    --method selects aggregate, files, or both. Estimates and their annual
    totals use GiB and include assumptions; unavailable file history is reported
    explicitly without producing a false disappearance measurement.

    Raises:
        SystemExit: Argument parsing requests help or rejects invalid options.
        ValueError: Aggregate data or comparisons are invalid.
        OSError: Required CSV data cannot be read.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--method", choices=("aggregate", "files", "both"), default="both")
    args = parser.parse_args()
    repository = SnapshotRepository()
    print("Interviewer reference: estimated stale exits, NOT verified team cleanup.")
    if args.method in ("files", "both"):
        history = repository.list_snapshots()
        try:
            compare_files(repository, history[0], history[1])
        except FileHistoryUnavailable as error:
            print("File comparison unavailable: " + str(error))
            print("Extra acquisition required: retain at least two complete dated inventories with stable file IDs "
                  "(or an equivalent change feed). Missing files still do not prove deletion or team attribution.")
    if args.method == "files":
        return
    totals = {}
    previous = None
    for current in repository.list_snapshots():
        if previous is not None and previous.disk == current.disk:
            print("\n{}: {} -> {}".format(current.disk, previous.scanned_at, current.scanned_at))
            result = estimate_stale_exits(repository, previous, current)
            print("  Estimated stale exits: {:.4f} GiB; expected age-in: {:.4f} GiB".format(
                float(result.estimated_stale_exits_gib), float(result.expected_inflow_gib)))
            for caveat in result.caveats:
                print("  Caveat: " + caveat)
            totals[current.disk] = totals.get(current.disk, Fraction(0)) + result.estimated_stale_exits_gib
        previous = current
    print("\nYear totals of estimated stale exits (not verified team cleanup):")
    for disk, value in sorted(totals.items()):
        print("  {}: {:.4f} GiB".format(disk, float(value)))
    print("  Fleet: {:.4f} GiB".format(float(sum(totals.values()))))


if __name__ == "__main__":
    main()
