"""Hidden acceptance tests for the allocate benchmark. Written from the spec before either arm ran.
Categories (by test name prefix): example_, error_, big_, prop_, diff_, split_."""
import random
from fractions import Fraction

import pytest

from allocate import allocate, split_evenly


def oracle(total, weights):
    """Independent exact implementation (Fractions) of the spec, used for differential testing."""
    W = sum(weights)
    n = abs(total)
    ideal = [Fraction(n * w, W) for w in weights]
    floors = [x.numerator // x.denominator for x in ideal]
    frac = [ideal[i] - floors[i] for i in range(len(weights))]
    left = n - sum(floors)
    pick = sorted(range(len(weights)), key=lambda i: (-frac[i], i))[:left]
    out = floors[:]
    for i in pick:
        out[i] += 1
    return [-x for x in out] if total < 0 else out


# ---------------------------------------------------------------- worked examples
@pytest.mark.parametrize("total, weights, expected", [
    (100, [1, 1, 1], [34, 33, 33]),
    (10, [1, 2, 3], [2, 3, 5]),
    (5, [1, 1], [3, 2]),
    (0, [1, 2], [0, 0]),
    (-100, [1, 1, 1], [-34, -33, -33]),
    (7, [0, 1, 0], [0, 7, 0]),
    (1, [0, 1, 1], [0, 1, 0]),
    (1, [1, 1, 1], [1, 0, 0]),
    (2, [1, 1, 1], [1, 1, 0]),
    (100, [50, 30, 20], [50, 30, 20]),
    (3, [1], [3]),
    (-7, [3, 3, 1], [-3, -3, -1]),
    (-10, [1, 2, 3], [-2, -3, -5]),
    (5, [2, 2, 1], [2, 2, 1]),
    (1000, [1] * 7, [143] * 6 + [142]),
    (10, [1, 1, 1, 1], [3, 3, 2, 2]),
    (11, [1, 2], [4, 7]),
    (11, [2, 1], [7, 4]),
    (1, [10 ** 20, 10 ** 20], [1, 0]),
    (-1, [1, 1, 1], [-1, 0, 0]),
    (-2, [0, 1, 1], [0, -1, -1]),
    (0, [0, 5], [0, 0]),
    (1, [3, 3, 0, 3], [1, 0, 0, 0]),
    (4, [1, 1, 0, 1, 1], [1, 1, 0, 1, 1]),
])
def test_example_worked_cases(total, weights, expected):
    assert allocate(total, weights) == expected


def test_example_negative_is_mirror_of_positive():
    for total, weights in [(100, [1, 1, 1]), (7, [2, 5]), (13, [1, 2, 3, 4]), (1, [1, 1, 1])]:
        assert allocate(-total, weights) == [-s for s in allocate(total, weights)]


def test_example_ties_go_to_lower_index():
    assert allocate(2, [5, 5, 5, 5]) == [1, 1, 0, 0]
    assert allocate(3, [1] * 5) == [1, 1, 1, 0, 0]


def test_example_largest_remainder_not_largest_weight():
    # weights [2, 1] with 11: the leftover cent goes to the LARGER remainder (index 1), not the larger weight
    assert allocate(11, [2, 1]) == [7, 4]


def test_example_zero_weight_party_gets_nothing():
    for total in (1, 2, 3, 10, 99, -1, -50):
        shares = allocate(total, [3, 0, 7, 0])
        assert shares[1] == 0 and shares[3] == 0
        assert sum(shares) == total


# ---------------------------------------------------------------- errors
@pytest.mark.parametrize("bad_total", [1.0, 10.5, "10", None, True, False, [10], (10,), 3 + 0j])
def test_error_total_must_be_a_real_int(bad_total):
    with pytest.raises(TypeError):
        allocate(bad_total, [1, 1])


@pytest.mark.parametrize("bad_weights", [(1, 1), None, "11", {1: 1}, 5, {1, 2}, range(3)])
def test_error_weights_must_be_a_list(bad_weights):
    with pytest.raises(TypeError):
        allocate(10, bad_weights)


@pytest.mark.parametrize("bad_weights", [[1, 2.5], [True, 1], [1, "2"], [None, 1], [1.0, 1], [1, False]])
def test_error_each_weight_must_be_a_real_int(bad_weights):
    with pytest.raises(TypeError):
        allocate(10, bad_weights)


@pytest.mark.parametrize("bad_weights", [[], [-1, 2], [2, -1], [0, 0, 0], [0], [5, -5]])
def test_error_invalid_weight_values_raise_value_error(bad_weights):
    with pytest.raises(ValueError):
        allocate(10, bad_weights)


def test_error_value_errors_do_not_depend_on_the_total():
    for total in (0, 5, -5):
        with pytest.raises(ValueError):
            allocate(total, [0, 0])


def test_error_type_error_is_not_a_value_error():
    with pytest.raises(TypeError) as info:
        allocate(1.5, [1])
    assert not isinstance(info.value, ValueError)


# ---------------------------------------------------------------- big numbers and exactness
def test_big_thirds_are_exact():
    assert allocate(10 ** 30, [1, 1, 1]) == [
        333333333333333333333333333334, 333333333333333333333333333333, 333333333333333333333333333333]
    assert allocate(-(10 ** 30), [1, 1, 1]) == [
        -333333333333333333333333333334, -333333333333333333333333333333, -333333333333333333333333333333]


def test_big_lopsided_weights_match_the_oracle():
    for total, weights in [(10 ** 18 + 1, [1, 10 ** 18]), (2 ** 100 + 12345, [3, 5, 7, 11]),
                           (10 ** 40 - 1, [10 ** 30, 1, 10 ** 20]), (-(10 ** 25) - 7, [9, 1, 1])]:
        assert allocate(total, weights) == oracle(total, weights)
        assert sum(allocate(total, weights)) == total


def test_big_shares_are_plain_ints():
    shares = allocate(10 ** 20 + 3, [1, 2, 3])
    assert all(type(s) is int for s in shares)


def test_big_float_precision_trap():
    # a float implementation loses the last digits here
    assert allocate(9007199254740993, [1, 1]) == [4503599627370497, 4503599627370496]


# ---------------------------------------------------------------- properties
def _cases(seed, count, max_total, max_w, max_len):
    rnd = random.Random(seed)
    for _ in range(count):
        total = rnd.randint(-max_total, max_total)
        weights = [rnd.randint(0, max_w) for _ in range(rnd.randint(1, max_len))]
        if sum(weights) == 0:
            weights[0] = 1
        yield total, weights


def test_prop_shares_always_sum_to_total():
    for total, weights in _cases(1, 400, 10 ** 6, 100, 8):
        assert sum(allocate(total, weights)) == total


def test_prop_every_share_is_within_one_cent_of_its_ideal_and_the_right_length():
    for total, weights in _cases(2, 300, 10 ** 5, 50, 7):
        shares = allocate(total, weights)
        W = sum(weights)
        assert len(shares) == len(weights)
        for s, w in zip(shares, weights):
            assert abs(Fraction(total * w, W) - s) < 1


def test_prop_input_list_is_not_modified():
    weights = [3, 1, 4, 1, 5]
    snapshot = list(weights)
    allocate(100, weights)
    assert weights == snapshot


def test_prop_signs_never_flip():
    for total, weights in _cases(3, 200, 10 ** 4, 20, 6):
        shares = allocate(total, weights)
        if total >= 0:
            assert all(s >= 0 for s in shares)
        else:
            assert all(s <= 0 for s in shares)


def test_prop_deterministic():
    assert allocate(97, [2, 3, 5, 7]) == allocate(97, [2, 3, 5, 7])


# ---------------------------------------------------------------- differential against the oracle
def test_diff_small_totals_tie_heavy():
    for total, weights in _cases(4, 500, 12, 3, 6):
        assert allocate(total, weights) == oracle(total, weights)


def test_diff_medium_totals():
    for total, weights in _cases(5, 500, 10 ** 6, 1000, 8):
        assert allocate(total, weights) == oracle(total, weights)


def test_diff_huge_totals():
    for total, weights in _cases(6, 80, 10 ** 40, 10 ** 12, 6):
        assert allocate(total, weights) == oracle(total, weights)


# ---------------------------------------------------------------- split_evenly
@pytest.mark.parametrize("total, n, expected", [
    (10, 3, [4, 3, 3]), (-10, 3, [-4, -3, -3]), (0, 5, [0] * 5), (7, 1, [7]), (2, 5, [1, 1, 0, 0, 0]),
    (100, 4, [25] * 4), (1, 3, [1, 0, 0]),
])
def test_split_examples(total, n, expected):
    assert split_evenly(total, n) == expected


def test_split_matches_allocate_with_unit_weights():
    for total, n in [(17, 4), (-17, 4), (1000, 7), (5, 9), (0, 3)]:
        assert split_evenly(total, n) == allocate(total, [1] * n)


@pytest.mark.parametrize("bad_n", [0, -1, -100])
def test_split_error_n_must_be_at_least_one(bad_n):
    with pytest.raises(ValueError):
        split_evenly(10, bad_n)


@pytest.mark.parametrize("bad_n", [2.0, "3", None, True, False, [3]])
def test_split_error_n_must_be_a_real_int(bad_n):
    with pytest.raises(TypeError):
        split_evenly(10, bad_n)


@pytest.mark.parametrize("bad_total", [1.5, "10", None, True])
def test_split_error_total_must_be_a_real_int(bad_total):
    with pytest.raises(TypeError):
        split_evenly(bad_total, 3)
