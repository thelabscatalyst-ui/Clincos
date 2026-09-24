"""
test_percentages.py — report shares that add up to 100.

Rounding each share on its own is what produced "33% / 33% / 33%" under a
heading claiming three known sources. These pin the property that was
missing: the column totals 100, whatever the split.
"""
import pytest

from services.percentages import largest_remainder, with_pct


class TestLargestRemainder:

    @pytest.mark.parametrize("values", [
        [1, 1, 1],                 # the reported case: naive rounding gives 99
        [1, 1, 1, 1, 1, 1],        # gives 102 naively
        [1, 1, 1, 1, 1, 1, 1],     # 7 equal parts
        [2, 1, 1],
        [7, 2, 1],
        [1, 2, 3, 4, 5, 6, 7],
        [5, 5],
        [1],
        [100, 1],
        [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
        [0.5, 0.25, 0.25],
        [1000000, 1, 1],
    ])
    def test_shares_always_total_100(self, values):
        assert sum(largest_remainder(values)) == 100

    def test_naive_rounding_would_have_been_wrong(self, values=[1, 1, 1]):
        """Guards the reason this module exists."""
        naive = [round(v / sum(values) * 100) for v in values]
        assert sum(naive) == 99
        assert sum(largest_remainder(values)) == 100

    def test_all_zeros_stays_zero(self):
        """A report of nothing shows nothing, not 100% against the first row."""
        assert largest_remainder([0, 0, 0]) == [0, 0, 0]
        assert largest_remainder([0]) == [0]

    def test_empty_input(self):
        assert largest_remainder([]) == []

    def test_none_is_treated_as_zero(self):
        assert sum(largest_remainder([None, 1, 1])) == 100

    def test_the_spare_point_goes_to_the_largest_remainder(self):
        assert largest_remainder([1, 1, 1]) == [34, 33, 33]

    def test_ties_are_stable(self):
        """Same input, same output — a report must not reshuffle on refresh."""
        for _ in range(5):
            assert largest_remainder([1, 1, 1, 1, 1, 1]) == \
                   largest_remainder([1, 1, 1, 1, 1, 1])

    def test_order_is_preserved(self):
        """The nth percentage belongs to the nth row."""
        out = largest_remainder([7, 2, 1])
        assert out[0] > out[1] > out[2]


class TestWithPct:

    def test_adds_pct_to_each_row(self):
        rows = with_pct([{"label": "a", "count": 1},
                         {"label": "b", "count": 1},
                         {"label": "c", "count": 1}])
        assert [r["pct"] for r in rows] == [34, 33, 33]
        assert sum(r["pct"] for r in rows) == 100

    def test_works_on_amounts_too(self):
        rows = with_pct([{"amount": 800.0}, {"amount": 400.0}],
                        value_key="amount")
        assert [r["pct"] for r in rows] == [67, 33]

    def test_rows_without_the_key_count_as_zero(self):
        rows = with_pct([{"count": 2}, {}])
        assert rows[0]["pct"] == 100 and rows[1]["pct"] == 0
