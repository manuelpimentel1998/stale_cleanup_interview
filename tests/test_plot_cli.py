"""Focused plotting CLI checks using only scratch images and candidate data."""

import ast
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("MPLBACKEND", "Agg")

from candidate.storage_metrics.models import KPIResult, Snapshot
from candidate.storage_metrics.plotting import plot_metrics, save_graphs
from candidate.storage_metrics.repository import SnapshotRepository

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = ROOT / "candidate"


class MemoryRepository:
    """Supply deliberately unsorted aggregate histories without private data."""

    def __init__(self, snapshots):
        self.snapshots = snapshots

    def list_snapshots(self, disk=None):
        return [snapshot for snapshot in self.snapshots if disk is None or snapshot.disk == disk]


class PlotCLITests(unittest.TestCase):
    def setUp(self):
        scratch = "/tmp/opencode" if Path("/tmp/opencode").is_dir() else None
        self.temporary = tempfile.TemporaryDirectory(prefix="stale_plot_cli_", dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.scratch = Path(self.temporary.name)

    def run_cli(self, *arguments, candidate=CANDIDATE):
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", MPLBACKEND="Agg",
                           MPLCONFIGDIR=str(self.scratch / "mpl"), TMPDIR=str(self.scratch))
        environment.pop("PYTHONPATH", None)
        return subprocess.run([sys.executable, "-m", "storage_metrics.plotting", *map(str, arguments)],
                              cwd=candidate, env=environment, capture_output=True, text=True, timeout=90)

    def assert_png(self, path):
        self.assertGreater(path.stat().st_size, 1000)
        self.assertEqual(path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_cli_neutral_stub_saves_only_stale_and_reports_this_run_paths(self):
        output = self.scratch / "custom output"
        output.mkdir()
        leftover = output / "kpi-9.png"
        leftover.write_bytes(b"preexisting unrelated file")
        result = self.run_cli("--output-dir", output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_png(output / "stale.png")
        self.assertEqual(leftover.read_bytes(), b"preexisting unrelated file")
        self.assertIn("Saved " + str(output / "stale.png"), result.stdout)
        self.assertIn("No candidate KPI results were available", result.stdout)
        self.assertNotIn(str(leftover), result.stdout)
        self.assertEqual({path.name for path in output.iterdir()}, {"stale.png", "kpi-9.png"})

    def test_cli_disk_selection_and_alternate_data_dir(self):
        data = self.scratch / "alternate data"
        shutil.copytree(CANDIDATE / "data", data)
        output = self.scratch / "selected"
        result = self.run_cli("--disk", "/projects/atlas", "--data-dir", data, "--output-dir", output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_png(output / "stale.png")
        self.assertEqual({path.name for path in output.iterdir()}, {"stale.png"})

    def test_cli_unknown_disk_is_a_clear_nonzero_error(self):
        output = self.scratch / "unknown"
        result = self.run_cli("--disk", "/projects/unknown", "--output-dir", output)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No snapshots found for disk /projects/unknown", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(output.exists())

    def test_cli_default_output_and_implemented_hook_in_candidate_only_copy(self):
        candidate = self.scratch / "candidate"
        shutil.copytree(CANDIDATE / "storage_metrics", candidate / "storage_metrics",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(CANDIDATE / "data", candidate / "data")
        # Only the scratch copy gets a synthetic constant hook, never the live starter.
        (candidate / "storage_metrics/solution.py").write_text(
            'from .models import KPIResult\n'
            'def calculate_kpi(repository, previous, current):\n'
            '    return KPIResult("Synthetic example", 12.5, "%")\n', encoding="utf-8")
        result = self.run_cli(candidate=candidate)
        self.assertEqual(result.returncode, 0, result.stderr)
        for filename in ("stale.png", "kpi-1.png"):
            self.assert_png(candidate / "graphs" / filename)
            self.assertIn("Saved " + str(candidate / "graphs" / filename), result.stdout)
        self.assertNotIn("No candidate KPI results", result.stdout)
        self.assertFalse((self.scratch / "interviewer").exists())

    def test_empty_or_unknown_selection_is_rejected_by_helper(self):
        with self.assertRaisesRegex(ValueError, "No snapshots found"):
            save_graphs(MemoryRepository([]), self.scratch / "empty")
        with self.assertRaisesRegex(ValueError, "No snapshots found for disk"):
            save_graphs(SnapshotRepository(), self.scratch / "unknown", disk="/projects/unknown")

    def test_grouping_alignment_values_gaps_and_same_disk_pairs(self):
        days = [date(2026, 1, 1) + timedelta(days=7 * index) for index in range(5)]
        atlas = [Snapshot("/projects/atlas", "team-a", days[index], True, 100.0, 10.0 + index)
                 for index in (0, 1, 2, 4)]
        birch = [Snapshot("/projects/birch", "team-b", days[index], True, 100.0, 20.0 + index)
                 for index in (0, 2, 3, 4)]
        repository = MemoryRepository(list(reversed(birch + atlas)))
        calls = []
        answers = {
            ("/projects/atlas", days[1]): KPIResult("Example", -2.5, "%"),
            ("/projects/atlas", days[2]): None,
            ("/projects/atlas", days[4]): KPIResult("Example", 0.0, "%"),
            ("/projects/birch", days[2]): KPIResult("Example", 125.0, "files/week"),
            ("/projects/birch", days[3]): KPIResult("Other ../ name", 7.0, "%"),
            ("/projects/birch", days[4]): KPIResult("Example", 50.0, "%"),
        }

        def calculator(repo, previous, current):
            self.assertIs(repo, repository)
            calls.append((previous, current))
            return answers[(current.disk, current.scanned_at)]

        with patch("candidate.storage_metrics.plotting.plot_metrics", wraps=plot_metrics) as plot:
            generated = save_graphs(repository, self.scratch / "grouped", calculator=calculator)
        self.assertEqual(len(calls), 6)
        expected = [(previous, current) for history in (atlas, birch)
                    for previous, current in zip(history, history[1:])]
        self.assertEqual(calls, expected)
        self.assertTrue(all(previous.disk == current.disk and previous.scanned_at < current.scanned_at
                            for previous, current in calls))
        self.assertEqual([path.name for path in generated], ["stale.png", "kpi-1.png", "kpi-2.png", "kpi-3.png"])
        for path in generated:
            self.assert_png(path)
        self.assertEqual(plot.call_count, 4)
        stale = plot.call_args_list[0]
        self.assertEqual(stale.args[0], days)
        self.assertEqual(stale.args[1], {"/projects/atlas": [10.0, 11.0, 12.0, None, 14.0],
                                       "/projects/birch": [20.0, None, 22.0, 23.0, 24.0]})
        self.assertEqual(stale.kwargs["ylabel"], "GiB")
        grouped = {(call.kwargs["title"], call.kwargs["ylabel"]): call for call in plot.call_args_list[1:]}
        self.assertEqual(set(grouped), {("Example", "%"), ("Example", "files/week"), ("Other ../ name", "%")})
        self.assertEqual(grouped[("Example", "%")].args[1],
                         {"/projects/atlas": [None, -2.5, None, None, 0.0],
                          "/projects/birch": [None, None, None, None, 50.0]})
        self.assertEqual(grouped[("Example", "files/week")].args[1], {"/projects/birch": [None, None, 125.0, None, None]})
        self.assertEqual(grouped[("Other ../ name", "%")].args[1], {"/projects/birch": [None, None, None, 7.0, None]})
        for call in plot.call_args_list:
            self.assertEqual(call.args[0], days)
            self.assertIs(call.kwargs["show"], False)

    def test_helper_disk_filter_selects_only_matching_history(self):
        repository = SnapshotRepository()
        with patch("candidate.storage_metrics.plotting.plot_metrics", wraps=plot_metrics) as plot:
            save_graphs(repository, self.scratch / "filtered", disk="/projects/birch")
        self.assertEqual(set(plot.call_args.args[1]), {"/projects/birch"})

    def test_designer_approved_plot_metrics_ast_is_unchanged(self):
        # Source digest recorded after matching the parent's AST baseline on this runtime.
        expected = "bbeeabaddd5cb152d4d3ad75f410c5199ff9906a0c4b6bcffc08fec98a18a011"
        source = (CANDIDATE / "storage_metrics/plotting.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "plot_metrics")
        segment = ast.get_source_segment(source, function)
        self.assertIsNotNone(segment)
        actual = hashlib.sha256(segment.encode("utf-8")).hexdigest()
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
