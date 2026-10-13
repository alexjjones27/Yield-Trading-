"""Build the cleaned daily market panel used by the backtest.

Per market-day (00:00 UTC snapshot) we keep pool reserves, implied rate, prices
and derive:
  * ``asset_usd``  USD price of the accounting asset, backed out of the PT price
                    and the implied rate (PT_usd = asset_usd * exp(-ln(1+r) * tau))
  * ``index``      SY -> asset exchange rate (syPrice / asset_usd)

Per SY token we build one continuous daily index series (median across all
maturities that share the SY), then
  * ``py_index``   running max of the index (Pendle's PY index never decreases)
  * ``fwd``        trailing realised yield EMAs used as the floating forecast
Everything trailing only uses data up to and including the current day.
"""
import glob
import json
import math
import os

import numpy as np
import pandas as pd

from pendle_bt.fetch_history import RAW

ROOT = os.path.dirname(RAW)
PROC = os.path.join(ROOT, "processed")
YEAR_DAYS = 365.0


def _load_raw():
    meta = {f"{m['chainId']}-{m['address']}": m
            for m in json.load(open(os.path.join(RAW, "markets_all.json")))}
    params = json.load(open(os.path.join(RAW, "market_params.json")))
    rows = []
    for f in glob.glob(os.path.join(RAW, "history", "*.json")):
        key = os.path.basename(f)[:-5]
        m = meta[key]
        p = params.get(key)
        if not p or "scalar_root" not in p:
            continue
        for r in json.load(open(f))["results"]:
            r.update(key=key, name=m["name"], chain=m["chainId"], expiry=m["expiry"],
                     sy=m["sy"], start=m["timestamp"],
                     input_tokens=",".join(m.get("inputTokens", [])),
                     output_tokens=",".join(m.get("outputTokens", [])),
                     underlying=m["underlyingAsset"],
                     scalar_root=p["scalar_root"], ln_fee_root=p.get("ln_fee_root", 0.001),
                     reserve_fee_pct=p.get("reserve_fee_pct", 80),
                     interest_fee=p.get("interest_fee", 0.05))
            rows.append(r)
    return pd.DataFrame(rows)


def build(save=True):
    df = _load_raw()
    df["date"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None).dt.normalize()
    df["expiry_ts"] = pd.to_datetime(df["expiry"]).dt.tz_localize(None)
    df["start_ts"] = pd.to_datetime(df["start"]).dt.tz_localize(None)
    df["tau"] = (df["expiry_ts"] - df["date"]).dt.total_seconds() / (YEAR_DAYS * 86400)
    df["age_days"] = (df["date"] - df["start_ts"].dt.normalize()).dt.days

    # basic sanity filters on the snapshot
    ok = ((df.tau > 0) & df.impliedApy.between(-0.5, 3.0) & (df.totalPt > 0) & (df.totalSy > 0)
          & (df.tvl > 10) & (df.ptPrice > 0) & (df.syPrice > 0))
    df = df[ok].copy()

    df["ln_implied"] = np.log1p(df.impliedApy)
    df["pt_mid"] = np.exp(-df.ln_implied * df.tau)         # asset terms
    df["asset_usd"] = df.ptPrice / df.pt_mid
    df["index_raw"] = df.syPrice / df.asset_usd

    # asset/USD sanity: keep depegs (down to 5 cents) so collapses are marked, drop garbage
    df = df[df.asset_usd.between(0.05, 1.5)]

    # ---- continuous index per SY (median across maturities sharing the SY)
    sy = (df.groupby(["sy", "date"]).agg(index=("index_raw", "median"),
                                         asset_usd_sy=("asset_usd", "median"),
                                         und_apy=("underlyingApy", "median"))
          .reset_index().sort_values(["sy", "date"]))
    out = []
    for s, g in sy.groupby("sy"):
        g = g.set_index("date").asfreq("D")
        g["sy"] = s
        g["index"] = g["index"].interpolate(limit=5)
        g["und_apy"] = g["und_apy"].ffill(limit=5)
        g["asset_usd_sy"] = g["asset_usd_sy"].ffill(limit=5)
        # de-noise the price-derived index: 3-day rolling median, then monotone
        sm = g["index"].rolling(3, min_periods=1, center=False).median()
        g["py_index"] = sm.cummax()
        r = np.log(sm).diff() * YEAR_DAYS                  # daily realised (cont.) rate
        g["r_real"] = r.clip(-1, 3)
        for hl in (3, 7, 14, 30, 60):
            g[f"ema{hl}"] = np.expm1(g["r_real"].ewm(halflife=hl, min_periods=2).mean())
        g["mean90"] = np.expm1(g["r_real"].rolling(90, min_periods=10).mean())
        g["days_hist"] = np.arange(len(g))
        out.append(g.reset_index())
    sy = pd.concat(out, ignore_index=True)
    df = df.merge(sy, on=["sy", "date"], how="left")

    # realised forward yield of the SY to the market's expiry (analysis only, never a signal)
    idx = sy.set_index(["sy", "date"])["py_index"]
    exp_day = df.expiry_ts.dt.normalize()
    df["idx_at_expiry"] = [idx.get((s, d), np.nan) for s, d in zip(df.sy, exp_day)]
    df["fwd_real_apy"] = (df.idx_at_expiry / df.py_index) ** (1 / df.tau) - 1

    # trailing LP incentive / fee APYs (7d medians per market, today inclusive)
    df = df.sort_values(["key", "date"])
    g = df.groupby("key")
    df["pendle7"] = g["pendleApy"].transform(lambda x: x.rolling(7, min_periods=1).median())
    df["swapfee7"] = g["swapFeeApy"].transform(lambda x: x.rolling(7, min_periods=1).median())

    # ex-ante size of each underlying on Pendle: total TVL across its live maturities today
    df["under_tvl"] = df.groupby(["underlying", "date"])["totalTvl"].transform("sum")

    cols = ["key", "name", "chain", "sy", "underlying", "input_tokens", "output_tokens",
            "date", "expiry_ts", "tau", "age_days",
            "impliedApy", "ln_implied", "underlyingApy", "underlyingInterestApy",
            "underlyingRewardApy", "totalPt", "totalSy", "totalSupply", "tvl", "totalTvl",
            "tradingVolume", "swapFeeApy", "pendleApy", "pendle7", "swapfee7", "under_tvl", "lpPrice", "ptPrice", "ytPrice",
            "syPrice", "pt_mid", "asset_usd", "index_raw", "index", "py_index", "r_real",
            "ema3", "ema7", "ema14", "ema30", "ema60", "mean90", "days_hist",
            "scalar_root", "ln_fee_root", "reserve_fee_pct", "interest_fee",
            "idx_at_expiry", "fwd_real_apy"]
    df = df[cols].sort_values(["date", "key"]).reset_index(drop=True)
    if save:
        os.makedirs(PROC, exist_ok=True)
        df.to_pickle(os.path.join(PROC, "panel.pkl"))
        sy.to_pickle(os.path.join(PROC, "sy_index.pkl"))
    return df, sy


def load():
    return (pd.read_pickle(os.path.join(PROC, "panel.pkl")),
            pd.read_pickle(os.path.join(PROC, "sy_index.pkl")))


if __name__ == "__main__":
    d, s = build()
    print(d.shape, d.key.nunique(), d.date.min(), d.date.max())


def cash_apy():
    """Daily Aave v3 USDC (Ethereum) supply APY as a fraction, forward filled."""
    d = pd.DataFrame(json.load(open(os.path.join(RAW, "aave_v3_usdc_eth.json"))))
    d["date"] = pd.to_datetime(d["timestamp"]).dt.tz_localize(None).dt.normalize()
    s = d.groupby("date")["apy"].median() / 100.0
    return s.asfreq("D").ffill()
