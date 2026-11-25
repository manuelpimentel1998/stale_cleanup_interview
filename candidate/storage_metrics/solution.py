"""Neutral extension point for the candidate's chosen KPI.

The starter leaves the calculation unimplemented. The candidate defines the
metric's name, meaning, and display units.
"""

from typing import Optional

from .models import KPIResult, Snapshot
from .repository import SnapshotRepository


def calculate_kpi(
    repository: SnapshotRepository, previous: Snapshot, current: Snapshot
) -> Optional[KPIResult]:
    """Return the candidate's chosen KPI, or None if unimplemented.

    Args:
        repository: Data access supplied to the calculation.
        previous: Earlier aggregate scan in a consecutive same-disk pair.
        current: Later aggregate scan in that pair.

    Returns:
        KPIResult with a candidate-defined name, numeric value, and display
        unit, or None. This starter implementation always returns None.
    """
    return None
