"""PT looping (leveraged fixed yield) backtest on real Morpho markets.

A loop with equity C at leverage L:
  open    buy PT worth L*C through the Pendle AMM (exact math, USDC->underlying
          conversion), borrow (L-1)*C from the cheapest Morpho market that accepts
          that PT as collateral and has the liquidity; one tx (~1.23M gas measured)
  daily   debt accrues at that market's actual daily borrow APY; collateral is
          valued the way that market's oracle values it:
            linear   price = 1 - discount * time_to_expiry  (x underlying USD price)
            market   Pendle PT TWAP ~ AMM price             (x underlying USD price)
          health = collateral * LLTV / debt; below 1 the position is liquidated
          with Morpho's incentive (LIF = min(1.15, 1/(0.3*LLTV + 0.7)))
  close   at maturity: redeem PT, convert to the loan asset, repay, ~0.97M gas;
          optionally unwind early through the AMM when borrowing costs more than
          the PT still earns
"""
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from pendle_bt import amm
from pendle_bt.costs import CostModel

YEAR_S = 365 * 86400
GAS_LOOP_IN = 1_230_000      # median of real Morpho borrow txs with swaps (PT markets, Ethereum)
GAS_LOOP_OUT = 970_000       # median of real repay/unwind txs with swaps
STABLE_LOANS = {"USDC", "USDT", "DAI", "USDS", "PYUSD"}


def lif(lltv):
    return min(1.15, 1.0 / (0.3 * lltv + 0.7))


@dataclass
class LoopParams:
    leverage: float = 3.0
    min_exp_apy: float = -1.0        # require expected equity APY after all costs >= this
    lev_of_max: float = 0.0          # if > 0, use this fraction of the max safe leverage instead
    min_hf: float = 1.05             # never open below this health factor
    min_spread: float = 0.0          # require (PT all-in APY - borrow APY 7d) >= this
    min_tau_days: int = 30
    min_pool_tvl: float = 1_000_000
    unwind_if_negative_days: int = 0  # > 0: unwind when borrow > remaining PT yield this many days
    allow_oracles: tuple = ("linear", "market", "unknown")


class LoopSim:
    def __init__(self, panel, sy_index, venues, history, costs: CostModel):
        self.c = costs
        self.panel = panel
        self.data_end = panel.date.max()
        self.rows = {k: g.set_index("date").sort_index() for k, g in panel.groupby("key")}
        self.sy = sy_index.set_index(["sy", "date"])
        v = venues[venues.loan.isin(STABLE_LOANS)].copy()
        self.venues = {k: g for k, g in v.groupby("key")}
        h = history.copy()
        h["b7"] = h.groupby("market_id").borrow_apy.transform(
            lambda x: x.rolling(7, min_periods=1).mean())
        self.hist = {m: g.set_index("date").sort_index() for m, g in h.groupby("market_id")}

    # ------------------------------------------------------------- helpers
    def gas(self, chain, date, units):
        if chain != 1:
            return self.c.l2_tx_usd
        return units * self.c.usd_per_gas(date) * self.c.gas_mult

    def borrow(self, mid, date):
        h = self.hist.get(mid)
        if h is None:
            return None
        s = h.loc[:date]
        if s.empty:
            return None
        row = s.iloc[-1]
        if (date - s.index[-1]).days > 5:
            return None
        return row

    def oracle_px(self, venue, r, date):
        """Collateral price per PT unit in USD, as the lending market's oracle sees it."""
        tau = max((r["expiry_ts"] - date).total_seconds() / YEAR_S, 0.0)
        if venue.oracle == "linear" and venue.linear_discount == venue.linear_discount:
            px = max(1.0 - venue.linear_discount * tau, 0.0)
        else:
            px = math.exp(-r["ln_implied"] * tau)
        return px * r["asset_usd"]

    def state(self, r):
        return amm.PoolState(r["totalPt"], r["totalSy"], max(r["index"], r["py_index"]),
                             r["ln_implied"], r["expiry_ts"].timestamp(), r["scalar_root"],
                             r["ln_fee_root"], r["reserve_fee_pct"])

    def pick_venue(self, key, date, debt_usd, p: LoopParams):
        vs = self.venues.get(key)
        if vs is None:
            return None, None
        best = None
        for v in vs.itertuples():
            if v.created > date or v.oracle not in p.allow_oracles:
                continue
            b = self.borrow(v.market_id, date)
            if b is None or (b.liquidity_usd or 0) < debt_usd * 1.5 or b.borrow_apy > 1.0:
                continue
            if best is None or b.b7 < best[1].b7:
                best = (v, b)
        return best if best else (None, None)

    # ------------------------------------------------------------- one trade
    def open_trade(self, key, date, capital, p: LoopParams):
        g = self.rows.get(key)
        if g is None or date not in g.index:
            return None
        r = g.loc[date].to_dict()
        r["expiry_ts"] = g.loc[date, "expiry_ts"]
        tau_days = (r["expiry_ts"] - date).days
        if tau_days < p.min_tau_days or r["tvl"] < p.min_pool_tvl or r["impliedApy"] <= 0:
            return None
        lev = p.leverage
        v, b = self.pick_venue(key, date, capital * (lev - 1), p)
        if v is None:
            return None
        looped = (p.leverage > 1.01) or p.lev_of_max > 0
        gas_in = self.gas(r["chain"], date, GAS_LOOP_IN if looped else self.c.gas_units["BUY_PT"])
        conv_in = self.c.conv(r["input_tokens"])
        conv_out = self.c.conv(r["output_tokens"])

        def build(L):
            eq = capital - gas_in
            if eq <= 0:
                return None
            notional = eq * L
            asset_in = notional * (1 - conv_in) / r["asset_usd"]
            sy_in = asset_in / r["index"]
            try:
                units, _, _ = amm.swap_exact_sy_for_pt(self.state(r), date.timestamp(), sy_in)
            except amm.TradeError:
                return None
            units *= 1 - self.c.router_eps
            debt = eq * (L - 1)
            hf = units * self.oracle_px(v, r, date) * v.lltv / debt if debt > 0 else 9.9
            return units, debt, hf, eq

        if p.lev_of_max > 0:          # search the max leverage that clears min_hf
            lo, hi = 1.0, 1.0 / (1.0 - v.lltv) * 0.999
            for _ in range(30):
                mid = 0.5 * (lo + hi)
                t = build(mid)
                if t and t[2] >= p.min_hf:
                    lo = mid
                else:
                    hi = mid
            lev = 1 + (lo - 1) * p.lev_of_max
        t = build(lev)
        if t is None or t[2] < p.min_hf:
            return None
        units, debt, hf, eq = t
        # expected all-in fixed APY on the PT leg and the carry at this leverage
        pt_apy = (units * r["asset_usd"] * (1 - conv_out) / (eq * lev)) ** (365 / tau_days) - 1
        spread = pt_apy - b.b7
        if spread < p.min_spread:
            return None
        gas_out = self.gas(r["chain"], date, GAS_LOOP_OUT if looped else self.c.gas_units["REDEEM_PT"])
        exp_end = (units * r["asset_usd"] * (1 - conv_out)
                   - debt * (1 + b.b7) ** (tau_days / 365) - gas_out)
        exp_apy = (max(exp_end, 1e-9) / capital) ** (365 / tau_days) - 1
        if exp_apy < p.min_exp_apy:
            return None
        return dict(key=key, name=r["name"], chain=r["chain"], venue=v.market_id, looped=looped,
                    oracle=v.oracle, lltv=v.lltv, open=date, expiry=r["expiry_ts"],
                    leverage=lev, capital=capital, equity0=eq, units=units, debt=debt,
                    hf0=hf, pt_apy=pt_apy, borrow0=b.borrow_apy, spread0=spread, exp_apy=exp_apy,
                    gas_in=gas_in, sy=r["sy"], output_tokens=r["output_tokens"], v=v)

    def run_trade(self, t, p: LoopParams):
        g = self.rows[t["key"]]
        v = t["v"]
        debt, units = t["debt"], t["units"]
        neg = 0
        last_r = None
        min_hf = t["hf0"]
        path = [(t["open"], t["equity0"])]
        t = dict(t, path=path)
        stop = min(t["expiry"], self.data_end)
        for date in pd.date_range(t["open"] + pd.Timedelta(days=1), stop, freq="D"):
            b = self.borrow(v.market_id, date)
            apy = b.borrow_apy if b is not None else t["borrow0"]
            debt *= (1 + min(apy, 5.0)) ** (1 / 365)
            if date >= t["expiry"]:
                break
            if date in g.index:
                last_r = g.loc[date].to_dict()
                last_r["expiry_ts"] = t["expiry"]
            r = last_r
            if r is None:
                continue
            hf = units * self.oracle_px(v, r, date) * v.lltv / debt
            min_hf = min(min_hf, hf)
            tau = max((t["expiry"] - date).total_seconds() / YEAR_S, 0.0)
            mark = (units * math.exp(-r["ln_implied"] * tau) * r["asset_usd"]
                    * (1 - self.c.conv(t["output_tokens"])) - debt)
            path.append((date, mark))
            if hf < 1.0:                                   # liquidated
                coll = units * self.oracle_px(v, r, date)
                left = max(coll - debt * lif(v.lltv), 0.0)
                left *= 1 - self.c.conv(t["output_tokens"]) - 0.01   # sell leftover PT
                left -= self.gas(t["chain"], date, 500_000)
                return dict(t, close=date, outcome="liquidated", equity_end=max(left, 0.0),
                            min_hf=hf, debt_end=debt)
            if p.unwind_if_negative_days and b is not None:
                tau = (t["expiry"] - date).total_seconds() / YEAR_S
                rem = math.expm1(r["ln_implied"])          # what the PT still earns
                neg = neg + 1 if b.b7 > rem else 0
                if neg >= p.unwind_if_negative_days and tau > 7 / 365:
                    try:
                        sy_out, _, _ = amm.swap_exact_pt_for_sy(self.state(r), date.timestamp(),
                                                                units)
                    except amm.TradeError:
                        continue
                    val = sy_out * r["index"] * r["asset_usd"] * (1 - self.c.conv(t["output_tokens"]))
                    eq = val - debt - self.gas(t["chain"], date, GAS_LOOP_OUT)
                    return dict(t, close=date, outcome="unwound (negative carry)",
                                equity_end=max(eq, 0.0), min_hf=min_hf, debt_end=debt)
        if t["expiry"] > self.data_end:             # still open when the data ends: mark at
            r = last_r                                # what unwinding through the AMM returns
            try:
                sy_out, _, _ = amm.swap_exact_pt_for_sy(self.state(r), self.data_end.timestamp(),
                                                        units)
                val = sy_out * r["index"] * r["asset_usd"] * (1 - self.c.conv(t["output_tokens"]))
            except (amm.TradeError, TypeError):
                tau = (t["expiry"] - self.data_end).total_seconds() / YEAR_S
                val = units * math.exp(-r["ln_implied"] * tau) * r["asset_usd"] * 0.99
            eq = val - debt - self.gas(t["chain"], self.data_end, GAS_LOOP_OUT)
            return dict(t, close=self.data_end, outcome="open (marked at data end)",
                        equity_end=max(eq, 0.0), min_hf=min_hf, debt_end=debt)
        # maturity: redeem PT 1:1 in asset, convert, repay
        sy_pt = self.sy.loc[t["sy"]].loc[:t["expiry"]]
        last = sy_pt.iloc[-1] if len(sy_pt) else None
        asset_usd = last["asset_usd_sy"] if last is not None else 1.0
        haircut = (last["index"] / last["py_index"]) if last is not None else 1.0
        val = units * min(haircut, 1.0) * asset_usd * (1 - self.c.conv(t["output_tokens"]))
        out_gas = GAS_LOOP_OUT if t["looped"] else self.c.gas_units["REDEEM_PT"]
        eq = val - debt - self.gas(t["chain"], t["expiry"], out_gas)
        return dict(t, close=t["expiry"], outcome="matured", equity_end=max(eq, 0.0),
                    min_hf=min_hf, debt_end=debt)


def summarize(trades):
    t = trades.copy()
    t["ret"] = t.equity_end / t.capital - 1
    t["days"] = (t.close - t.open).dt.days.clip(lower=1)
    t["apy"] = (1 + t.ret.clip(lower=-0.999)) ** (365 / t.days) - 1
    return t


def run_portfolio(sim: "LoopSim", capital, p: LoopParams, start, end, chains=(1, 42161),
                  keys=None):
    """One loop at a time, compounding: whenever capital is free, open the best
    available loop (highest expected equity APY) that passes the filters; idle cash
    earns nothing. Returns (equity curve, trades)."""
    keys = [k for k in (keys or sim.venues) if k in sim.rows
            and sim.rows[k]["chain"].iloc[0] in chains]
    d, eq = pd.Timestamp(start), capital
    curve, trades = [(d, eq)], []
    while d <= pd.Timestamp(end) and eq > 1:
        best = None
        for k in keys:
            if d not in sim.rows[k].index:
                continue
            t = sim.open_trade(k, d, eq, p)
            if t is None:
                continue
            score = t["exp_apy"]
            if best is None or score > best[0]:
                best = (score, t)
        if best is None:
            d += pd.Timedelta(days=1)
            continue
        res = sim.run_trade(best[1], p)
        res.pop("v", None)
        curve += [(dt, max(x, 0.0)) for dt, x in res.pop("path", [])[1:]]
        trades.append(res)
        eq = res["equity_end"]
        d = res["close"] + pd.Timedelta(days=1)
        curve.append((res["close"], eq))
    s = pd.Series(dict(curve)).sort_index()
    return s[~s.index.duplicated(keep="last")], pd.DataFrame(trades)
