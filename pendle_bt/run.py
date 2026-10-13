"""End-to-end run: study, baselines, search, walk-forward, stress tests, charts.

    python -m pendle_bt.run            # full search (~20-30 min on 4 cores)
    python -m pendle_bt.run --quick    # small search for a smoke test
"""
import argparse
import json
import os
import time

import numpy as np
import pandas as pd

from pendle_bt import optimize as opt
from pendle_bt import panel as panel_mod
from pendle_bt.baselines import cash_nav, hold_floating
from pendle_bt.costs import CostModel
from pendle_bt.engine import Engine, Params
from pendle_bt.metrics import excess_sharpe, stats
from pendle_bt.study import dislocation_table

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "results")
CAPITAL = 10_000.0
N_SAMPLES = {"pt_naive": 72, "pt": 140, "yt": 70, "lp": 90, "combo": 110}


def run_one(df, sy, cash, p, cost_kw=None, capital=CAPITAL, start=opt.START):
    e = Engine(df, sy, cash, CostModel(**(cost_kw or {})), p, capital=capital, start=start)
    nav, trades, costs = e.run()
    return nav, trades, costs


def fmt_stats(s):
    return {k: (round(float(v), 4) if isinstance(v, (float, np.floating)) else v)
            for k, v in s.items()}


def main(quick=False):
    t0 = time.time()
    os.makedirs(OUT, exist_ok=True)
    df, sy = panel_mod.build()
    cash = panel_mod.cash_apy()
    costs = CostModel()
    summary = {"universe": dict(markets=int(df.key.nunique()), market_days=int(len(df)),
                                first=str(df.date.min().date()), last=str(df.date.max().date()))}

    # ---------------------------------------------------------------- study
    tab, s, _ = dislocation_table(df, asof=df.date.max())
    tab.to_csv(os.path.join(OUT, "dislocation_study.csv"))
    summary["study"] = fmt_stats(s)
    print(tab.round(4).to_string(), "\n", s, flush=True)

    # ---------------------------------------------------------------- baselines
    dates = sorted(df[df.date >= pd.Timestamp(opt.START)].date.unique())
    base = {"Cash (Aave USDC)": cash_nav(cash, dates, CAPITAL, costs),
            "Hold sUSDe": hold_floating(df, sy, "sUSDe", dates, CAPITAL, costs)}

    # ---------------------------------------------------------------- search
    n = {k: (8 if quick else v) for k, v in N_SAMPLES.items()}
    allres, wf = {}, {}
    for fam, space in opt.SPACES.items():
        cands = opt.sample(space, n[fam], seed=1)
        t = time.time()
        res = opt.run_many(cands)
        allres[fam] = res
        oos, picks = opt.walk_forward(res)
        wf[fam] = (oos, picks)
        best_full = max(res, key=lambda r: opt.score(r[1], opt.START, None))
        print(f"{fam}: {len(res)} runs in {time.time() - t:.0f}s | OOS chained "
              f"{oos.iloc[-1] - 1:+.4f} | best full-period IS "
              f"{best_full[1].iloc[-1] / CAPITAL - 1:+.4f}", flush=True)
        pd.DataFrame({i: r[1] for i, r in enumerate(res)}).to_pickle(
            os.path.join(OUT, f"search_navs_{fam}.pkl"))
        pd.DataFrame([dict(**r[0], final_liq=r[1].iloc[-1], trades=r[3],
                           **{f"cost_{k}": v for k, v in r[4].items()},
                           **{f"full_{k}": v for k, v in stats(r[1]).items()})
                      for r in res]).to_csv(os.path.join(OUT, f"search_{fam}.csv"), index=False)

    # ---------------------------------------------------------------- OOS summary
    oos_start = pd.Timestamp(opt.FOLDS[0][0])
    curves = {}
    for name, nav in base.items():
        seg = nav[nav.index >= oos_start]
        curves[name] = seg / seg.iloc[0]
    label = {"pt_naive": "PT carry (no signal)", "pt": "PT dislocation", "yt": "YT dislocation",
             "lp": "LP carry", "combo": "Combined PT/YT/LP"}
    for fam, (oos, picks) in wf.items():
        curves[label[fam]] = oos
    table = {}
    for name, c in curves.items():
        st = stats(c)
        st["excess_sharpe"] = excess_sharpe(c, cash)
        table[name] = fmt_stats(st)
    summary["oos"] = table
    summary["oos_window"] = [str(oos_start.date()), opt.FOLDS[-1][1]]
    summary["picks"] = {fam: [dict(p, params={k: v for k, v in p["params"].items()},
                                   oos=fmt_stats(p["oos"])) for p in picks]
                        for fam, (_, picks) in wf.items()}
    pd.DataFrame(curves).to_csv(os.path.join(OUT, "oos_equity.csv"))
    print(pd.DataFrame(table).T.round(4).to_string(), flush=True)

    # ---------------------------------------------------------------- robustness across parameters
    # OOS-window return of EVERY candidate (not tradable as such: shows how much the
    # family's result depends on picking the right parameters)
    sus = base["Hold sUSDe"]
    sus_ret = stats(sus, oos_start, None)["ret"]
    cash_ret = stats(base["Cash (Aave USDC)"], oos_start, None)["ret"]
    dist = {}
    for fam, res in allres.items():
        rr = np.array([stats(r[1], oos_start, None)["ret"] for r in res])
        dd = np.array([stats(r[1], oos_start, None)["maxdd"] for r in res])
        dist[label[fam]] = dict(n=len(rr), median=float(np.median(rr)),
                                p10=float(np.percentile(rr, 10)), p90=float(np.percentile(rr, 90)),
                                worst=float(rr.min()), best=float(rr.max()),
                                beat_cash=float((rr > cash_ret).mean()),
                                beat_susde=float((rr > sus_ret).mean()),
                                median_maxdd=float(np.median(dd)), worst_maxdd=float(dd.min()))
    summary["oos_distribution"] = dist
    print(pd.DataFrame(dist).T.round(4).to_string(), flush=True)

    # ---------------------------------------------------------------- recommended config
    # the family is fixed a priori (the requested PT dislocation strategy), not picked on OOS
    best_fam = "pt"
    rec_params = wf[best_fam][1][-1]["params"]          # most recent fold's selection
    summary["recommended"] = dict(family=best_fam, params=rec_params)
    p = Params(**rec_params)
    nav, trades, cst = run_one(df, sy, cash, p)
    trades.to_csv(os.path.join(OUT, "recommended_trades.csv"), index=False)
    nav.to_csv(os.path.join(OUT, "recommended_nav.csv"))
    summary["recommended"]["full_period"] = fmt_stats(stats(nav["nav_liq"]))
    summary["recommended"]["oos_window"] = fmt_stats(stats(nav["nav_liq"], opt.FOLDS[0][0], None))
    summary["recommended"]["costs"] = {k: round(float(v), 2) for k, v in cst.items()}
    summary["recommended"]["n_trades"] = int(len(trades))

    # ---------------------------------------------------------------- stress tests
    oos0 = opt.FOLDS[0][0]

    def both(nv):
        return dict(full=fmt_stats(stats(nv["nav_liq"])),
                    oos=fmt_stats(stats(nv["nav_liq"], oos0, None)))

    # risk-capped variants of the recommended config (constraints, not optimised)
    variants = {
        "as selected": {},
        "max 25% per market": {"max_frac_market": 0.25},
        "core underlyings only": {"min_under_tvl": 2.5e7},
        "max 25% per market + core only": {"max_frac_market": 0.25, "min_under_tvl": 2.5e7},
    }
    summary["risk_variants"] = {}
    for name, kw in variants.items():
        nv, tr, cs = run_one(df, sy, cash, Params(**{**rec_params, **kw}))
        summary["risk_variants"][name] = dict(**both(nv), trades=int(len(tr)))
        nv.to_csv(os.path.join(OUT, f"variant_{name.replace(' ', '_').replace('%', 'pct').replace('+', 'and')}.csv"))
    print(json.dumps(summary["risk_variants"], indent=1), flush=True)

    stress = {}
    scen = {
        "base": ({}, {}),
        "gas x2": ({"gas_mult": 2.0}, {}),
        "conversion x2": ({"conv_mult": 2.0}, {}),
        "gas x2 + conv x2": ({"gas_mult": 2.0, "conv_mult": 2.0}, {}),
        "trade on yesterday's signal": ({}, {"lag": 1}),
        "Ethereum only": ({}, {"chains": (1,)}),
        "core underlyings only (>$25M on Pendle)": ({}, {"min_under_tvl": 2.5e7}),
    }
    for name, (ckw, pkw) in scen.items():
        pp = Params(**{**rec_params, **pkw})
        nv, tr, cs = run_one(df, sy, cash, pp, cost_kw=ckw)
        stress[name] = dict(**both(nv), trades=int(len(tr)), gas=round(cs["gas"], 2))
    sizes = {}
    for cap in (2_500, 10_000, 50_000, 250_000, 1_000_000):
        nv, tr, cs = run_one(df, sy, cash, p, capital=cap)
        sizes[cap] = dict(**fmt_stats(stats(nv["nav_liq"])),
                          oos=fmt_stats(stats(nv["nav_liq"], oos0, None)),
                          cost_pct={k: round(v / cap, 5) for k, v in cs.items()})
    summary["stress"] = stress
    summary["capital_sizes"] = sizes
    print(json.dumps(stress, indent=1), json.dumps(sizes, indent=1), flush=True)

    # baselines full period
    summary["baselines_full"] = {k: fmt_stats(stats(v)) for k, v in base.items()}
    pd.DataFrame(base).to_csv(os.path.join(OUT, "baselines.csv"))
    json.dump(summary, open(os.path.join(OUT, "summary.json"), "w"), indent=1, default=str)
    print(f"done in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    main(**vars(ap.parse_args()))
