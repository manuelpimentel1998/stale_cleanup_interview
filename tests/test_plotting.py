"""Focused checks for the plotting helper (headless, scratch output only)."""

import math
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

os.environ.setdefault("MPLBACKEND", "Agg")

from candidate.storage_metrics.plotting import plot_metrics

DATES = [date(2025, 11, 23) + timedelta(weeks=i) for i in range(53)]


class PlotMetricsTests(unittest.TestCase):
    def test_renders_png_with_labels_and_alignment(self):
        stale = [10240 + i * 384 for i in range(53)]
        flat = [4096.0] * 53
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            out = Path(tmp) / "sub" / "chart.png"
            fig = plot_metrics(DATES, {"atlas": stale, "birch": flat},
                               title="Stale data", ylabel="GiB", output_path=out)
            self.assertGreater(out.stat().st_size, 1000)
            self.assertEqual(out.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        ax = fig.axes[0]
        self.assertEqual(ax.get_title(loc="left"), "Stale data")
        self.assertEqual(ax.get_ylabel(), "GiB")
        self.assertEqual([t.get_text() for t in ax.get_legend().get_texts()],
                         ["atlas", "birch"])
        lines = ax.get_lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(list(lines[0].get_ydata()), stale)
        self.assertEqual(list(lines[1].get_ydata()), flat)
        self.assertEqual(len(lines[0].get_xdata()), 53)
        self.assertNotEqual(lines[0].get_color(), lines[1].get_color())

    def test_gaps_are_not_filled(self):
        values = [1, 2, None, None, 5, 6, None, 8]
        fig = plot_metrics(DATES[:8], {"kpi": values}, ylabel="%")
        ys = list(fig.axes[0].get_lines()[0].get_ydata())
        for got, want in zip(ys, values):
            if want is None:
                self.assertTrue(math.isnan(got))
            else:
                self.assertEqual(got, want)
        # the isolated point at index 7 gets a marker
        self.assertEqual(list(fig.axes[0].get_lines()[1].get_ydata()), [8])

    def test_no_output_returns_figure_without_file(self):
        fig = plot_metrics(DATES[:2], {"a": [1, 2]})
        self.assertEqual(fig.axes[0].get_ylabel(), "GiB")

    def test_invalid_input(self):
        with self.assertRaisesRegex(ValueError, "'a'.*2 values.*3 dates"):
            plot_metrics(DATES[:3], {"a": [1, 2]})
        with self.assertRaisesRegex(ValueError, "dates"):
            plot_metrics([], {"a": []})
        with self.assertRaisesRegex(ValueError, "series"):
            plot_metrics(DATES[:2], {})


if __name__ == "__main__":
    unittest.main()
