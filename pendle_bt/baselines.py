"""Passive benchmarks with the same cost model.

* cash:  USDC in Aave v3 (one deposit tx)
* floating: buy the underlying yield token (e.g. sUSDe) once, hold, sell at the end
"""
import pandas as pd

from pendle_bt.costs import CostModel


def cash_nav(cash_apy, dates, capital, costs: CostModel):
    nav, v = [], capital - costs.gas_usd(1, dates[0], "CASH_MOVE")
    for d in dates:
        a = cash_apy.get(pd.Timestamp(d), 0.0)
        v *= (1 + a) ** (1 / 365)
        nav.append(v)
    s = pd.Series(nav, index=pd.DatetimeIndex(dates))
    s.iloc[-1] -= costs.gas_usd(1, dates[-1], "CASH_MOVE")
    return s


def hold_floating(panel, sy_index, name, dates, capital, costs: CostModel, chain=1):
    """Buy-and-hold the yield token behind markets called ``name``.

    Pendle deploys a new SY contract for some maturities, so the token's growth
    is chained across every SY of that name: each day's return is the median
    across SYs observed on both days.
    """
    cand = panel[(panel.name == name) & (panel.chain == chain)]
    sys_ = cand.sy.unique()
    row = cand.iloc[0]
    sub = sy_index[sy_index.sy.isin(sys_)].copy()
    sub["val"] = sub["index"] * sub["asset_usd_sy"]
    sub = sub.sort_values(["sy", "date"])
    sub["ret"] = sub.groupby("sy")["val"].pct_change()
    daily = sub.dropna(subset=["ret"]).groupby("date")["ret"].median()
    daily = daily.reindex(pd.DatetimeIndex(dates)).fillna(0.0)
    first = daily.index[0]
    gas_in = costs.gas_usd(chain, first, "DEX_SWAP")
    v0 = (capital - gas_in) * (1 - costs.conv(row["input_tokens"]))
    nav = v0 * (1 + daily).cumprod()
    nav.iloc[-1] = nav.iloc[-1] * (1 - costs.conv(row["output_tokens"])) - costs.gas_usd(
        chain, nav.index[-1], "DEX_SWAP")
    return nav
