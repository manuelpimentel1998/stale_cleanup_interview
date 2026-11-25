"""Focused interviewer checks; intentionally outside the candidate directory."""

import csv
import io
import json
import tempfile
import unittest
from dataclasses import replace
from contextlib import redirect_stdout
from datetime import date, timedelta
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from candidate.storage_metrics.models import AgeBucket, FileRecord, KPIResult, Snapshot
from candidate.storage_metrics.report import render_report
from candidate.storage_metrics.repository import AGE_COLUMNS, FileHistoryUnavailable, SnapshotRepository
from candidate.storage_metrics.solution import calculate_kpi
from interviewer.generate_dataset import BOUNDS, ROOT, START, generate_contents, main as generator_main
from interviewer.reference_solution import compare_files, estimate_stale_exits


class FixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.expected_contents = generate_contents()

    def setUp(self):
        self.repository = SnapshotRepository()
        self.truth = json.loads((ROOT / "interviewer/ground_truth.json").read_text(encoding="utf-8"))

    def test_deterministic_fixtures_match_generator(self):
        first = self.expected_contents
        self.assertEqual(first, generate_contents())
        for relative, content in first.items():
            with self.subTest(path=relative):
                self.assertEqual((ROOT / relative).read_bytes(), content)
        self.assertEqual(set(first), {"candidate/data/scans.csv", "candidate/data/files.csv",
                                      "interviewer/ground_truth.json"})
        self.assertFalse((ROOT / "candidate/data/age_buckets.csv").exists())

    def test_check_mode_is_read_only(self):
        output = io.StringIO()
        with patch("sys.argv", ["generate_dataset", "--check"]), redirect_stdout(output):
            with patch.object(Path, "write_bytes", side_effect=AssertionError("--check attempted to write")):
                generator_main()
        self.assertIn("All 3 generated fixtures match byte-for-byte; no files written.", output.getvalue())

    def test_year_of_wide_aggregate_history(self):
        snapshots = self.repository.list_snapshots()
        self.assertEqual(len(snapshots), 106)
        for disk, initial, growth, owner in [("/projects/atlas", 10240, 384, "team-a"),
                                              ("/projects/birch", 4096, 0, "team-b")]:
            history = self.repository.list_snapshots(disk)
            self.assertEqual([item.scanned_at for item in history],
                             [date(2025, 11, 23) + timedelta(days=7 * index) for index in range(53)])
            self.assertEqual(history[-1].scanned_at, date(2026, 11, 22))
            self.assertEqual([item.stale_gib for item in history], [initial + index * growth for index in range(53)])
            self.assertEqual([item.total_gib for item in history],
                             [(57088 if disk == "/projects/atlas" else 27520) + index * growth for index in range(53)])
            self.assertEqual({item.owner for item in history}, {owner})
        with (ROOT / "candidate/data/scans.csv").open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 106)
        expected_bounds = [(0, 30)] + [(start, start + 29) for start in range(31, 512, 30)] + [(541, None)]
        self.assertEqual(BOUNDS, expected_bounds)
        self.assertEqual(len(AGE_COLUMNS), 19)
        self.assertEqual([column for column, _, _ in AGE_COLUMNS],
                         ["age_{}_{}_gib".format(lower, upper if upper is not None else "plus") for lower, upper in expected_bounds])
        for row in rows:
            self.assertEqual(sum(int(row[column]) for column, _, _ in AGE_COLUMNS), int(row["total_gib"]))
            wholly = sum(int(row[column]) for column, lower, _ in AGE_COLUMNS if lower > 365)
            self.assertLessEqual(wholly, int(row["stale_gib"]))
            self.assertLessEqual(int(row["stale_gib"]), wholly + int(row["age_361_390_gib"]))
            self.assertNotIn("disk_id", row)
            self.assertNotIn("snapshot_id", row)
        for snapshot in snapshots:
            with self.subTest(disk=snapshot.disk, scanned_at=snapshot.scanned_at):
                self.assertTrue(snapshot.complete)
                buckets = self.repository.get_age_buckets(snapshot.disk, snapshot.scanned_at)
                self.assertEqual([(item.min_age_days, item.max_age_days) for item in buckets], BOUNDS)
                self.assertEqual(sum(item.size_gib for item in buckets), snapshot.total_gib)

    def test_latest_inventories_match_bins_sizes_and_retention(self):
        with (ROOT / "candidate/data/files.csv").open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual({row["scanned_at"] for row in rows}, {"2026-11-22"})
        self.assertEqual({row["disk"] for row in rows}, {"/projects/atlas", "/projects/birch"})
        self.assertEqual(len(rows), 4128)
        self.assertNotIn("last_accessed", rows[0])
        self.assertEqual(list(rows[0]), ["disk", "scanned_at", "file_id", "path", "size_gib", "days_since_last_access"])
        self.assertTrue(all(int(row["days_since_last_access"]) >= 0 for row in rows))
        for disk, file_count, sizes in [("/projects/atlas", 2408, {8, 16, 32, 72}),
                                        ("/projects/birch", 1720, {4, 8, 16, 36})]:
            snapshot = self.repository.list_snapshots(disk)[-1]
            with self.subTest(disk=disk):
                files = list(self.repository.iter_files(disk))
                self.assertEqual(len(files), file_count)
                self.assertEqual(len({item.file_id for item in files}), len(files))
                self.assertEqual({item.size_gib for item in files}, sizes)
                self.assertTrue(all(item.disk == disk and item.scanned_at == snapshot.scanned_at
                                    and item.path.startswith(disk + "/") for item in files))
                self.assertEqual(sum(item.size_gib for item in files), snapshot.total_gib)
                stale = sum(item.size_gib for item in files if item.days_since_last_access > 365)
                self.assertEqual(stale, snapshot.stale_gib)
                buckets = self.repository.get_age_buckets(disk, snapshot.scanned_at)
                bucket_counts = []
                for bucket in buckets:
                    members = [item for item in files if bucket.min_age_days <= item.days_since_last_access
                               and (bucket.max_age_days is None or item.days_since_last_access <= bucket.max_age_days)]
                    self.assertEqual(sum(item.size_gib for item in members), bucket.size_gib)
                    bucket_counts.append(len(members))
                self.assertEqual(sum(bucket_counts), file_count)

    def test_aggregate_reference_matches_private_schedule_with_partial_caveat(self):
        snapshots = {(item.disk, item.scanned_at.isoformat()): item for item in self.repository.list_snapshots()}
        self.assertEqual(len(self.truth["intervals"]), 104)
        for interval in self.truth["intervals"]:
            previous = snapshots[(interval["disk"], interval["previous_scanned_at"])]
            current = snapshots[(interval["disk"], interval["current_scanned_at"])]
            with self.subTest(disk=current.disk, scanned_at=current.scanned_at):
                self.assertEqual(interval["elapsed_days"], 7)
                self.assertEqual(previous.stale_gib, interval["previous_stale_gib"])
                self.assertEqual(current.stale_gib, interval["current_stale_gib"])
                self.assertGreaterEqual(current.stale_gib, previous.stale_gib)
                aggregate = estimate_stale_exits(self.repository, previous, current)
                expected_removal = 512 if current.disk == "/projects/atlas" else 448
                expected_aging = 896 if current.disk == "/projects/atlas" else 448
                self.assertEqual(interval["scheduled_stale_removals_gib"], expected_removal)
                self.assertEqual(interval["aged_in_gib"], expected_aging)
                self.assertEqual(interval["new_active_gib"], expected_aging)
                self.assertEqual(len(interval["scheduled_removed_file_ids"]), 16 if current.disk == "/projects/atlas" else 28)
                self.assertEqual(aggregate.estimated_stale_exits_gib, expected_removal)
                self.assertEqual(aggregate.expected_inflow_gib, interval["aged_in_gib"])
                self.assertTrue(any("Partial overlap" in item and "uniform" in item for item in aggregate.caveats))
                self.assertTrue(any("not proof of deletion" in item for item in aggregate.caveats))
                self.assertTrue(any("Inferred split" in item and "uniform" in item for item in aggregate.caveats))
                with self.assertRaisesRegex(FileHistoryUnavailable, "Only latest weekly detail is retained"):
                    compare_files(self.repository, previous, current)
        summaries = {item["disk"]: item for item in self.truth["disks"]}
        self.assertEqual(summaries["/projects/atlas"]["scheduled_stale_removals_gib"], 26624)
        self.assertEqual(summaries["/projects/birch"]["scheduled_stale_removals_gib"], 23296)
        self.assertEqual(summaries["/projects/atlas"]["aged_in_gib"], 46592)
        self.assertEqual(summaries["/projects/birch"]["aged_in_gib"], 23296)
        self.assertEqual(self.truth["fleet"], {"initial_stale_gib": 14336, "final_stale_gib": 34304,
                                             "scheduled_stale_removals_gib": 49920, "aged_in_gib": 69888})

    def test_first_crossing_bin_has_only_five_active_days_not_thirty_uniform_days(self):
        for disk, daily in [("/projects/atlas", 128), ("/projects/birch", 64)]:
            history = self.repository.list_snapshots(disk)
            previous, current = history[:2]
            buckets = self.repository.get_age_buckets(disk, previous.scanned_at)
            crossing = next(bucket for bucket in buckets if bucket.min_age_days == 361)
            wholly_stale = sum(bucket.size_gib for bucket in buckets if bucket.min_age_days > 365)
            self.assertEqual(crossing.size_gib, 5 * daily)
            self.assertEqual(previous.stale_gib - wholly_stale, 0)  # No 366..390 data at the initial scan.
            result = estimate_stale_exits(self.repository, previous, current)
            self.assertEqual(result.expected_inflow_gib, 7 * daily)
            naive = Fraction(2 * daily) + Fraction(str(crossing.size_gib)) * Fraction(5, 30)
            self.assertNotEqual(result.expected_inflow_gib, naive)


class RepositoryAndReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_contents = generate_contents()

    def setUp(self):
        # Prefer local scratch over NFS, with a portable fallback on other hosts.
        scratch = "/tmp/opencode" if Path("/tmp/opencode").is_dir() else None
        self.temporary = tempfile.TemporaryDirectory(prefix="stale_cleanup_fixture_", dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name)
        for relative, content in self.fixture_contents.items():
            if relative.startswith("candidate/data/"):
                (self.data_dir / Path(relative).name).write_bytes(content)

    def mutate(self, filename, change):
        path = self.data_dir / filename
        with path.open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            columns = reader.fieldnames
            rows = list(reader)
        change(rows)
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)

    def test_default_path_filtering_and_sorting(self):
        repository = SnapshotRepository()
        history = repository.list_snapshots()
        self.assertEqual([(item.disk, item.scanned_at) for item in history],
                         sorted((item.disk, item.scanned_at) for item in history))
        self.assertEqual(len(repository.list_snapshots("/projects/atlas")), 53)
        self.assertEqual({item.disk for item in repository.list_snapshots("/projects/birch")}, {"/projects/birch"})
        self.assertEqual(repository.list_snapshots("unknown"), [])
        self.assertEqual(SnapshotRepository(self.data_dir).list_snapshots(), history)

    def test_report_calls_only_same_disk_adjacent_pairs_and_stub_is_neutral(self):
        repository = SnapshotRepository(self.data_dir)
        calls = []

        def calculator(repo, previous, current):
            self.assertIs(repo, repository)
            calls.append((previous, current))
            return None

        output = render_report(repository, calculator=calculator)
        self.assertEqual(len(calls), 104)
        for previous, current in calls:
            self.assertEqual(previous.disk, current.disk)
            self.assertEqual((current.scanned_at - previous.scanned_at).days, 7)
            self.assertIsNone(calculate_kpi(repository, previous, current))
        self.assertEqual(output.count("N/A (initial scan)"), 2)
        self.assertEqual(output.count("not implemented"), 104)
        self.assertIn("2026-11-22", output)
        self.assertIn("30208.00", output)
        self.assertIn("Stale GiB", output)
        self.assertIn("interpretation and units defined by the candidate", output)
        filtered = render_report(repository, "/projects/birch")
        self.assertIn("Disk /projects/birch", filtered)
        self.assertNotIn("Disk /projects/atlas", filtered)
        self.assertIn("No snapshots found.", render_report(repository, "unknown"))

    def test_report_preserves_diverse_kpi_names_values_and_units(self):
        repository = SnapshotRepository(self.data_dir)
        for result, text in [(KPIResult("Example ratio", 12.5, "%"), "Example ratio: 12.50 %"),
                             (KPIResult("Example throughput", 125.0, "files/week"), "Example throughput: 125.00 files/week"),
                             (KPIResult("Signed example", -1.25, "GiB"), "Signed example: -1.25 GiB")]:
            with self.subTest(result=result):
                output = render_report(repository, "/projects/atlas", calculator=lambda *_: result)
                self.assertEqual(output.count(text), 52)

    def test_inventory_is_lazy_and_not_cached(self):
        repository = SnapshotRepository(self.data_dir)
        disk = "/projects/atlas"
        iterator = repository.iter_files(disk)
        self.assertIs(iter(iterator), iterator)
        self.mutate("files.csv", lambda rows: rows[0].update(size_gib="17"))
        self.assertEqual(next(iterator).size_gib, 17)
        iterator.close()
        self.mutate("files.csv", lambda rows: rows[0].update(size_gib="23"))
        fresh_iterator = repository.iter_files(disk)
        try:
            self.assertEqual(next(fresh_iterator).size_gib, 23)
        finally:
            fresh_iterator.close()

    def test_historical_details_are_unavailable_and_default_is_latest(self):
        repository = SnapshotRepository(self.data_dir)
        for disk in ("/projects/atlas", "/projects/birch"):
            history = repository.list_snapshots(disk)
            latest = history[-1]
            self.assertEqual(list(repository.iter_files(disk)), list(repository.iter_files(disk, latest.scanned_at)))
            for old in history[:-1]:
                with self.subTest(disk=disk, date=old.scanned_at):
                    with self.assertRaisesRegex(FileHistoryUnavailable, "Only latest weekly detail is retained"):
                        repository.iter_files(disk, old.scanned_at)

    def test_default_rejects_mismatched_or_missing_latest_detail(self):
        repository = SnapshotRepository(self.data_dir)
        original = (self.data_dir / "files.csv").read_bytes()
        self.mutate("files.csv", lambda rows: rows[0].update(scanned_at="2026-11-15"))
        with self.assertRaisesRegex(FileHistoryUnavailable, "does not match latest aggregate"):
            list(repository.iter_files("/projects/atlas"))
        (self.data_dir / "files.csv").write_bytes(original)
        self.mutate("files.csv", lambda rows: rows.__setitem__(slice(None), [row for row in rows if row["disk"] != "/projects/atlas"]))
        with self.assertRaisesRegex(FileHistoryUnavailable, "no detail matches"):
            list(repository.iter_files("/projects/atlas"))

    def test_snapshot_validation(self):
        for key, value in [("scanned_at", "not-a-date"), ("total_gib", "-1"),
                           ("stale_gib", "-1"), ("complete", "yes")]:
            with self.subTest(field=key):
                original = (self.data_dir / "scans.csv").read_bytes()
                self.mutate("scans.csv", lambda rows: rows[0].update({key: value}))
                with self.assertRaises(ValueError):
                    SnapshotRepository(self.data_dir)
                (self.data_dir / "scans.csv").write_bytes(original)
        self.mutate("scans.csv", lambda rows: rows.append(dict(rows[0])))
        with self.assertRaisesRegex(ValueError, "Duplicate disk/date"):
            SnapshotRepository(self.data_dir)

    def test_wide_bucket_validation(self):
        for values in [{column: "-1"} for column, _, _ in AGE_COLUMNS] + [
            {"age_0_30_gib": "1"}, {"stale_gib": "1"}, {"total_gib": "1"}]:
            with self.subTest(values=values):
                original = (self.data_dir / "scans.csv").read_bytes()
                self.mutate("scans.csv", lambda rows: rows[0].update(values))
                with self.assertRaises(ValueError):
                    SnapshotRepository(self.data_dir)
                (self.data_dir / "scans.csv").write_bytes(original)

    def test_fractional_gib_and_monthly_age_boundaries(self):
        ages, sizes = [30, 31, 365, 366, 540, 541], [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
        scan = {"disk": "/synthetic/disk", "owner": "team", "scanned_at": "2026-11-22", "complete": "true",
                "total_gib": sum(sizes), "stale_gib": sum(size for age, size in zip(ages, sizes) if age > 365)}
        for column, lower, upper in AGE_COLUMNS:
            scan[column] = sum(size for age, size in zip(ages, sizes) if lower <= age and (upper is None or age <= upper))
        files = [{"disk": scan["disk"], "scanned_at": scan["scanned_at"], "file_id": "f{}".format(index),
                  "path": "/synthetic/f{}".format(index), "size_gib": size, "days_since_last_access": age}
                 for index, (age, size) in enumerate(zip(ages, sizes))]
        self.mutate("scans.csv", lambda rows: rows.__setitem__(slice(None), [scan]))
        self.mutate("files.csv", lambda rows: rows.__setitem__(slice(None), files))
        repository = SnapshotRepository(self.data_dir)
        snapshot = repository.list_snapshots()[0]
        buckets = {(bucket.min_age_days, bucket.max_age_days): bucket.size_gib
                   for bucket in repository.get_age_buckets(snapshot.disk, snapshot.scanned_at)}
        self.assertAlmostEqual(snapshot.total_gib, 2.1)
        self.assertAlmostEqual(snapshot.stale_gib, 1.5)
        self.assertEqual(buckets[(0, 30)], 0.1)
        self.assertEqual(buckets[(31, 60)], 0.2)
        self.assertAlmostEqual(buckets[(361, 390)], 0.7)
        self.assertEqual(buckets[(511, 540)], 0.5)
        self.assertEqual(buckets[(541, None)], 0.6)
        wholly = sum(size for (lower, _), size in buckets.items() if lower > 365)
        self.assertNotAlmostEqual(snapshot.stale_gib, wholly)
        self.assertNotAlmostEqual(snapshot.stale_gib, wholly + buckets[(361, 390)])
        fetched = list(repository.iter_files(snapshot.disk))
        self.assertEqual([item.days_since_last_access for item in fetched], ages)
        self.assertEqual([item.size_gib for item in fetched], sizes)
        self.assertFalse(hasattr(snapshot, "stale_bytes"))
        self.assertFalse(hasattr(fetched[0], "last_accessed"))

    def test_stale_gib_must_be_within_crossing_bin_lower_and_upper_bounds(self):
        original = (self.data_dir / "scans.csv").read_bytes()
        for stale, valid in [(10, True), (20, True), (40, True), (9.5, False), (40.5, False)]:
            with self.subTest(stale=stale):
                (self.data_dir / "scans.csv").write_bytes(original)
                values = {column: "0" for column, _, _ in AGE_COLUMNS}
                values.update(age_331_360_gib="40", age_361_390_gib="30", age_391_420_gib="10",
                              total_gib="80", stale_gib=str(stale))
                self.mutate("scans.csv", lambda rows: rows[0].update(values))
                if valid:
                    self.assertEqual(SnapshotRepository(self.data_dir).list_snapshots()[0].stale_gib, stale)
                else:
                    with self.assertRaisesRegex(ValueError, "outside monthly-bin bounds"):
                        SnapshotRepository(self.data_dir)

    def test_metric_gib_must_be_finite_and_nonnegative(self):
        original = (self.data_dir / "scans.csv").read_bytes()
        for field in ["total_gib", "stale_gib"] + [column for column, _, _ in AGE_COLUMNS]:
            for invalid in ("nan", "inf", "-inf", "-0.1"):
                with self.subTest(field=field, invalid=invalid):
                    (self.data_dir / "scans.csv").write_bytes(original)
                    self.mutate("scans.csv", lambda rows: rows[0].update({field: invalid}))
                    with self.assertRaisesRegex(ValueError, "finite nonnegative GiB"):
                        SnapshotRepository(self.data_dir)
        (self.data_dir / "scans.csv").write_bytes(original)
        repository = SnapshotRepository(self.data_dir)
        original_files = (self.data_dir / "files.csv").read_bytes()
        for invalid in ("nan", "inf", "-inf", "-0.1"):
            (self.data_dir / "files.csv").write_bytes(original_files)
            self.mutate("files.csv", lambda rows: rows[0].update(size_gib=invalid))
            with self.assertRaisesRegex(ValueError, "finite nonnegative GiB"):
                list(repository.iter_files("/projects/atlas"))

    def test_file_validation_and_unknown_snapshot_requests(self):
        repository = SnapshotRepository(self.data_dir)
        disk = "/projects/atlas"
        for values in [{"disk": "/projects/unknown"}, {"size_gib": "-1"}, {"days_since_last_access": "-1"},
                       {"days_since_last_access": "1.5"}, {"days_since_last_access": "nan"},
                       {"file_id": ""}, {"path": "relative/path"}]:
            with self.subTest(values=values):
                original = (self.data_dir / "files.csv").read_bytes()
                self.mutate("files.csv", lambda rows: rows[0].update(values))
                with self.assertRaises(ValueError):
                    list(repository.iter_files(disk))
                (self.data_dir / "files.csv").write_bytes(original)
        self.mutate("files.csv", lambda rows: rows.insert(1, dict(rows[0])))
        with self.assertRaisesRegex(ValueError, "Duplicate file ID"):
            list(repository.iter_files(disk))
        with self.assertRaisesRegex(ValueError, "Unknown disk/date"):
            repository.get_age_buckets("/projects/unknown", START)
        with self.assertRaisesRegex(ValueError, "Unknown disk"):
            list(repository.iter_files("/projects/unknown"))

    def test_schema_validation(self):
        (self.data_dir / "scans.csv").write_text("wrong,columns\na,b\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "expected columns"):
            SnapshotRepository(self.data_dir)


class MemoryRepository:
    def __init__(self, files=None, buckets=None):
        self.files = files or {}
        self.buckets = buckets or {}

    def iter_files(self, disk, scanned_at=None):
        return iter(self.files[(disk, scanned_at)])

    def get_age_buckets(self, disk, scanned_at):
        return self.buckets[(disk, scanned_at)]


def record(identity, size, scanned_at, age):
    return FileRecord("/synthetic/disk", scanned_at, identity, "/synthetic/{}.dat".format(identity),
                      size, age)


def snapshot_from_files(scanned_at, files):
    return Snapshot("/synthetic/disk", "team", scanned_at, True,
                    sum(item.size_gib for item in files),
                    sum(item.size_gib for item in files if item.days_since_last_access > 365))


class ReferenceEdgeTests(unittest.TestCase):
    def test_exact_365_366_boundary(self):
        start, end = date(2026, 6, 1), date(2026, 6, 2)
        before = [record("a{}".format(age), 10, start, age) for age in range(361, 366)] + [record("z", 30, start, 366)]
        after = [replace(item, scanned_at=end, days_since_last_access=item.days_since_last_access + 1) for item in before]
        previous, current = snapshot_from_files(start, before), snapshot_from_files(end, after)
        repository = MemoryRepository({(previous.disk, start): before, (current.disk, end): after}, {(previous.disk, start): [
            AgeBucket(361, 390, 80)]})
        self.assertEqual(previous.stale_gib, 30)
        self.assertEqual(current.stale_gib, 40)
        aggregate = estimate_stale_exits(repository, previous, current)
        observed = compare_files(repository, previous, current)
        self.assertEqual(aggregate.expected_inflow_gib, 10)
        self.assertEqual(aggregate.estimated_stale_exits_gib, 0)
        self.assertEqual(observed.aged_in_gib, 10)
        self.assertEqual(observed.missing_previously_stale_gib, 0)
        self.assertEqual(observed.conservation_residual_gib, 0)

    def test_irregular_interval_partial_bucket_uniform_caveat(self):
        previous = Snapshot("/synthetic/disk", "team", date(2026, 6, 1), True, 450, 100)
        current = replace(previous, scanned_at=date(2026, 6, 18), total_gib=450, stale_gib=200)
        repository = MemoryRepository(buckets={(previous.disk, previous.scanned_at): [AgeBucket(331, 360, 300),
                                                        AgeBucket(361, 390, 50), AgeBucket(391, 420, 100)]})
        result = estimate_stale_exits(repository, previous, current)
        self.assertEqual(result.expected_inflow_gib, Fraction(170))
        self.assertEqual(result.estimated_stale_exits_gib, Fraction(70))
        self.assertTrue(any("Partial overlap" in caveat and "uniform" in caveat for caveat in result.caveats))

    def test_fractional_estimate_is_not_rounded_or_clamped(self):
        previous = Snapshot("/synthetic/disk", "team", date(2026, 6, 1), True, 101, 100)
        current = replace(previous, scanned_at=date(2026, 6, 8), total_gib=102, stale_gib=101)
        repository = MemoryRepository(buckets={(previous.disk, previous.scanned_at): [AgeBucket(331, 360, 1),
                                                     AgeBucket(361, 390, 0), AgeBucket(391, 420, 100)]})
        result = estimate_stale_exits(repository, previous, current)
        self.assertEqual(result.expected_inflow_gib, Fraction(1, 15))
        self.assertEqual(result.estimated_stale_exits_gib, Fraction(-14, 15))

    def test_mixed_crossing_bin_uses_inferred_active_portion_not_whole_bin(self):
        previous = Snapshot("/synthetic/disk", "team", date(2026, 6, 1), True, 700.0, 350.0)
        current = replace(previous, scanned_at=date(2026, 6, 8), stale_gib=400.0)
        repository = MemoryRepository(buckets={(previous.disk, previous.scanned_at): [
            AgeBucket(331, 360, 300.0), AgeBucket(361, 390, 200.0), AgeBucket(391, 420, 200.0)]})
        result = estimate_stale_exits(repository, previous, current)
        # Crossing bin: 150 stale GiB inferred, leaving 50 active GiB at ages 361..365.
        self.assertEqual(result.expected_inflow_gib, 20 + 50)
        self.assertEqual(result.estimated_stale_exits_gib, 20)
        self.assertNotEqual(result.expected_inflow_gib, 20 + Fraction(200 * 5, 30))
        self.assertTrue(any("150 GiB stale, 50 GiB active" in caveat for caveat in result.caveats))
        self.assertTrue(any("uniform GiB per day within its active ages 361..365" in caveat for caveat in result.caveats))

    def test_fractional_gib_uses_decimal_fraction_arithmetic(self):
        previous = Snapshot("/synthetic/disk", "team", date(2026, 6, 1), True, 0.3, 0.1)
        current = replace(previous, scanned_at=date(2026, 6, 2), stale_gib=0.15)
        repository = MemoryRepository(buckets={(previous.disk, previous.scanned_at): [AgeBucket(361, 390, 0.3)]})
        result = estimate_stale_exits(repository, previous, current)
        self.assertEqual(result.expected_inflow_gib, Fraction(1, 25))
        self.assertEqual(result.estimated_stale_exits_gib, Fraction(-1, 100))

    def test_incompatible_comparisons_rejected_by_both_methods(self):
        previous = Snapshot("/synthetic/disk", "team", date(2026, 6, 1), True, 0, 0)
        current = replace(previous, scanned_at=date(2026, 7, 1))
        invalid = [(previous, replace(current, disk="/synthetic/other")),
                   (replace(previous, complete=False), current),
                   (previous, replace(current, complete=False)),
                   (previous, replace(current, scanned_at=previous.scanned_at)), (current, previous)]
        for before, after in invalid:
            for method in (estimate_stale_exits, compare_files):
                with self.subTest(method=method.__name__, before=before, after=after):
                    with self.assertRaises(ValueError):
                        method(MemoryRepository(), before, after)

    def test_missing_reaccess_first_seen_age_in_and_size_delta_are_separate(self):
        start, end = date(2026, 6, 1), date(2026, 7, 1)
        before = [record("a", 10, start, 400), record("b", 20, start, 400),
                  record("c", 30, start, 350), record("d", 40, start, 400)]
        after = [record("b", 25, end, 0), record("c", 35, end, 380),
                 record("d", 45, end, 430), record("e", 50, end, 500)]
        # Match identity despite a path change; do not classify renamed files as missing.
        after[2] = replace(after[2], path="/synthetic/renamed.dat")
        previous, current = snapshot_from_files(start, before), snapshot_from_files(end, after)
        result = compare_files(MemoryRepository({(previous.disk, start): before, (current.disk, end): after}), previous, current)
        self.assertEqual(result.missing_previously_stale_gib, 10)
        self.assertEqual(result.reaccessed_previously_stale_gib, 20)
        self.assertEqual(result.aged_in_gib, 35)
        self.assertEqual(result.first_seen_stale_gib, 50)
        self.assertEqual(result.retained_stale_size_delta_gib, 5)
        self.assertEqual(result.conservation_residual_gib, 0)

    def test_reaccess_alone_is_not_missing(self):
        start, end = date(2026, 6, 1), date(2026, 7, 1)
        before = [record("a", 10, start, 400)]
        after = [record("a", 10, end, 0)]
        previous, current = snapshot_from_files(start, before), snapshot_from_files(end, after)
        result = compare_files(MemoryRepository({(previous.disk, start): before, (current.disk, end): after}), previous, current)
        self.assertEqual(result.missing_previously_stale_gib, 0)
        self.assertEqual(result.reaccessed_previously_stale_gib, 10)
        self.assertEqual(result.conservation_residual_gib, 0)
        self.assertFalse(hasattr(result, "deleted_gib"))

    def test_file_comparison_refuses_inconsistent_inventory_totals(self):
        previous = Snapshot("/synthetic/disk", "team", date(2026, 6, 1), True, 10, 10)
        current = replace(previous, scanned_at=date(2026, 7, 1))
        with self.assertRaisesRegex(ValueError, "Inventory does not match"):
            compare_files(MemoryRepository({(previous.disk, previous.scanned_at): [],
                                            (current.disk, current.scanned_at): []}), previous, current)


if __name__ == "__main__":
    unittest.main()
