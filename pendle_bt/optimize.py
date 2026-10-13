"""Parameter search with walk-forward (expanding window) selection.

Every candidate parameter set is simulated once over the full period; because
the engine is causal, the NAV path up to any date is exactly what a run that
stopped on that date would have produced. For each fold we pick the candidate
with the best in-sample score on [start, train_end] and record its
out-of-sample return on (train_end, test_end], measured on liquidation NAV
(i.e. after the cost of exiting everything). The OOS segments are chained.
"""
import itertools
import json
import os
import random
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

from pendle_bt import panel as panel_mod
from pendle_bt.costs import CostModel
from pendle_bt.engine import Engine, Params
from pendle_bt.metrics import stats

START = "2024-01-01"
FOLDS = [  # (train_end, test_end)
    ("2024-12-31", "2025-06-30"),
    ("2025-06-30", "2025-12-31"),
    ("2025-12-31", "2026-06-30"),
    ("2026-06-30", "2026-10-12"),
]

SPACES = {
    "pt_naive": dict(instruments=[("pt",)], naive=[True], pt_exit=[-9.0],
                     pt_min_carry=[0.0, 0.02, 0.04], min_tvl=[1e6, 5e6],
                     min_under_tvl=[0.0, 2.5e7], max_frac_market=[0.34, 0.5, 1.0],
                     min_tau_pt=[21, 60]),
    "pt": dict(instruments=[("pt",)], forecast=["ema7", "ema30"], blend_w=[1.0, 0.5],
               achievable=["max_cash", "own"], pt_entry=[0.0, 0.01, 0.02, 0.04],
               pt_exit=[-0.03, -0.01, 0.0], pt_min_carry=[0.0, 0.02], min_tvl=[1e6, 5e6],
               min_under_tvl=[0.0, 2.5e7], max_frac_market=[0.34, 0.5, 1.0],
               min_tau_pt=[21, 60]),
    "yt": dict(instruments=[("yt",)], forecast=["ema7", "ema30"], blend_w=[1.0, 0.5],
               yt_entry=[0.0, 0.02, 0.05], yt_exit=[-0.02, 0.0], yt_max_frac=[0.1, 0.25, 0.5],
               max_yt_impact=[0.005, 0.01], min_tvl=[1e6, 5e6], min_tau_yt=[30, 60],
               max_frac_market=[0.25, 0.5], stop_loss_yt=[-0.5, -0.9]),
    "lp": dict(instruments=[("lp",)], forecast=["ema7", "ema30"], lp_entry=[0.0, 0.02, 0.04],
               lp_band=[0.02, 0.05, 1.0], lp_max_frac=[1.0], min_tvl=[1e6, 5e6],
               min_under_tvl=[0.0, 2.5e7], max_frac_market=[0.34, 0.5, 1.0],
               min_tau_pt=[21, 60]),
    "combo": dict(instruments=[("pt", "yt", "lp"), ("pt", "lp"), ("pt", "yt")],
                  forecast=["ema7", "ema30"], achievable=["max_cash"],
                  pt_entry=[0.0, 0.01, 0.02], pt_exit=[-0.03, -0.01], yt_entry=[0.02, 0.05],
                  yt_max_frac=[0.1, 0.2], lp_entry=[0.02, 0.04], lp_band=[0.02, 0.05],
                  lp_max_frac=[0.5], min_tvl=[1e6, 5e6], min_under_tvl=[0.0, 2.5e7],
                  max_frac_market=[0.34, 0.5], min_tau_pt=[21, 60]),
}

_G = {}


def _init(cost_kw, capital):
    df, sy = panel_mod.load()
    _G.update(df=df, sy=sy, cash=panel_mod.cash_apy(), costs=CostModel(**cost_kw),
              capital=capital)


def _run(pdict):
    p = Params(**pdict)
    e = Engine(_G["df"], _G["sy"], _G["cash"], _G["costs"], p, capital=_G["capital"],
               start=START)
    nav, trades, costs = e.run()
    return pdict, nav["nav_liq"], nav["nav_mid"], len(trades), costs


def sample(space, n, seed=0):
    keys = list(space)
    grid = list(itertools.product(*[space[k] for k in keys]))
    random.Random(seed).shuffle(grid)
    out = []
    for combo in grid[:n]:
        d = dict(zip(keys, combo))
        if "pt_exit" in d and "pt_entry" in d and d["pt_exit"] >= d["pt_entry"]:
            d["pt_exit"] = d["pt_entry"] - 0.01
        out.append(d)
    return out


def run_many(cands, cost_kw=None, capital=10_000.0, workers=4):
    with ProcessPoolExecutor(workers, initializer=_init, initargs=(cost_kw or {}, capital)) as ex:
        return list(ex.map(_run, cands, chunksize=1))


def score(nav, start, end):
    s = stats(nav, start, end)
    if s["days"] < 30 or not np.isfinite(s["cagr"]):
        return -9.0
    return s["cagr"] + 0.5 * s["maxdd"]          # maxdd is negative


def walk_forward(results):
    """results: list of (params, nav_liq, ...). Returns chained OOS NAV and picks."""
    picks, segs = [], []
    level = None
    for train_end, test_end in FOLDS:
        best = max(results, key=lambda r: score(r[1], START, train_end))
        nav = best[1]
        seg = nav[(nav.index >= pd.Timestamp(train_end)) & (nav.index <= pd.Timestamp(test_end))]
        seg = seg / seg.iloc[0]
        if level is None:
            level = 1.0
        segs.append(seg * level)
        level = (seg * level).iloc[-1]
        picks.append(dict(train_end=train_end, test_end=test_end,
                          is_score=score(nav, START, train_end),
                          oos=stats(nav, train_end, test_end), params=best[0]))
    oos = pd.concat(segs)
    oos = oos[~oos.index.duplicated(keep="last")]
    return oos, picks
