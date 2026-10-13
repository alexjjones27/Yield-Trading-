"""Empirical study: does implied APY over/under-shoot the yield later realised?

For every market-day with a yield-bearing underlying we compare the implied APY
with the SY's realised APY from that day to expiry (forward-looking, analysis
only), and bucket by the trailing spread that a live strategy could observe.
"""
import numpy as np
import pandas as pd

BUCKETS = [-1, -0.03, -0.01, 0.01, 0.03, 0.06, 1]


def dislocation_table(df, forecast="ema14", min_tvl=1e6, min_tau_days=14, asof=None):
    d = df[(df.tau > min_tau_days / 365) & (df.tvl > min_tvl) & df.fwd_real_apy.notna()].copy()
    if asof is not None:
        d = d[d.expiry_ts <= pd.Timestamp(asof)]
    d = d[d.underlyingApy > 0.005]                       # drop points-only / zero-yield tokens
    d["trail_spread"] = d.impliedApy - d[forecast]
    d["fwd_spread"] = d.impliedApy - d.fwd_real_apy
    d["bucket"] = pd.cut(d.trail_spread, BUCKETS)
    g = d.groupby("bucket", observed=True)
    out = pd.DataFrame({
        "market_days": g.size(),
        "markets": g.key.nunique(),
        "mean_fwd_spread": g.fwd_spread.mean(),
        "median_fwd_spread": g.fwd_spread.median(),
        "pt_beats_floating": g.fwd_spread.apply(lambda x: (x > 0).mean()),
    })
    summary = dict(market_days=len(d), markets=d.key.nunique(),
                   mean_fwd_spread=d.fwd_spread.mean(), median_fwd_spread=d.fwd_spread.median(),
                   pt_beats_floating=(d.fwd_spread > 0).mean(),
                   corr_trail_fwd=d[["trail_spread", "fwd_spread"]].corr().iloc[0, 1])
    return out, summary, d
