"""Percentage shares that add up to 100.

Rounding each share independently does not work. Three equal parts round to
33% each and the card reads 99%; other splits overshoot and read 101%. Both
look like an arithmetic bug to anyone reading the report, because they are.

The largest remainder method (Hare-Niemeyer) fixes the total by construction:
give everyone their whole-number floor, then hand the leftover points out one
at a time to whoever was cut by the most.
"""

from typing import List, Sequence


def largest_remainder(values: Sequence[float], total_pct: int = 100) -> List[int]:
    """Whole-number percentages of `values` that sum to exactly `total_pct`.

    Returns zeros when the values sum to zero — a report of nothing should
    show nothing, not a spurious 100% against the first row.

    Ties go to the earlier index, so a breakdown already sorted by size hands
    its spare point to the larger share, and the same input always gives the
    same output.
    """
    vals = [float(v or 0) for v in values]
    if not vals:
        return []

    total = sum(vals)
    if total <= 0:
        return [0] * len(vals)

    exact  = [v / total * total_pct for v in vals]
    floors = [int(e) for e in exact]           # int() == floor for non-negatives
    spare  = total_pct - sum(floors)

    if spare > 0:
        # Biggest fractional part first; index second so ties are stable.
        order = sorted(
            range(len(vals)),
            key=lambda i: (-(exact[i] - floors[i]), i),
        )
        for i in order[:spare]:
            floors[i] += 1

    return floors


def with_pct(rows: List[dict], value_key: str = "count",
             pct_key: str = "pct", total_pct: int = 100) -> List[dict]:
    """Add a percentage to each row in place, summing to total_pct.

    Convenience for the report breakdowns, which are all lists of dicts
    carrying a count or an amount.
    """
    shares = largest_remainder([r.get(value_key, 0) for r in rows], total_pct)
    for row, pct in zip(rows, shares):
        row[pct_key] = pct
    return rows
