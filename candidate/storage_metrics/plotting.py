"""Plot date-aligned metric series with Matplotlib without changing their units.

The helper supports a noninteractive Figure, optional image output, and
opt-in interactive display. Missing values remain gaps, not interpolated data.
"""

import argparse
from datetime import date, datetime
from math import isnan
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Union
import matplotlib.dates as mdates

from .models import KPIResult, Snapshot
from .repository import SnapshotRepository

# Okabe-Ito palette: distinguishable for most forms of colour blindness.
_COLORS = ("#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9", "#000000")
_STYLES = ("-", "--", "-.", ":")


def plot_metrics(dates, series, *, title="Storage metrics over time",
                  ylabel="GiB", output_path=None, show=False):
    """Plot one or more metrics against dates and return the Figure.

    Args:
        dates: Nonempty sequence of datetime.date or datetime values.
        series: Nonempty mapping of legend labels to numeric-value sequences.
            Each sequence must have exactly the same length as dates. None or
            a floating-point NaN leaves a gap; values are never interpolated.
        title: Chart title.
        ylabel: Shared y-axis label, defaulting to GiB. Values are not converted;
            use separate calls for incompatible units such as GiB and GiB/week.
        output_path: Optional destination for saving the Figure. Parent
            directories are created; raster output uses 150 dpi.
        show: If True, create a pyplot Figure and call pyplot.show(). Otherwise
            create a Figure directly without requesting interactive display.

    Returns:
        The matplotlib.figure.Figure, whether or not it is saved or displayed.

    Raises:
        ValueError: Dates or series are empty, dates have invalid types, series
            lengths differ, or a value cannot be converted to a number.
        TypeError: An input is not iterable or a value cannot be converted.
        OSError: An output directory or file cannot be written.
    """
    dates = list(dates)
    if not dates:
        raise ValueError("dates must not be empty")
    if not series:
        raise ValueError("series must not be empty")
    for d in dates:
        if not isinstance(d, (date, datetime)):
            raise ValueError(f"dates must be date or datetime, got {d!r}")
    cleaned = {}
    for label, values in series.items():
        values = list(values)
        if len(values) != len(dates):
            raise ValueError(
                f"series {label!r} has {len(values)} values but there are "
                f"{len(dates)} dates")
        cleaned[label] = [
            None if v is None or (isinstance(v, float) and isnan(v)) else float(v)
            for v in values
        ]

    if show:
        from matplotlib import pyplot
        fig = pyplot.figure(figsize=(10, 5.2), layout="constrained")
    else:
        from matplotlib.figure import Figure
        fig = Figure(figsize=(10, 5.2), layout="constrained")
    ax = fig.add_subplot()

    for i, (label, values) in enumerate(cleaned.items()):
        nan = float("nan")
        ys = [nan if v is None else v for v in values]
        color = _COLORS[i % len(_COLORS)]
        ax.plot(dates, ys, label=label, color=color, linewidth=2.2,
                linestyle=_STYLES[(i // len(_COLORS)) % len(_STYLES)])
        # Points cut off from their neighbours by gaps would be invisible
        # as lines, so mark them.
        lone = [
            j for j, v in enumerate(values)
            if v is not None
            and (j == 0 or values[j - 1] is None)
            and (j == len(values) - 1 or values[j + 1] is None)
        ]
        if lone:
            ax.plot([dates[j] for j in lone], [values[j] for j in lone],
                    linestyle="none", marker="o", markersize=5, color=color)

    ax.set_title(title, loc="left", fontsize=14, fontweight="bold", pad=12)
    ax.set_ylabel(ylabel, fontsize=11)
    ax.yaxis.grid(True, color="#d9d9d9", linewidth=0.7)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#808080")
    ax.tick_params(colors="#333333", labelsize=10)
    ax.margins(x=0.02)

    locator = mdates.AutoDateLocator(minticks=4, maxticks=8)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    ax.legend(frameon=False, fontsize=10, loc="best")

    if output_path is not None:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(path, dpi=150)
    if show:
        from matplotlib import pyplot
        pyplot.show()
    return fig


def save_graphs(
    repository: SnapshotRepository,
    output_dir: Union[str, Path],
    disk: Optional[str] = None,
    calculator: Optional[Callable[[SnapshotRepository, Snapshot, Snapshot], Optional[KPIResult]]] = None,
) -> List[Path]:
    """Save stale history and available candidate KPIs as headless PNG graphs.

    Args:
        repository: Source of aggregate scan history.
        output_dir: Destination directory, created if needed. Only fixed names
            stale.png and kpi-1.png, kpi-2.png, ... are used.
        disk: Exact readable disk path to select, or None for every disk.
        calculator: Optional candidate KPI callback for consecutive same-disk
            scans. None resolves the candidate's calculate_kpi function.

    Returns:
        Absolute paths generated by this call, starting with stale.png in GiB.
        Candidate KPIs are grouped by identical name and unit, with a separate
        image and axis for each group. Disk series align on the union of scan
        dates; initial comparisons and missing results remain None gaps. KPI
        values are not converted. Existing unrelated output files are retained
        and are never included in the returned list.

    Raises:
        ValueError: No aggregate history matches the selection, or graph data
            or a candidate calculation is invalid.
        OSError: An output directory or PNG cannot be written.
    """
    if calculator is None:
        from .solution import calculate_kpi
        calculator = calculate_kpi
    snapshots = repository.list_snapshots(disk)
    if not snapshots:
        raise ValueError("No snapshots found" + (" for disk " + disk if disk is not None else "") + ".")
    histories: Dict[str, List[Snapshot]] = {}
    for snapshot in sorted(snapshots, key=lambda item: (item.disk, item.scanned_at)):
        histories.setdefault(snapshot.disk, []).append(snapshot)
    dates = sorted({snapshot.scanned_at for snapshot in snapshots})
    stale_series = {}
    groups: Dict[Tuple[str, str], Dict[str, Dict[date, float]]] = {}
    for disk_name, history in histories.items():
        stale_values = {snapshot.scanned_at: snapshot.stale_gib for snapshot in history}
        stale_series[disk_name] = [stale_values.get(scanned_at) for scanned_at in dates]
        for previous, current in zip(history, history[1:]):
            result = calculator(repository, previous, current)
            if result is not None:
                values = groups.setdefault((result.name, result.unit), {}).setdefault(disk_name, {})
                values[current.scanned_at] = result.value
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    generated = [destination / "stale.png"]
    plot_metrics(dates, stale_series, title="Stale history", ylabel="GiB", output_path=generated[0], show=False)
    for index, ((name, unit), disk_values) in enumerate(sorted(groups.items()), start=1):
        series = {disk_name: [values.get(scanned_at) for scanned_at in dates]
                  for disk_name, values in disk_values.items()}
        path = destination / "kpi-{}.png".format(index)
        plot_metrics(dates, series, title=name, ylabel=unit, output_path=path, show=False)
        generated.append(path)
    return generated


def main() -> None:
    """Save graph PNGs from the candidate repository without opening windows.

    --output-dir defaults to graphs; --disk selects a readable disk path and
    --data-dir selects an alternate CSV directory. Print only paths generated
    by this run, and explicitly report when no candidate KPI results exist.

    Raises:
        SystemExit: Help is requested, arguments are invalid, or data/selection
            or output errors prevent graph generation.
    """
    parser = argparse.ArgumentParser(description="Save stale history and available candidate KPI graphs as PNG files.")
    parser.add_argument("--output-dir", default="graphs", help="Destination directory (default: graphs)")
    parser.add_argument("--disk", help="Select an exact readable disk path")
    parser.add_argument("--data-dir", help="Directory containing the candidate CSV data")
    args = parser.parse_args()
    try:
        generated = save_graphs(SnapshotRepository(args.data_dir), args.output_dir, args.disk)
    except (ValueError, OSError) as error:
        parser.exit(2, "error: {}\n".format(error))
    for path in generated:
        print("Saved " + str(path))
    if len(generated) == 1:
        print("No candidate KPI results were available; saved stale history only.")


if __name__ == "__main__":
    main()
