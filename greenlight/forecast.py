"""Toto 2.0 forecasting over daily test metrics.

Optional dependency: `pip install toto-models` (Python 3.12+). Defaults to Toto-2.0-22m, which
runs fine on CPU. Set GREENLIGHT_TOTO_MODEL to try a larger checkpoint.

Toto is used for continuous signals (durations, rerun volume, suite failure rate). Flake
classification itself stays in analysis.py, because pass/fail on the same SHA is a counting
problem, not a forecasting one.
"""
from __future__ import annotations

import logging
import math
import os
import sqlite3
import statistics
import threading
from collections import defaultdict
from datetime import timedelta
from typing import Any

from .db import day_range, since, utcnow

MODEL_ID = os.environ.get("GREENLIGHT_TOTO_MODEL", "Datadog/Toto-2.0-22m")
BATCH_SIZE = 64
SUITE_METRICS = ("failure_rate", "suite_duration_ms", "runs", "reruns")
_COUNT_METRICS = {"runs", "reruns"}  # a day with no rows is a real zero, not missing data

_model = None
_lock = threading.Lock()


def _load_model():
    global _model
    with _lock:
        if _model is None:
            try:
                import torch
                from toto2 import Toto2Model
            except ImportError as e:
                raise RuntimeError("Toto is not installed. Run `pip install toto-models` (requires Python 3.12+).") from e
            logging.getLogger("httpx").setLevel(logging.WARNING)  # quiet Hugging Face checks
            device = "cuda" if torch.cuda.is_available() else "cpu"
            _model = Toto2Model.from_pretrained(MODEL_ID).to(device).eval()
    return _model


def forecast_batch(series: list[list[float | None]], horizon: int) -> list[dict[str, list[float]]]:
    """Zero-shot quantile forecast for each series. None = missing. Returns p10/p50/p90 per series."""
    model = _load_model()  # first, so a missing install raises the friendly RuntimeError
    import torch

    patch = getattr(getattr(model, "config", None), "patch_size", 32)
    device = next(model.parameters()).device
    out: list[dict[str, list[float]]] = []

    for start in range(0, len(series), BATCH_SIZE):
        chunk = series[start:start + BATCH_SIZE]
        length = math.ceil(max(len(s) for s in chunk) / patch) * patch  # Toto needs whole patches
        target = torch.zeros(len(chunk), 1, length)
        mask = torch.zeros(len(chunk), 1, length, dtype=torch.bool)
        for i, s in enumerate(chunk):
            offset = length - len(s)  # left-pad; padded steps stay masked out
            for j, v in enumerate(s):
                if v is not None and math.isfinite(v):
                    target[i, 0, offset + j] = float(v)
                    mask[i, 0, offset + j] = True
        ids = torch.zeros(len(chunk), 1, dtype=torch.long)
        with torch.no_grad():
            q = model.forecast(
                {"target": target.to(device), "target_mask": mask.to(device), "series_ids": ids.to(device)},
                horizon=horizon,
                decode_block_size=None,  # single pass: better for short horizons
                has_missing_values=not bool(mask.all()),
            ).cpu()  # (9 quantiles 0.1..0.9, batch, variates, horizon)
        for i in range(len(chunk)):
            out.append({"p10": q[0, i, 0].tolist(), "p50": q[4, i, 0].tolist(), "p90": q[8, i, 0].tolist()})
    return out


def _round_band(pred: dict[str, list[float]], floor: float = 0.0) -> dict[str, list[float]]:
    return {k: [round(max(v, floor), 4) for v in vals] for k, vals in pred.items()}


def detect_anomalies(
    series: dict[str, list[float | None]],
    days: list[str],
    holdout: int = 3,
    min_points: int = 14,
    min_ratio: float = 0.2,
) -> tuple[list[dict[str, Any]], int]:
    """Forecast the last `holdout` days from everything before them and flag days that land above
    p90 AND at least min_ratio above p50. Returns (anomalies, series_checked)."""
    keys = [k for k, v in series.items() if sum(x is not None for x in v[:-holdout]) >= min_points]
    if not keys:
        return [], 0
    preds = forecast_batch([series[k][:-holdout] for k in keys], holdout)

    anomalies = []
    for key, pred in zip(keys, preds):
        breaches = []
        for j in range(holdout):
            actual = series[key][len(series[key]) - holdout + j]
            p50, p90 = max(pred["p50"][j], 0.0), max(pred["p90"][j], 0.0)
            if actual is None or p50 <= 0:
                continue
            if actual > p90 and actual >= p50 * (1 + min_ratio):
                breaches.append({"day": days[len(days) - holdout + j], "actual": round(actual, 3),
                                 "expected_p50": round(p50, 3), "upper_p90": round(p90, 3),
                                 "pct_over_expected": round(100 * (actual / p50 - 1), 1)})
        if breaches:
            anomalies.append({"series": key, "breaches": breaches,
                              "worst_pct_over_expected": max(b["pct_over_expected"] for b in breaches),
                              "forecast": _round_band(pred)})
    anomalies.sort(key=lambda a: a["worst_pct_over_expected"], reverse=True)
    return anomalies, len(keys)


def daily_test_durations(
    conn: sqlite3.Connection, lookback_days: int, min_points: int, limit: int, test_ids: list[str] | None = None
) -> tuple[list[str], dict[str, list[float | None]]]:
    """Median passing duration per test per day. Keeps the `limit` slowest tests with enough history."""
    days = day_range(lookback_days)
    index = {d: i for i, d in enumerate(days)}
    buckets: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    params: list = [since(lookback_days)]
    test_filter = ""
    if test_ids:
        test_filter = f"AND r.test_id IN ({','.join('?' * len(test_ids))})"
        params.extend(test_ids)
    for test_id, day, ms in conn.execute(
        f"""SELECT r.test_id, substr(ru.started_at, 1, 10), r.duration_ms
            FROM results r JOIN runs ru ON ru.run_id = r.run_id
            WHERE ru.started_at >= ? AND r.outcome = 'pass' AND r.duration_ms IS NOT NULL {test_filter}""",
        params,
    ):
        if day in index:
            buckets[test_id][day].append(ms)

    candidates = []
    for test_id, per_day in buckets.items():
        if len(per_day) < min_points:
            continue
        values: list[float | None] = [None] * len(days)
        for day, vals in per_day.items():
            values[index[day]] = float(statistics.median(vals))
        overall = statistics.median(v for v in values if v is not None)
        candidates.append((overall, test_id, values))
    candidates.sort(reverse=True)
    return days, {t: v for _, t, v in candidates[:limit]}


def daily_suite_metric(conn: sqlite3.Connection, metric: str, lookback_days: int) -> tuple[list[str], list[float | None]]:
    if metric not in SUITE_METRICS:
        raise ValueError(f"metric must be one of {SUITE_METRICS}")
    days = day_range(lookback_days)
    if metric == "failure_rate":
        sql = """SELECT substr(ru.started_at, 1, 10),
                        1.0 * SUM(r.outcome IN ('fail','error')) / COUNT(*)
                 FROM results r JOIN runs ru ON ru.run_id = r.run_id
                 WHERE ru.started_at >= ? AND r.outcome != 'skip' GROUP BY 1"""
        rows = dict(conn.execute(sql, (since(lookback_days),)).fetchall())
    elif metric == "suite_duration_ms":
        per_day: dict[str, list[int]] = defaultdict(list)
        for day, ms in conn.execute(
            "SELECT substr(started_at, 1, 10), duration_ms FROM runs WHERE started_at >= ? AND duration_ms IS NOT NULL",
            (since(lookback_days),)):
            per_day[day].append(ms)
        rows = {d: float(statistics.median(v)) for d, v in per_day.items()}
    else:
        cond = "attempt > 1" if metric == "reruns" else "1 = 1"
        rows = dict(conn.execute(
            f"SELECT substr(started_at, 1, 10), COUNT(*) FROM runs WHERE started_at >= ? AND {cond} GROUP BY 1",
            (since(lookback_days),)).fetchall())
    fill = 0.0 if metric in _COUNT_METRICS else None
    return days, [float(rows[d]) if d in rows else fill for d in days]


def duration_regressions(
    conn: sqlite3.Connection,
    holdout_days: int = 3,
    lookback_days: int = 90,
    min_points: int = 14,
    limit: int = 200,
    min_ratio: float = 0.2,
    include_series: int = 0,
) -> dict[str, Any]:
    """include_series=N attaches the last N days of actuals to each regression (for charts)."""
    days, series = daily_test_durations(conn, lookback_days, min_points + holdout_days, limit)
    anomalies, checked = detect_anomalies(series, days, holdout_days, min_points, min_ratio)
    if include_series:
        for a in anomalies:
            a["days"] = days[-include_series:]
            a["values"] = series[a["series"]][-include_series:]
    return {"model": MODEL_ID, "holdout_days": holdout_days, "tests_checked": checked, "regressions": anomalies}


def test_duration_forecast(
    conn: sqlite3.Connection,
    test_id: str,
    lookback_days: int = 60,
    holdout_days: int = 3,
    horizon_days: int = 7,
    min_points: int = 14,
) -> dict[str, Any]:
    """One test's daily median duration with a backtest band over the last holdout_days
    (forecast from the days before them) and a forward forecast."""
    days, series = daily_test_durations(conn, lookback_days, 1, 1, [test_id])
    values = series.get(test_id)
    observed = sum(v is not None for v in values) if values else 0
    if observed < min_points + holdout_days:
        raise ValueError(f"Only {observed} days with passing runs; need {min_points + holdout_days}+ to forecast.")
    h = max(holdout_days, horizon_days)
    backtest, future = forecast_batch([values[:-holdout_days], values], h)
    last = utcnow().date()
    return {
        "model": MODEL_ID, "test_id": test_id, "days": days, "values": values,
        "backtest": _round_band({k: v[:holdout_days] for k, v in backtest.items()}),
        "future_days": [(last + timedelta(days=i + 1)).isoformat() for i in range(horizon_days)],
        "future": _round_band({k: v[:horizon_days] for k, v in future.items()}),
    }


def suite_forecast(
    conn: sqlite3.Connection,
    metric: str = "failure_rate",
    lookback_days: int = 90,
    horizon_days: int = 7,
    min_points: int = 14,
    history_days: int = 14,
) -> dict[str, Any]:
    days, values = daily_suite_metric(conn, metric, lookback_days)
    observed = sum(v is not None for v in values)
    if observed < min_points:
        raise ValueError(f"Only {observed} days of '{metric}' data; need {min_points}+ for a useful forecast.")
    pred = forecast_batch([values], horizon_days)[0]
    last = utcnow().date()
    forecast_rows = [{"day": (last + timedelta(days=i + 1)).isoformat(),
                      "p10": round(max(pred["p10"][i], 0.0), 4),
                      "p50": round(max(pred["p50"][i], 0.0), 4),
                      "p90": round(max(pred["p90"][i], 0.0), 4)} for i in range(horizon_days)]
    recent, _ = detect_anomalies({metric: values}, days, holdout=3, min_points=min_points)
    return {"model": MODEL_ID, "metric": metric,
            "recent": [{"day": d, "value": None if v is None else round(v, 4)}
                       for d, v in zip(days[-history_days:], values[-history_days:])],
            "forecast": forecast_rows,
            "recent_anomaly": recent[0]["breaches"] if recent else None}
