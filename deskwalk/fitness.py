"""Stride length, cadence and calories."""

# Stride length without calibration: typical walking values (3 km/h about 0.52 m, 5 km/h about 0.70 m)
STRIDE_BASE, STRIDE_SLOPE = 0.25, 0.09
STRIDE_MIN, STRIDE_MAX = 0.20, 1.20
MAX_CALIBRATION_POINTS = 3

# MET values for level walking (Compendium of Physical Activities), speed in km/h
MET_TABLE = [(1.0, 2.0), (3.2, 2.8), (4.0, 3.0), (4.8, 3.5), (5.6, 4.3), (6.4, 5.0)]


def interpolate(points, x, extrapolate=False):
    """Piecewise linear over sorted (x, y) points. Outside: hold the edge value or extend its slope."""
    if len(points) == 1:
        return points[0][1]
    if x <= points[0][0] or x >= points[-1][0]:
        low = x <= points[0][0]
        (x0, y0), (x1, y1) = (points[0], points[1]) if low else (points[-2], points[-1])
        edge = points[0] if low else points[-1]
        if not extrapolate or x1 == x0:
            return edge[1]
        return edge[1] + (x - edge[0]) * (y1 - y0) / (x1 - x0)
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= x <= x1:
            return y0 if x1 == x0 else y0 + (x - x0) * (y1 - y0) / (x1 - x0)
    return points[-1][1]


def stride_m(speed: float, calibration: list) -> float:
    """Stride length in metres for a speed, from up to 3 calibration points."""
    points = sorted({round(p["speed"], 1): p["stride_m"] for p in calibration}.items())
    if not points:
        stride = STRIDE_BASE + STRIDE_SLOPE * speed
    elif len(points) == 1:
        stride = points[0][1] + STRIDE_SLOPE * (speed - points[0][0])  # typical slope through the one point
    else:
        stride = interpolate(points, speed, extrapolate=True)
    return min(STRIDE_MAX, max(STRIDE_MIN, stride))


def cadence(speed: float, stride: float) -> float:
    """Steps per minute."""
    return speed * 1000 / 60 / stride if speed > 0 and stride > 0 else 0.0


def met(speed: float) -> float:
    return interpolate(MET_TABLE, speed) if speed > 0 else 0.0


def kcal(speed: float, weight_kg: float, seconds: float) -> float:
    """Calories = MET x body weight in kg x hours."""
    return met(speed) * weight_kg * seconds / 3600


def calibration_point(speed: float, steps: int, seconds: float, ts: float) -> dict:
    meters = speed / 3.6 * seconds
    return {"speed": round(speed, 1), "steps": int(steps), "seconds": seconds,
            "stride_m": round(meters / steps, 4), "ts": ts}


def add_calibration(points: list, new: dict) -> list:
    """The same speed replaces the old point. At most 3 points, the oldest one is dropped."""
    points = [p for p in points if round(p["speed"], 1) != new["speed"]] + [new]
    points.sort(key=lambda p: p["ts"])
    return sorted(points[-MAX_CALIBRATION_POINTS:], key=lambda p: p["speed"])
