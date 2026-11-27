"""Which scans' liver mean HU stands out from the other scans of the same kind.

Scans are compared only with scans of the same contrast phase (non-contrast, contrast-enhanced,
uncertain), because contrast raises liver HU by tens of HU. Within a group of at least
MIN_SCANS scans, a value is an outlier when it lies more than 1.5 x the spread of the middle
half of the scans (the interquartile range, IQR) below the lower quartile or above the upper
quartile (Tukey's rule). The spread is taken as at least MIN_SPREAD_HU so that a group of
near-identical values does not flag tiny differences.
"""
from __future__ import annotations

import numpy as np

MIN_SCANS = 5
MIN_SPREAD_HU = 5.0
GROUP_ORDER = ("non-contrast", "contrast-enhanced", "uncertain")
GROUP_TITLES = {"non-contrast": "Non-contrast scans", "contrast-enhanced": "Contrast-enhanced scans",
                "uncertain": "Contrast phase uncertain"}


def fences(values) -> dict:
    v = np.asarray([x for x in values if x is not None], float)
    q1, med, q3 = (float(x) for x in np.percentile(v, [25, 50, 75]))
    spread = max(q3 - q1, MIN_SPREAD_HU)
    return {"q1": round(q1, 1), "median": round(med, 1), "q3": round(q3, 1),
            "low": round(q1 - 1.5 * spread, 1), "high": round(q3 + 1.5 * spread, 1)}


def group_of(phase) -> str:
    return phase if phase in GROUP_ORDER else "uncertain"


def mark_outliers(items: list[dict], value_key: str, phase_key: str, out_key: str = "outlier") -> dict:
    """Set item[out_key] to 'high', 'low' or '' for every item; return per-group summaries
    {group: {"n": .., "fences": {...} or None, "title": ..}} in a fixed group order."""
    groups: dict[str, list[dict]] = {}
    for it in items:
        groups.setdefault(group_of(it.get(phase_key)), []).append(it)
    summary = {}
    for g in GROUP_ORDER:
        its = groups.get(g)
        if not its:
            continue
        vals = [it.get(value_key) for it in its if it.get(value_key) is not None]
        f = fences(vals) if len(vals) >= MIN_SCANS else None
        for it in its:
            v = it.get(value_key)
            it[out_key] = "" if f is None or v is None else ("high" if v > f["high"] else "low" if v < f["low"] else "")
        summary[g] = {"n": len(vals), "fences": f, "title": GROUP_TITLES[g]}
    return summary
