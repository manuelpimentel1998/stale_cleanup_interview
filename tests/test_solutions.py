"""Checks for the two interviewer solution tracks in interviewer/solutions."""

import unittest
from dataclasses import replace
from datetime import date
from fractions import Fraction

from candidate.storage_metrics.models import AgeBucket, FileRecord, Snapshot
from candidate.storage_metrics.repository import FileHistoryUnavailable, SnapshotRepository
from interviewer.solutions.cohort import AGING_BUCKET, KPI_NAME, calculate_kpi as cohort_kpi, estimate_cleanup, month_pairs
from interviewer.solutions.inventory_diff import (
    _demo, calculate_kpi as inventory_kpi, compare_repository, compare_stale_inventories,
)


class BucketRepository:
    def __init__(self, buckets):
        self.buckets = buckets

    def get_age_buckets(self, disk, scanned_at):
        return self.buckets[(disk, scanned_at)]


class HistoryRepository:
    """Minimal retaining repository for exercising the KPI hook end to end."""

    def __init__(self, snapshots, files):
        self.snapshots = snapshots
        self.files = files

    def list_snapshots(self, disk=None):
        return sorted((snapshot for snapshot in self.snapshots if disk is None or snapshot.disk == disk),
                      key=lambda snapshot: snapshot.scanned_at)

    def iter_files(self, disk, scanned_at=None):
        return iter(self.files[(disk, scanned_at)])


def record(identity, size, scanned_at, age, path=None):
    return FileRecord("/synthetic/disk", scanned_at, identity, path or "/synthetic/{}.dat".format(identity),
                      size, age)


def snapshot(scanned_at, files):
    return Snapshot("/synthetic/disk", "team", scanned_at, True,
                    sum(item.size_gib for item in files),
                    sum(item.size_gib for item in files if item.days_since_last_access >= 365))


class CohortTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repository = SnapshotRepository()

    def test_month_pairs_are_28_days_and_do_not_overlap(self):
        pairs = list(month_pairs(self.repository))
        self.assertEqual(len(pairs), 26)  # 13 months x 2 disks
        for previous, current in pairs:
            self.assertEqual((current.scanned_at - previous.scanned_at).days, 28)
        atlas = [(previous, current) for previous, current in pairs if current.disk == "/projects/atlas"]
        self.assertEqual(atlas[0][0].scanned_at, date(2025, 11, 23))
        self.assertEqual(atlas[-1][1].scanned_at, date(2026, 11, 22))

    def test_fixture_estimate_uses_previous_11_12_month_bucket(self):
        previous, current = next(month_pairs(self.repository, "/projects/atlas"))
        result = estimate_cleanup(self.repository, previous, current)
        self.assertEqual(result.elapsed_days, 28)
        self.assertEqual(result.aging_inbound_gib, Fraction(4480))
        self.assertEqual(result.estimated_cleanup_gib, Fraction(2944))  # truth is 2048
        self.assertTrue(any("330-365" in caveat and "7 of 35" in caveat for caveat in result.caveats))
        self.assertTrue(any("not proof of deletion" in caveat for caveat in result.caveats))

    def test_year_totals_have_a_stated_positive_bias(self):
        totals = {}
        for previous, current in month_pairs(self.repository):
            totals[current.disk] = totals.get(current.disk, Fraction(0)) + estimate_cleanup(
                self.repository, previous, current).estimated_cleanup_gib
        self.assertEqual(totals["/projects/atlas"], Fraction(13 * 2944))
        self.assertEqual(totals["/projects/birch"], Fraction(13 * 2240))

    def test_negative_residual_is_retained(self):
        previous = Snapshot("/synthetic/disk", "team", date(2026, 6, 1), True, 500, 100)
        current = replace(previous, scanned_at=date(2026, 6, 29), stale_gib=150)
        repository = BucketRepository({(previous.disk, previous.scanned_at): [
            AgeBucket(AGING_BUCKET[0], AGING_BUCKET[1], 10)]})
        self.assertEqual(estimate_cleanup(repository, previous, current).estimated_cleanup_gib, Fraction(-40))

    def test_weekly_and_incompatible_pairs_are_rejected(self):
        history = self.repository.list_snapshots("/projects/atlas")
        with self.assertRaisesRegex(ValueError, "28-day step"):
            estimate_cleanup(self.repository, history[0], history[1])
        previous, current = history[0], history[4]
        with self.assertRaises(ValueError):
            estimate_cleanup(self.repository, previous, replace(current, disk="/projects/other"))
        with self.assertRaises(ValueError):
            estimate_cleanup(self.repository, replace(previous, complete=False), current)

    def test_calculate_kpi_uses_a_trailing_28_day_baseline(self):
        history = self.repository.list_snapshots("/projects/atlas")
        self.assertIsNone(cohort_kpi(self.repository, history[0], history[1]))  # no month of history yet
        result = cohort_kpi(self.repository, history[3], history[4])  # current 2025-12-21, baseline 2025-11-23
        self.assertEqual((result.name, result.value, result.unit), (KPI_NAME, 2944.0, "GiB"))
        self.assertEqual(cohort_kpi(self.repository, history[0], history[4]).value, 2944.0)


class InventoryDiffTests(unittest.TestCase):
    def test_missing_reaccess_retained_and_first_seen_are_separate(self):
        start, end = date(2026, 6, 1), date(2026, 7, 1)
        before = {item.file_id: item for item in [
            record("gone", 10, start, 400), record("reacc", 20, start, 400),
            record("keep", 30, start, 400), record("active", 40, start, 100)]}
        after = {item.file_id: item for item in [
            record("reacc", 20, end, 0), record("keep", 45, end, 430),
            record("new", 50, end, 500), record("active", 40, end, 128)]}
        result = compare_stale_inventories(before, after)
        self.assertEqual(result.disappeared_gib, 10)
        self.assertEqual(result.reaccessed_gib, 20)
        self.assertEqual(result.retained_size_delta_gib, 15)
        self.assertEqual(result.first_seen_stale_gib, 50)
        self.assertEqual(result.conservation_residual_gib, 0)
        self.assertFalse(hasattr(result, "deleted_gib"))

    def test_residual_is_zero_for_the_checked_demo(self):
        self.assertEqual(_demo().conservation_residual_gib, 0)

    def test_empty_inventories_balance(self):
        result = compare_stale_inventories({}, {})
        self.assertEqual(result.conservation_residual_gib, 0)
        self.assertEqual(result.disappeared_gib, 0)

    def test_repository_comparison_requires_retained_history(self):
        repository = SnapshotRepository()
        history = repository.list_snapshots("/projects/atlas")
        with self.assertRaisesRegex(FileHistoryUnavailable, "Only latest weekly detail is retained"):
            compare_repository(repository, history[-5], history[-1])

    def test_calculate_kpi_returns_none_when_history_is_unavailable(self):
        repository = SnapshotRepository()
        history = repository.list_snapshots("/projects/atlas")
        self.assertIsNone(inventory_kpi(repository, history[-2], history[-1]))

    def test_calculate_kpi_reports_month_of_disappeared_stale_data(self):
        start, end = date(2026, 6, 1), date(2026, 6, 29)
        before = [record("gone", 10, start, 400), record("keep", 30, start, 400)]
        after = [record("keep", 30, end, 428)]
        previous, current = snapshot(start, before), snapshot(end, after)
        repository = HistoryRepository([previous, current],
                                       {(previous.disk, start): before, (current.disk, end): after})
        result = inventory_kpi(repository, previous, current)
        self.assertEqual(result.name, "Trailing-month stale disappearance (file diff)")
        self.assertEqual((result.value, result.unit), (10.0, "GiB"))


if __name__ == "__main__":
    unittest.main()
