"""Month-over-month cohort estimate of stale-data cleanup from aggregates only.

For a pair of scans 28 days apart, assume that is one month and that every file
in the previous scan's 11-12 month age bucket (ages 330..364, the 330-365 bin)
has reached the 365-day-or-more threshold. The month's aging inflow is therefore
that bucket's GiB, and cleanup closes the stale-stock balance:

    estimated cleanup = previous stale + aging inflow - current stale

The estimate is signed. ``current stale - inflow`` is only the stock left after
subtracting inflow; it is not the interval's cleanup. This calculation is an
estimate, not proof of deletion or team attribution.

``calculate_kpi`` implements the candidate hook: for consecutive weekly calls it
resolves the scan exactly 28 days before ``current``, so the report and plotting
helpers can call this solution directly. Verification against
``ground_truth.json`` is interviewer-only.
"""

import argparse
import json
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from candidate.storage_metrics.models import KPIResult, Snapshot
from candidate.storage_metrics.repository import SnapshotRepository
from interviewer.solutions.common import MONTH_DAYS, month_pair, validate_pair

AGING_BUCKET = (330, 364)  # 330-365 bin; upper edge 365 is exclusive
TRUTH_PATH = Path(__file__).resolve().parents[1] / "ground_truth.json"
KPI_NAME = "Trailing-month estimated cleanup (cohort)"


@dataclass(frozen=True)
class CohortEstimate:
    """One month's stale-stock balance with its assumed inflow.

    Attributes:
        elapsed_days: Days between the two scans; 28 by construction.
        previous_stale_gib: Exact stale GiB at the earlier scan.
        aging_inbound_gib: Previous 11-12 month bucket GiB, assumed to have
            crossed the threshold during the month.
        current_stale_gib: Exact stale GiB at the later scan.
        caveats: Stated assumptions and limitations, including the bucket
            boundary bias and the lack of attribution.
    """
    elapsed_days: int
    previous_stale_gib: Fraction
    aging_inbound_gib: Fraction
    current_stale_gib: Fraction
    caveats: Tuple[str, ...]

    @property
    def estimated_cleanup_gib(self) -> Fraction:
        """Return previous stock plus assumed inflow minus current stock."""
        return self.previous_stale_gib + self.aging_inbound_gib - self.current_stale_gib


def month_pairs(repository: SnapshotRepository, disk: Optional[str] = None) -> Iterator[Tuple[Snapshot, Snapshot]]:
    """Yield non-overlapping same-disk scan pairs exactly 28 days apart.

    Args:
        repository: Source of aggregate scan history.
        disk: Exact disk path to select, or None for every disk.

    Returns:
        Pairs in scan-date order per disk. A missing 28-day sample ends that
        disk's sequence rather than interpolating one.
    """
    histories: Dict[str, List[Snapshot]] = {}
    for snapshot in repository.list_snapshots(disk):
        histories.setdefault(snapshot.disk, []).append(snapshot)
    for history in histories.values():
        start = 0
        while start + 1 < len(history):
            end = next((index for index in range(start + 1, len(history))
                        if (history[index].scanned_at - history[start].scanned_at).days == MONTH_DAYS), None)
            if end is None:
                break
            yield history[start], history[end]
            start = end


def estimate_cleanup(repository: SnapshotRepository, previous: Snapshot, current: Snapshot) -> CohortEstimate:
    """Estimate one 28-day month of stale exits using the previous 11-12 bucket.

    Args:
        repository: Source of the previous scan's inclusive monthly age bins.
        previous: Earlier complete scan for a disk.
        current: Later complete scan for the same disk, exactly 28 days later.

    Returns:
        CohortEstimate in GiB with a signed cleanup value and explicit caveats.
        Inflow is the whole previous 330..364 bucket (the 330-365 edge bin), as
        specified by the cohort assumption, not a boundary-prorated subset.

    Raises:
        ValueError: Scans differ in disk, are incomplete or out of order, the
            step is not 28 days, or the bucket is absent.
    """
    elapsed = validate_pair(previous, current)
    if elapsed != MONTH_DAYS:
        raise ValueError("Cohort estimate needs a 28-day step, got {} days".format(elapsed))
    buckets = {(bucket.min_age_days, bucket.max_age_days): bucket
               for bucket in repository.get_age_buckets(previous.disk, previous.scanned_at)}
    if AGING_BUCKET not in buckets:
        raise ValueError("Previous scan has no {}-{} day age bucket".format(*AGING_BUCKET))
    inflow = Fraction(str(buckets[AGING_BUCKET].size_gib))
    # Ages that stay active: a + 28 < 365, bounded to this 35-day edge bin.
    still_active_days = min(35, max(0, 365 - elapsed - AGING_BUCKET[0]))
    caveats = (
        "Assumes 28 days is one month and treats the whole previous 330-365 bin (ages 330-364, "
        "11-12 months) as inflow. The true 28-day crossing set is ages 337-364: this bin's tail, "
        "because age 365 is already stale at the previous scan. So ages 330-336 ({} of 35 age-days) "
        "are counted but stay active; net overstatement is about 7 age-days.".format(still_active_days),
        "Estimated exits, not proof of deletion or team attribution; reaccess, size changes, migration, "
        "identity churn and incomplete scans all affect the signed residual.",
        "A negative value is retained, not clamped: it means the assumption does not explain the observed change.",
    )
    return CohortEstimate(elapsed, Fraction(str(previous.stale_gib)), inflow,
                          Fraction(str(current.stale_gib)), caveats)


def calculate_kpi(repository: SnapshotRepository, previous: Snapshot,
                  current: Snapshot) -> Optional[KPIResult]:
    """Implement the candidate KPI hook for consecutive scans.

    Consecutive weekly pairs cannot supply a monthly inflow directly, so the
    scan exactly 28 days before ``current`` is used as the baseline. Returns
    None until that baseline exists, leaving a gap rather than a fabricated
    value. If the supplied pair is already 28 days, it is used as-is.

    The result is the trailing month's estimated cleanup in GiB, not verified
    deletion and not the current stale stock.
    """
    pair = month_pair(repository, previous, current)
    if pair is None:
        return None
    result = estimate_cleanup(repository, *pair)
    return KPIResult(KPI_NAME, float(result.estimated_cleanup_gib), "GiB")


def _scheduled_removals(truth: Sequence[dict], disk: str, start: date, end: date) -> int:
    """Sum private synthetic removals for intervals inside the 28-day window."""
    return sum(interval["scheduled_stale_removals_gib"] for interval in truth
               if interval["disk"] == disk
               and start.isoformat() <= interval["previous_scanned_at"]
               and interval["current_scanned_at"] <= end.isoformat())


def main() -> None:
    """Print monthly cohort estimates, verify them, or plot the KPI hook.

    ``--verify`` uses the private synthetic ground truth, so it is available to
    the interviewer but not to the candidate. ``--plot`` calls the plotting
    helper with this module's ``calculate_kpi``, producing the trailing-month
    KPI graph from the same weekly hook the candidate would implement.

    Raises:
        SystemExit: Argument parsing requests help or rejects invalid options.
        ValueError: Repository data, scan pairs, or graph data are invalid.
        OSError: A CSV, ground-truth file, or graph output cannot be read or written.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disk")
    parser.add_argument("--data-dir")
    parser.add_argument("--verify", action="store_true", help="Compare with private synthetic removals")
    parser.add_argument("--plot", metavar="DIR", help="Save the KPI graph with calculate_kpi")
    args = parser.parse_args()
    repository = SnapshotRepository(args.data_dir)
    if args.plot:
        from candidate.storage_metrics.plotting import save_graphs
        for path in save_graphs(repository, args.plot, args.disk, calculator=calculate_kpi):
            print("Saved " + str(path))
        return
    truth = json.loads(TRUTH_PATH.read_text(encoding="utf-8"))["intervals"] if args.verify else []
    print("Month-over-month cohort estimate (28 days = 1 month): estimated cleanup, NOT verified team cleanup.")
    print("Formula: previous stale + previous 330-365 bucket - current stale.")
    totals: Dict[str, Fraction] = {}
    verified: Dict[str, List[int]] = {}
    for previous, current in month_pairs(repository, args.disk):
        result = estimate_cleanup(repository, previous, current)
        print("\n{}: {} -> {} ({} days)".format(current.disk, previous.scanned_at, current.scanned_at, result.elapsed_days))
        print("  previous stale {:.2f} GiB + assumed inflow {:.2f} GiB - current stale {:.2f} GiB".format(
            float(result.previous_stale_gib), float(result.aging_inbound_gib), float(result.current_stale_gib)))
        print("  estimated cleanup: {:.2f} GiB".format(float(result.estimated_cleanup_gib)))
        for caveat in result.caveats:
            print("  Caveat: " + caveat)
        totals[current.disk] = totals.get(current.disk, Fraction(0)) + result.estimated_cleanup_gib
        if args.verify:
            actual = _scheduled_removals(truth, current.disk, previous.scanned_at, current.scanned_at)
            verified.setdefault(current.disk, []).append(actual)
            print("  private synthetic removals: {} GiB; error: {:+.2f} GiB".format(
                actual, float(result.estimated_cleanup_gib) - actual))
    print("\nTotals of estimated cleanup:")
    for disk, value in sorted(totals.items()):
        line = "  {}: {:.2f} GiB".format(disk, float(value))
        if args.verify:
            actual = sum(verified[disk])
            line += " (private synthetic {} GiB, {:+.1f}%)".format(actual, 100 * (float(value) - actual) / actual)
        print(line)
    print("  Fleet: {:.2f} GiB".format(float(sum(totals.values()))))


if __name__ == "__main__":
    main()
