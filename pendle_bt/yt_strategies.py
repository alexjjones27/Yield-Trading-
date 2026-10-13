"""Classify real YT round trips into strategies and score them.

    python -m pendle_bt.yt_strategies

Each round trip (from ``yt_flows.round_trips``) is tagged by
  * holding style   flip (<1d), swing (1-7d), position (7d+, sold), to maturity
  * entry timing    launch (market <= 3 days old), late (<= 14 days to expiry)
  * entry premium   implied APY at entry minus the SY's trailing 30d realised
                    yield; a large premium means the buyer paid for something
                    other than yield (points / airdrops)
P&L is yield-only (points are not valued), in accounting-asset units (~USD).
"""
import os

import numpy as np
import pandas as pd

from pendle_bt import panel as panel_mod
from pendle_bt.yt_flows import load_trades, round_trips

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, "results")


def tag(rt, df):
    trail = df.set_index(["key", "date"])["ema30"]
    d = rt.t_in.dt.normalize()
    rt["trail_yield"] = [trail.get((k, x), np.nan) for k, x in zip(rt.key, d)]
    rt["premium"] = rt.implied_in - rt.trail_yield
    rt["style"] = np.select(
        [rt.to_maturity, rt.hold_days < 1, rt.hold_days < 7],
        ["held to maturity", "flip (<1 day)", "swing (1-7 days)"], "position (7+ days, sold)")
    rt["timing"] = np.select([rt.age_in <= 3, rt.days_to_exp_in <= 14],
                             ["launch (first 3 days)", "late (<=14d to expiry)"], "mid-life")
    rt["premium_bucket"] = pd.cut(rt.premium, [-9, -0.02, 0.02, 0.05, 0.15, 9],
                                  labels=["below yield (<-2pt)", "fair (+/-2pt)",
                                          "+2 to +5pt", "+5 to +15pt", "points-priced (>+15pt)"])
    return rt


def summarize(rt, by):
    g = rt.groupby(by, observed=True)
    out = pd.DataFrame({
        "round_trips": g.size(),
        "wallets": g.wallet.nunique(),
        "capital_in": g.cost.sum(),
        "pnl": g.pnl.sum(),
        "win_rate": g.pnl.apply(lambda x: (x > 0).mean()),
        "median_ret": g.ret.median(),
    })
    out["ret_on_capital"] = out.pnl / out.capital_in
    return out


def wallets(rt):
    w = rt.groupby("wallet").agg(trips=("pnl", "size"), markets=("key", "nunique"),
                                 capital=("cost", "sum"), pnl=("pnl", "sum"),
                                 med_hold=("hold_days", "median"),
                                 share_flip=("style", lambda s: (s == "flip (<1 day)").mean()),
                                 share_maturity=("style", lambda s: (s == "held to maturity").mean()),
                                 med_premium=("premium", "median"))
    w["ret"] = w.pnl / w.capital
    w["profile"] = np.select(
        [(w.trips >= 50) & (w.share_flip >= 0.5), w.share_maturity >= 0.7,
         w.med_premium > 0.15, w.med_hold < 7],
        ["high-frequency flipper", "buy & hold to maturity", "points farmer (premium >15pt)",
         "short-term trader"], "swing / position trader")
    return w


def main():
    df, sy = panel_mod.load()
    tr = load_trades(df)
    rt = tag(round_trips(tr, sy), df)
    os.makedirs(RES, exist_ok=True)
    rt.to_pickle(os.path.join(RES, "yt_round_trips.pkl"))
    w = wallets(rt)
    w.to_csv(os.path.join(RES, "yt_wallets.csv"))
    pd.set_option("display.width", 220)
    pd.set_option("display.float_format", lambda x: f"{x:,.3f}")
    print(f"trades {len(tr):,}  markets {tr.key.nunique()}  wallets {tr.wallet.nunique():,}  "
          f"round trips {len(rt):,}")
    print(f"unmatched SELL_YT notional (YT that came from minting, i.e. yield sellers): "
          f"{tr[tr.side == 'SELL_YT'].notional.sum():,.0f} sold vs "
          f"{tr[tr.side == 'BUY_YT'].notional.sum():,.0f} bought")
    for by in ("style", "timing", "premium_bucket"):
        print("\n", summarize(rt, by).to_string())
    print("\n", summarize(rt, ["premium_bucket", "style"]).to_string())
    pw = w.groupby("profile").agg(wallets=("pnl", "size"), capital=("capital", "sum"),
                                  pnl=("pnl", "sum"),
                                  profitable=("pnl", lambda x: (x > 0).mean()),
                                  median_ret=("ret", "median"))
    pw["ret_on_capital"] = pw.pnl / pw.capital
    print("\n", pw.to_string())
    print("\nTop 15 wallets by yield-only P&L:\n",
          w.sort_values("pnl", ascending=False).head(15).to_string())
    return tr, rt, w


if __name__ == "__main__":
    main()


def pendle_positions():
    """Per wallet-market P&L from Pendle's records, corrected for expired YT.

    Pendle's netGain books claimed yield and realised sale gains but never writes off
    the cost of YT still held when it expires worthless, so for expired markets
    true P&L = netGain - open YT cost basis. Only pure-YT positions (no PT or LP
    left) are kept; the denominator is Pendle's maxCapital."""
    import json
    from pendle_bt.yt_flows import PNL_DIR
    rows = []
    for f in os.listdir(PNL_DIR):
        for p in json.load(open(os.path.join(PNL_DIR, f))).get("positions", []):
            rows.append(dict(wallet=f[:-5], key=f"{p['chainId']}-{p['market']}",
                             net_gain=p["pnl"]["netGain"]["usd"],
                             max_capital=p["pnl"]["maxCapital"]["usd"],
                             yt_open_cost=p["ytData"]["spent_v2"]["usd"],
                             pt_units=p["ptData"]["unit"], lp_units=p["lpData"]["unit"]))
    pn = pd.DataFrame(rows)
    pn = pn[(pn.pt_units == 0) & (pn.lp_units == 0) & (pn.max_capital > 100)]
    pn["true_pnl"] = pn.net_gain - pn.yt_open_cost
    return pn


def pendle_scored(rt):
    """Score strategies on corrected Pendle P&L for pure-YT positions in expired markets."""
    pn = pendle_positions()
    feats = rt.groupby(["wallet", "key"]).agg(
        yield_pnl=("pnl", "sum"), premium=("premium", "median"),
        share_maturity=("to_maturity", "mean"), t_in=("t_in", "min"), name=("name", "first"))
    m = pn.merge(feats.reset_index(), on=["wallet", "key"], how="inner")
    m["market_pricing"] = pd.cut(m.premium, [-9, 0.02, 0.05, 0.15, 9],
                                 labels=["at/below yield (<=2pt)", "+2 to +5pt", "+5 to +15pt",
                                         "points-priced (>15pt)"])
    m["exit"] = np.where(m.yt_open_cost > 1, "held to expiry", "fully exited before expiry")
    m["episode"] = np.where((m.name == "sUSDe") & m.t_in.between("2024-09-01", "2024-11-30"),
                            "sUSDe YT bought Sep-Nov 2024", "everything else")
    m["year"] = m.t_in.dt.year

    def agg(by):
        g = m.groupby(by, observed=True)
        o = pd.DataFrame({"positions": g.size(), "wallets": g.wallet.nunique(),
                          "capital": g.max_capital.sum(), "pnl": g.true_pnl.sum(),
                          "profitable": g.true_pnl.apply(lambda x: (x > 0).mean()),
                          "median_ret": (g.true_pnl.sum() * 0 + g.apply(
                              lambda x: (x.true_pnl / x.max_capital).median()))})
        o["return"] = o.pnl / o.capital
        return o
    return m, {k: agg(k) for k in ("market_pricing", "exit", "episode", "year")} | {
        "pricing x exit": agg(["market_pricing", "exit"])}
