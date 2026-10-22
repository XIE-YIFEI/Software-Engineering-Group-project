"""Short-term traffic forecasting for a fixed camera: 1-minute series of one direction (or lane) forecast 5, 15
and 30 minutes ahead, plus whether the road will be congested (TEAM_GUIDE.md section 10).

    python -m alpr.forecast --days data/series/jb_ciq_ipc_2023-03-21.csv data/series/jb_ciq_cam02_2023-03-22.csv         --entity "To CIQ" --measures fill --extra none --pool TC1,TC2,TC3,TC4         --congestion 0.5 --congestion-measure fill --out outputs/forecast/to_ciq_AC_fill_pool.json

Series files come from ``python -m alpr.congestion --series`` (far / high cameras: occupancy, vehicles, fill,
queued zones) or ``python -m alpr.series`` (lane cameras: flow, occupancy, density, speed): one row per
minute of the camera's on-screen clock, one column per <entity>.<measure>. Every model is trained on one day
and tested on another (every ordered pair of --days); at a forecast origin t (the end of minute t) only data
up to and including minute t is used ("past-only"; the unit tests check that changing the future never
changes a feature or a forecast). All models are scored on the same origins.

Models
  persistence      y(t+h) = y(t)
  moving average   mean of the last 15 minutes
  daily profile    the training day's value at clock time t+h (15-minute moving average): the
                   "same time yesterday" forecast, no information from the test day at all
  profile change   today's value plus the training day's change from clock time t to t+h (keeps
                   today's level, borrows the other day's shape)
  ARIMA            statsmodels ARIMA, order picked by AIC on the training day, then run over the test
                   day with its parameters fixed (Kalman filter: the state at t uses data <= t only)
  LightGBM         gradient-boosted trees, one model per horizon, on lags, rolling statistics, trend
                   and time of day; it predicts the change from the last value (transfers across cameras);
                   --pool adds more series of the training day (e.g. its zones) to the training data
  GRU              a small recurrent network (torch) on the last 60 minutes of the measures + time of
                   day, one output (change from the last value) per horizon; --pool as for LightGBM

Metrics: MAE and RMSE per horizon. Congestion (on --congestion-measure): congested while the 5-minute mean
is >= --congestion, dips shorter than 10 minutes merged; an onset starts an episode of >= 10 minutes. Per
horizon h: minute-level precision / recall of "congested at t+h", and per onset the warning time = how
many minutes before it a forecast made while the road was still free first called it (onsets are rare:
with two days this is a handful of events, reported as such).
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

HORIZONS = (5, 15, 30)
LAGS = (0, 1, 2, 3, 4, 5, 10, 15, 20, 30)          # minutes back from the origin (0 = the origin minute)
WINDOWS = (5, 15, 30)                               # rolling mean / std windows (minutes, ending at the origin)
CONGESTION_OCC = 0.30
SMOOTH_MIN = 5                                      # congestion state from the 5-minute mean occupancy
ONSET_MIN_LEN = 10                                  # an onset counts when congestion lasts this long


# ---------------------------------------------------------------------------- series
@dataclass
class DaySeries:
    """One camera-day: per-minute columns (NaN = not observed)."""
    name: str
    minute_of_day: np.ndarray                      # int, minutes since 00:00 (camera clock)
    columns: Dict[str, np.ndarray] = field(default_factory=dict)
    times: List[str] = field(default_factory=list)  # ISO minute stamps

    def __len__(self) -> int:
        return len(self.minute_of_day)

    def get(self, entity: str, measure: str) -> np.ndarray:
        return self.columns[f"{entity}.{measure}"]

    def entities(self) -> List[str]:
        return list(dict.fromkeys(c.rsplit(".", 1)[0] for c in self.columns))


def load_series(path: str) -> DaySeries:
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    name = os.path.splitext(os.path.basename(path))[0]
    cols = [c for c in rows[0] if c not in ("time", "minute_of_day")]
    out = DaySeries(name, np.array([int(r["minute_of_day"]) for r in rows]),
                    times=[r["time"] for r in rows])
    for c in cols:
        out.columns[c] = np.array([float(r[c]) if r[c] not in ("", "nan") else np.nan for r in rows])
    return out


# ---------------------------------------------------------------------------- features (past only)
def _ffill(y: np.ndarray) -> np.ndarray:
    """Forward fill (a gap takes the last observed value: only the past is used)."""
    y = np.asarray(y, float).copy()
    last = np.nan
    for i, v in enumerate(y):
        if np.isnan(v):
            y[i] = last
        else:
            last = v
    return y


def past_features(y: np.ndarray, minute_of_day: np.ndarray, extra: Optional[Dict[str, np.ndarray]] = None,
                  lags: Sequence[int] = LAGS, windows: Sequence[int] = WINDOWS) -> Tuple[np.ndarray, List[str]]:
    """Feature matrix, row t built from minutes <= t only: lagged values, rolling mean / std over the last
    w minutes, the 5-minute trend, time of day (sin, cos), and the same lags of any `extra` series
    (e.g. occupancy when forecasting flow). NaN where the history is too short."""
    n = len(y)
    names, cols = [], []

    def add(name, v):
        names.append(name)
        cols.append(v)

    series = {"y": _ffill(y)}
    for k, v in (extra or {}).items():
        series[k] = _ffill(v)
    for k, s in series.items():
        for L in lags:
            add(f"{k}_lag{L}", np.r_[np.full(L, np.nan), s[:n - L]] if L else s.copy())
        for w in windows:
            m, sd = np.full(n, np.nan), np.full(n, np.nan)
            for t in range(w - 1, n):
                seg = s[t - w + 1:t + 1]
                if not np.isnan(seg).all():
                    m[t], sd[t] = np.nanmean(seg), np.nanstd(seg)
            add(f"{k}_mean{w}", m)
            add(f"{k}_std{w}", sd)
        add(f"{k}_trend5", s - np.r_[np.full(5, np.nan), s[:n - 5]])
    ang = 2 * np.pi * np.asarray(minute_of_day, float) / 1440.0
    add("tod_sin", np.sin(ang))
    add("tod_cos", np.cos(ang))
    return np.column_stack(cols), names


def future_target(y: np.ndarray, h: int) -> np.ndarray:
    """target[t] = y[t + h] (NaN past the end)."""
    return np.r_[np.asarray(y, float)[h:], np.full(h, np.nan)]


# ---------------------------------------------------------------------------- models
class Forecaster:
    """fit on a training day, then forecast(test) -> {h: array}: element t = forecast for t+h made at t."""
    name = "base"

    def fit(self, train: "Target") -> "Forecaster":
        return self

    def forecast(self, test: "Target", horizons: Sequence[int]) -> Dict[int, np.ndarray]:
        raise NotImplementedError


@dataclass
class Target:
    """What to forecast on one day: the series y, its time of day, and other measures of the same entity."""
    y: np.ndarray
    minute_of_day: np.ndarray
    extra: Dict[str, np.ndarray] = field(default_factory=dict)


class Persistence(Forecaster):
    name = "persistence"

    def forecast(self, test, horizons):
        y = _ffill(test.y)
        return {h: y.copy() for h in horizons}


class MovingAverage(Forecaster):
    name = "moving average"

    def __init__(self, window: int = 15):
        self.window = window

    def forecast(self, test, horizons):
        y = _ffill(test.y)
        out = np.full(len(y), np.nan)
        for t in range(len(y)):
            seg = y[max(0, t - self.window + 1):t + 1]
            if not np.isnan(seg).all():
                out[t] = np.nanmean(seg)
        return {h: out.copy() for h in horizons}


class DailyProfile(Forecaster):
    """The training day's (smoothed) value at the same clock time: uses nothing from the test day."""
    name = "daily profile"

    def __init__(self, smooth: int = 15):
        self.smooth = smooth

    def fit(self, train):
        y = _ffill(train.y)
        k = self.smooth // 2
        sm = np.array([np.nanmean(y[max(0, i - k):i + k + 1]) for i in range(len(y))])
        self.profile = dict(zip(np.asarray(train.minute_of_day).tolist(), sm.tolist()))
        return self

    def forecast(self, test, horizons):
        mod = np.asarray(test.minute_of_day)
        return {h: np.array([self.profile.get(int(m) + h, np.nan) for m in mod]) for h in horizons}


class ProfileChange(DailyProfile):
    """Persistence plus the training day's change over the same clock interval: y(t) + P(t+h) - P(t).
    Keeps today's level (the cameras / days differ in level) and borrows only the shape of the other day."""
    name = "profile change"

    def forecast(self, test, horizons):
        y = _ffill(test.y)
        mod = np.asarray(test.minute_of_day)
        now = np.array([self.profile.get(int(m), np.nan) for m in mod])
        return {h: y + np.array([self.profile.get(int(m) + h, np.nan) for m in mod]) - now for h in horizons}


class ARIMAForecaster(Forecaster):
    """ARIMA(p, d, q) with the order picked by AIC on the training day; on the test day the fitted
    parameters stay fixed and the Kalman filter runs over the test day's data, so the forecast made at
    t depends on the test day only up to t."""
    name = "ARIMA"

    def __init__(self, orders: Sequence[Tuple[int, int, int]] = ((1, 0, 0), (2, 0, 0), (3, 0, 0), (5, 0, 0),
                                                                 (1, 0, 1), (2, 0, 1), (1, 1, 1), (2, 1, 1))):
        self.orders = orders

    def fit(self, train):
        import warnings
        from statsmodels.tsa.arima.model import ARIMA
        y = _ffill(train.y)
        y = y[~np.isnan(y)]
        best = None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            for order in self.orders:
                try:
                    res = ARIMA(y, order=order, trend="c" if order[1] == 0 else "n").fit()
                except Exception:                                    # noqa: BLE001  (non-convergent order)
                    continue
                if best is None or res.aic < best.aic:
                    best = res
        self.res = best
        self.order = best.model.order
        return self

    def forecast(self, test, horizons):
        import warnings
        y = _ffill(test.y)
        n = len(y)
        first = int(np.argmax(~np.isnan(y))) if (~np.isnan(y)).any() else n
        out = {h: np.full(n, np.nan) for h in horizons}
        hmax = max(horizons)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            res = self.res.apply(y[first:])                          # same parameters, test-day data
            for t in range(first, n):
                fc = res.get_prediction(start=t - first + 1, end=t - first + hmax, dynamic=True).predicted_mean \
                    if t - first + 1 < n - first else res.forecast(hmax)
                fc = np.asarray(fc, float)
                if len(fc) < hmax:                                   # past the end: extend from t
                    fc = np.asarray(self.res.apply(y[first:t + 1]).forecast(hmax), float)
                for h in horizons:
                    out[h][t] = fc[h - 1]
        return out


class LightGBMForecaster(Forecaster):
    """One gradient-boosted regressor per horizon on past_features (lags, rolling stats, trend, time of day).
    It learns the change y(t+h) - y(t), not the level: the two days come from different cameras whose
    levels differ, the dynamics (how fast a queue builds) transfer better."""
    name = "LightGBM"

    def __init__(self, extra_keys: Sequence[str] = (), seed: int = 0):
        self.extra_keys = list(extra_keys)
        self.seed = seed
        self.models: Dict[int, object] = {}

    def _X(self, tgt: Target):
        return past_features(tgt.y, tgt.minute_of_day, {k: tgt.extra[k] for k in self.extra_keys if k in tgt.extra})[0]

    def fit(self, train, horizons: Sequence[int] = HORIZONS, pool: Sequence[Target] = ()):
        """pool: more series of the training day in the same units (e.g. the zones of the direction):
        one model is trained on all of them (a "global" model; 1 day is little data)."""
        import lightgbm as lgb
        params = {"objective": "regression", "learning_rate": 0.03, "num_leaves": 15, "min_data_in_leaf": 10,
                  "bagging_fraction": 0.8, "bagging_freq": 1, "feature_fraction": 0.8, "seed": self.seed,
                  "deterministic": True, "num_threads": 4, "verbose": -1}
        series = [train] + list(pool)
        Xs = [self._X(t) for t in series]
        for h in horizons:
            X_all, y_all = [], []
            for t, X in zip(series, Xs):
                yt = future_target(t.y, h) - _ffill(t.y)            # the change over h minutes
                ok = ~np.isnan(yt) & ~np.isnan(X).all(1)
                X_all.append(X[ok])
                y_all.append(yt[ok])
            self.models[h] = lgb.train(params, lgb.Dataset(np.vstack(X_all), np.concatenate(y_all)),
                                       num_boost_round=300)
        return self

    def forecast(self, test, horizons):
        X = self._X(test)
        now = _ffill(test.y)
        return {h: now + self.models[h].predict(X) for h in horizons}


class GRUForecaster(Forecaster):
    """Small GRU on the last `seq` minutes of [y, extra measures, time of day]; one output per horizon (the
    change from the last value, like LightGBM).
    Inputs are standardised with the training day's mean / std; the last 20 % of the training day
    (chronological) is the validation set for early stopping."""
    name = "GRU"

    def __init__(self, extra_keys: Sequence[str] = (), seq: int = 60, hidden: int = 24, epochs: int = 300,
                 seed: int = 0):
        self.extra_keys, self.seq, self.hidden, self.epochs, self.seed = list(extra_keys), seq, hidden, epochs, seed

    def _inputs(self, tgt: Target) -> np.ndarray:
        chans = [_ffill(tgt.y)] + [_ffill(tgt.extra[k]) for k in self.extra_keys if k in tgt.extra]
        X = np.column_stack(chans)
        return X

    def _windows(self, X: np.ndarray, mod: np.ndarray, ends: np.ndarray) -> np.ndarray:
        ang = 2 * np.pi * np.asarray(mod, float) / 1440.0
        Z = np.column_stack([(X - self.mu) / self.sd, np.sin(ang), np.cos(ang)])
        Z = np.nan_to_num(Z, nan=0.0)
        return np.stack([Z[e - self.seq + 1:e + 1] for e in ends]).astype(np.float32)

    def fit(self, train, horizons: Sequence[int] = HORIZONS, pool: Sequence[Target] = ()):
        """pool: more series of the training day in the same units, trained on together (see LightGBM)."""
        import torch
        torch.manual_seed(self.seed)
        np.random.seed(self.seed)
        self.horizons = list(horizons)
        series = [train] + list(pool)
        X = self._inputs(train)
        self.mu, self.sd = np.nanmean(X, 0), np.nanstd(X, 0) + 1e-6
        parts = []
        for t in series:
            Xt, y = self._inputs(t), _ffill(t.y)
            ends = np.array([e for e in range(self.seq - 1, len(y) - max(horizons))
                             if not np.isnan([y[e + h] for h in horizons]).any()], int)
            if not len(ends):
                continue
            W = self._windows(Xt, t.minute_of_day, ends)
            T = np.column_stack([(y[ends + h] - y[ends]) / self.sd[0] for h in horizons]).astype(np.float32)
            n_val = max(1, int(0.2 * len(ends)))                # last 20 % of each series (chronological)
            parts.append((W[:-n_val], T[:-n_val], W[-n_val:], T[-n_val:]))
        Wtr, Ttr = np.concatenate([p[0] for p in parts]), np.concatenate([p[1] for p in parts])
        Wva, Tva = np.concatenate([p[2] for p in parts]), np.concatenate([p[3] for p in parts])
        net = _GRUNet(W.shape[2], self.hidden, len(horizons))
        opt = torch.optim.Adam(net.parameters(), lr=3e-3, weight_decay=1e-4)
        best, best_state, bad = np.inf, None, 0
        xt, yt = torch.from_numpy(Wtr), torch.from_numpy(Ttr)
        xv, yv = torch.from_numpy(Wva), torch.from_numpy(Tva)
        for _ in range(self.epochs):
            net.train()
            perm = torch.randperm(len(xt))
            for i in range(0, len(xt), 64):
                b = perm[i:i + 64]
                opt.zero_grad()
                loss = torch.mean((net(xt[b]) - yt[b]) ** 2)
                loss.backward()
                opt.step()
            net.eval()
            with torch.no_grad():
                v = float(torch.mean((net(xv) - yv) ** 2))
            if v < best - 1e-4:
                best, bad = v, 0
                best_state = {k: t.clone() for k, t in net.state_dict().items()}
            else:
                bad += 1
                if bad >= 30:
                    break
        net.load_state_dict(best_state)
        self.net = net
        return self

    def forecast(self, test, horizons):
        import torch
        X = self._inputs(test)
        n = len(X)
        out = {h: np.full(n, np.nan) for h in horizons}
        ends = np.arange(self.seq - 1, n)
        if len(ends):
            with torch.no_grad():
                P = self.net(torch.from_numpy(self._windows(X, test.minute_of_day, ends))).numpy()
            now = _ffill(test.y)
            for j, h in enumerate(self.horizons):
                if h in out:
                    out[h][ends] = now[ends] + P[:, j] * self.sd[0]
        return out


def _GRUNet(n_in: int, hidden: int, n_out: int):
    import torch

    class Net(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.gru = torch.nn.GRU(n_in, hidden, batch_first=True)
            self.head = torch.nn.Linear(hidden, n_out)

        def forward(self, x):
            _, h = self.gru(x)
            return self.head(h[-1])

    return Net()


# ---------------------------------------------------------------------------- metrics
def mae_rmse(y_true: np.ndarray, y_pred: np.ndarray) -> Tuple[float, float, int]:
    ok = ~np.isnan(y_true) & ~np.isnan(y_pred)
    if not ok.any():
        return float("nan"), float("nan"), 0
    e = np.asarray(y_pred)[ok] - np.asarray(y_true)[ok]
    return float(np.mean(np.abs(e))), float(np.sqrt(np.mean(e ** 2))), int(ok.sum())


def smooth(y: np.ndarray, k: int = SMOOTH_MIN) -> np.ndarray:
    """Trailing k-minute mean (past only), NaN-aware."""
    y = np.asarray(y, float)
    return np.array([np.nanmean(y[max(0, i - k + 1):i + 1]) if not np.isnan(y[max(0, i - k + 1):i + 1]).all()
                     else np.nan for i in range(len(y))])


def congestion_state(occ: np.ndarray, threshold: float = CONGESTION_OCC, k: int = SMOOTH_MIN,
                     min_gap: int = ONSET_MIN_LEN) -> np.ndarray:
    """1 where the trailing k-minute mean is >= threshold (NaN stays 0); a dip shorter than `min_gap`
    minutes between two congested stretches counts as congested (one episode, not two)."""
    s = smooth(occ, k)
    st = np.where(np.isnan(s), 0, s >= threshold).astype(int)
    ones = np.nonzero(st)[0]
    for a, b in zip(ones[:-1], ones[1:]):
        if 1 < b - a <= min_gap:
            st[a:b] = 1
    return st


def onsets(state: np.ndarray, min_len: int = ONSET_MIN_LEN) -> List[int]:
    """Indices where the state switches 0 -> 1 and stays 1 for >= min_len minutes."""
    out = []
    n = len(state)
    for i in range(n):
        if state[i] == 1 and (i == 0 or state[i - 1] == 0):
            j = i
            while j < n and state[j] == 1:
                j += 1
            if j - i >= min_len:
                out.append(i)
    return out


def congestion_scores(actual_occ: np.ndarray, forecast: np.ndarray, h: int, threshold: float = CONGESTION_OCC,
                      origins: Optional[np.ndarray] = None) -> Dict:
    """Forecast made at t for t+h (forecast[t]) vs the actual congestion state at t+h.
    Minute level: precision / recall / F1 of "congested at t+h".
    Onsets (start of a congested episode, see congestion_state): the warning time is T - t for the first
    origin t, while the road was still free (state 0 at t), whose forecast called congestion for a time
    t+h >= T - 5; 0 = only noticed once it happened, None = never by T.
    False alarms: forecast switches to "congested" made while the road was free, for a time t+h that is
    neither congested nor within 5 minutes before an onset."""
    actual = congestion_state(actual_occ, threshold)
    n = len(actual)
    origins = np.arange(n) if origins is None else np.asarray(origins)
    origins = origins[origins + h < n]
    called = np.array([1 if (not np.isnan(forecast[t]) and forecast[t] >= threshold) else 0 for t in origins])
    act = actual[origins + h]
    tp = int(((called == 1) & (act == 1)).sum())
    fp = int(((called == 1) & (act == 0)).sum())
    fn = int(((called == 0) & (act == 1)).sum())
    prec = tp / (tp + fp) if tp + fp else float("nan")
    rec = tp / (tp + fn) if tp + fn else float("nan")
    f1 = 2 * prec * rec / (prec + rec) if tp and not math.isnan(prec + rec) else 0.0
    ons = onsets(actual)
    warn = []
    for T in ons:
        lead = None
        for t, c in zip(origins, called):
            if t > T:
                break
            if c and actual[t] == 0 and t + h >= T - 5:
                lead = int(T - t)
                break
        if lead is None and any(t == T for t in origins):
            lead = 0                                        # seen when it happened (no warning)
        warn.append({"onset_index": int(T), "warning_min": lead})
    alarms = [int(t) for (t, c), prev in zip(zip(origins[1:], called[1:]), called[:-1]) if c and not prev and actual[t] == 0]
    false_alarms = sum(1 for t in alarms if actual[t + h] == 0 and not any(T - 5 <= t + h <= T for T in ons))
    return {"precision": prec, "recall": rec, "f1": f1, "minutes": len(origins), "onsets": warn,
            "alarms": len(alarms), "false_alarms": false_alarms}


# ---------------------------------------------------------------------------- evaluation
def default_models(extra_keys: Sequence[str] = ()) -> List[Forecaster]:
    return [Persistence(), MovingAverage(15), DailyProfile(15), ProfileChange(15), ARIMAForecaster(),
            LightGBMForecaster(extra_keys), GRUForecaster(extra_keys)]


def evaluate_day_pair(train: Target, test: Target, horizons: Sequence[int] = HORIZONS,
                      models: Optional[List[Forecaster]] = None, warmup: int = 30,
                      congestion: Optional[float] = None, pool: Sequence[Target] = ()) -> Dict:
    """Fit every model on `train`, forecast `test`, and score every model on the SAME origins: t >= warmup
    where the target y(t+h) is known and every model has a forecast (the daily profile has none where the
    training day did not record that clock time). MAE / RMSE per horizon, plus congestion scores when
    `congestion` is a threshold. pool: more series of the training day for LightGBM / GRU."""
    models = models if models is not None else default_models(list(train.extra))
    n = len(test.y)
    preds = {}
    orders = {}
    for m in models:
        m.fit(train) if not isinstance(m, (LightGBMForecaster, GRUForecaster)) else m.fit(train, horizons, pool)
        preds[m.name] = m.forecast(test, horizons)
        if isinstance(m, ARIMAForecaster):
            orders[m.name] = list(m.order)
    out = {"models": {name: {} for name in preds}, "origins": {}}
    for h in horizons:
        tgt = future_target(test.y, h)
        o = np.arange(min(warmup, n), max(warmup, n - h))
        ok = ~np.isnan(tgt[o])
        for fc in preds.values():
            ok &= ~np.isnan(fc[h][o])
        o = o[ok]
        out["origins"][str(h)] = int(len(o))
        for name, fc in preds.items():
            mae, rmse, k = mae_rmse(tgt[o], fc[h][o])
            row = {"mae": mae, "rmse": rmse, "n": k}
            if congestion is not None:
                row["congestion"] = congestion_scores(test.y, fc[h], h, congestion, o)
            out["models"][name][str(h)] = row
    for name, order in orders.items():
        out["models"][name]["order"] = order
    out["predictions"] = {k: {str(h): [None if v != v else round(float(v), 5) for v in fc[h]] for h in horizons}
                          for k, fc in preds.items()}
    return out


def target_of(day: DaySeries, entity: str, measure: str, extra: Sequence[str] = ()) -> Target:
    return Target(day.get(entity, measure), day.minute_of_day,
                  {e: day.get(entity, e) for e in extra if f"{entity}.{e}" in day.columns})


def _fmt_table(res: Dict, horizons: Sequence[int]) -> str:
    lines = [f"{'model':<16}" + "".join(f"   MAE@{h:<3} RMSE@{h:<3}" for h in horizons)]
    for name, row in res["models"].items():
        lines.append(f"{name:<16}" + "".join(f"   {row[str(h)]['mae']:6.3f} {row[str(h)]['rmse']:7.3f}"
                                             for h in horizons))
    return "\n".join(lines)


def _day_arg(spec: str, default_entity: str) -> Tuple[DaySeries, str]:
    """'path.csv' or 'path.csv@entity' (another camera can name its carriageway differently)."""
    path, _, ent = spec.partition("@")
    return load_series(path), (ent or default_entity)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", nargs="+", required=True,
                    help="series CSVs (python -m alpr.series / alpr.congestion), >= 2; 'file.csv@entity' to name its entity")
    ap.add_argument("--entity", default="", help="lane or direction, e.g. 'To CIQ' (default for --days without @)")
    ap.add_argument("--measures", default="flow,occupancy", help="measures to forecast, e.g. excess,density")
    ap.add_argument("--extra", default="", help="other measures used as inputs (default: the other --measures)")
    ap.add_argument("--horizons", default="5,15,30")
    ap.add_argument("--congestion", type=float, default=CONGESTION_OCC, help="threshold for the congestion scores")
    ap.add_argument("--congestion-measure", default="occupancy", help="measure the threshold applies to")
    ap.add_argument("--warmup", type=int, default=30, help="first forecast origin (minutes into the test day)")
    ap.add_argument("--from", dest="t_from", default="", help="score origins from this clock time only (HH:MM)")
    ap.add_argument("--pool", default="", help="comma-separated entities of the TRAINING day pooled into the "
                    "LightGBM / GRU training data (same measure), e.g. the zones TC1,TC2,TC3,TC4")
    ap.add_argument("--out", help="write the full results (scores + forecasts) here")
    args = ap.parse_args(argv)
    horizons = [int(h) for h in args.horizons.split(",")]
    measures = args.measures.split(",")
    days = [_day_arg(d, args.entity) for d in args.days]
    results = {"horizons": horizons, "congestion_threshold": args.congestion,
               "congestion_measure": args.congestion_measure, "pairs": []}
    for i, (tr, etr) in enumerate(days):
        for j, (te, ete) in enumerate(days):
            if i == j:
                continue
            for meas in measures:
                extra = [m for m in (args.extra.split(",") if args.extra else measures) if m and m != meas]
                t_tr, t_te = target_of(tr, etr, meas, extra), target_of(te, ete, meas, extra)
                warm = args.warmup
                if args.t_from:
                    hh, mm = (int(v) for v in args.t_from.split(":"))
                    later = np.nonzero(te.minute_of_day >= hh * 60 + mm)[0]
                    warm = max(warm, int(later[0]) if len(later) else warm)
                pool = [target_of(tr, e, meas, extra) for e in args.pool.split(",")
                        if e and f"{e}.{meas}" in tr.columns]
                res = evaluate_day_pair(t_tr, t_te, horizons, warmup=warm,
                                        congestion=args.congestion if meas == args.congestion_measure else None,
                                        pool=pool)
                res["pool"] = [e for e in args.pool.split(",") if e]
                res.update({"train": f"{tr.name}@{etr}", "test": f"{te.name}@{ete}", "measure": meas,
                            "times": te.times, "measured": [None if v != v else float(v) for v in t_te.y]})
                results["pairs"].append(res)
                print(f"\n== {meas}: train {tr.name} ({etr}) -> test {te.name} ({ete}), origins per horizon {res['origins']}")
                print(_fmt_table(res, horizons))
                if meas == args.congestion_measure:
                    for name, row in res["models"].items():
                        c = [row[str(h)]["congestion"] for h in horizons]
                        print(f"   {name:<16}" + "  ".join(
                            f"@{h}: P {x['precision']:.2f} R {x['recall']:.2f} warn {[o['warning_min'] for o in x['onsets']]}"
                            for h, x in zip(horizons, c)))
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=1, default=float)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
