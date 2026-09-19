"""Reference solution for the allocate benchmark (the oracle the hidden tests are checked against)."""


def _check_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")


def allocate(total, weights):
    _check_int(total, "total")
    if not isinstance(weights, list):
        raise TypeError("weights must be a list")
    for w in weights:
        _check_int(w, "weight")
    if not weights:
        raise ValueError("weights must not be empty")
    if any(w < 0 for w in weights):
        raise ValueError("weights must be non-negative")
    W = sum(weights)
    if W == 0:
        raise ValueError("at least one weight must be positive")
    n = abs(total)
    shares = [n * w // W for w in weights]
    remainders = [n * w % W for w in weights]
    leftover = n - sum(shares)
    order = sorted(range(len(weights)), key=lambda i: (-remainders[i], i))
    for i in order[:leftover]:
        shares[i] += 1
    if total < 0:
        shares = [-s for s in shares]
    return shares


def split_evenly(total, n):
    _check_int(total, "total")
    _check_int(n, "n")
    if n < 1:
        raise ValueError("n must be at least 1")
    return allocate(total, [1] * n)
