from typing import Optional, List

def last_confirmed_swing_low(lows: List[float], L: int) -> Optional[int]:
    """
    Return index of last confirmed swing low using pivot L.
    Confirmed pivot at i means lows[i] is lower than L bars on the left
    AND lower than L bars on the right.
    """
    n = len(lows)
    if n < 2 * L + 1:
        return None

    last_idx = None
    for i in range(L, n - L):
        pivot = lows[i]
        left = lows[i - L:i]
        right = lows[i + 1:i + L + 1]
        if all(pivot < x for x in left) and all(pivot < x for x in right):
            last_idx = i

    return last_idx


def last_confirmed_swing_high(highs: List[float], L: int) -> Optional[int]:
    """
    Returns index of last confirmed swing high.
    A swing high is higher than L bars on both sides.
    """
    n = len(highs)
    if n < 2 * L + 1:
        return None

    for i in range(n - L - 1, L - 1, -1):
        pivot = highs[i]
        left = highs[i - L:i]
        right = highs[i + 1:i + 1 + L]
        if all(pivot > x for x in left) and all(pivot > x for x in right):
            return i

    return None
