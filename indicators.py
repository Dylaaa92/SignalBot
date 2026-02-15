import numpy as np

def ema(values, period: int):
    """
    Compute EMA for a list/array of values.
    Returns the latest EMA value (float) or None if not enough data.
    """
    values = np.array(values, dtype=float)
    if len(values) < period:
        return None

    k = 2 / (period + 1)
    e = float(values[0])
    for v in values[1:]:
        e = float(v) * k + e * (1 - k)
    return float(e)
