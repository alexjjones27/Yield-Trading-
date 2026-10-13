"""Daily event-driven backtest of PT / YT / LP dislocation strategies.

One step per 00:00 UTC snapshot:
  1. accrue idle cash at the Aave v3 USDC supply rate
  2. accrue YT interest (PY index growth, net of the YT interest fee) and LP
     PENDLE emissions
  3. settle matured positions (PT redeem, YT claim, LP unwind); redeemed SY can
     be rolled into another maturity of the same SY without a token conversion
  4. exits, then entries, each executed against that day's real pool reserves
     with the exact Pendle AMM math (fee + price impact), plus gas and
     USDC<->underlying conversion cost
  5. record mid NAV and liquidation NAV (what a full exit would return today)

Signals only use information available at the snapshot (``lag`` = 1 makes the
bot trade on the previous day's signal instead).
"""
import math
from dataclasses import dataclass, field, asdict

import numpy as np
import pandas as pd

from pendle_bt import amm
from pendle_bt.costs import CostModel

YEAR_S = 365 * 86400


@dataclass
class Params:
    name: str = "pt_dislocation"
    instruments: tuple = ("pt",)
    forecast: str = "ema14"          # trailing realised-yield column used as the floating forecast
    blend_w: float = 1.0             # forecast = w * col + (1-w) * mean90
    naive: bool = False              # rank PT purely by carry over cash (no dislocation filter)
    achievable: str = "max_cash"     # floating benchmark: "own" SY yield or max(own, cash rate)
    min_under_tvl: float = 0.0       # ex-ante quality: total Pendle TVL of the underlying (USD)
    min_hold_days: int = 2           # no discretionary exit within this many days of entry
    pt_entry: float = 0.02           # min (all-in fixed APY - forecast) to buy PT
    pt_exit: float = -0.01           # sell early when (hold-to-maturity APY - forecast) < this
    pt_min_carry: float = 0.0        # min (all-in fixed APY - cash APY)
    yt_entry: float = 0.03           # min (forecast - YT break-even yield) to buy YT
    yt_exit: float = -0.01           # sell when (forecast - break-even at sale price) < this
    yt_max_frac: float = 0.10        # cap on capital in YT
    lp_entry: float = 0.03           # min (expected all-in LP APY - cash)
    lp_band: float = 0.02            # LP only when |implied - forecast| <= band
    lp_max_frac: float = 0.60
    min_tvl: float = 1_000_000       # pool liquidity filter (USD)
    min_tau_pt: int = 21             # days to expiry to open PT / LP
    min_tau_yt: int = 45
    min_age: int = 3                 # skip a market's first days (launch noise)
    min_hist: int = 14               # days of SY index history needed for a forecast
    max_frac_market: float = 0.34
    max_frac_sy: float = 0.50
    max_pool_frac: float = 0.02      # our trade <= 2% of pool liquidity
    max_rate_impact: float = 0.004   # max fee+impact in implied-rate terms on PT entry
    max_yt_impact: float = 0.01      # max (YT break-even yield - mid implied) on YT entry
    min_trade_usd: float = 750
    lag: int = 0
    idle_in_aave: bool = True
    chains: tuple = (1, 42161)
    stop_loss_yt: float = -0.6       # cut YT when liquidation value < (1+x) * cost

    def to_dict(self):
        return asdict(self)


@dataclass
class Position:
    kind: str
    key: str
    sy: str
    chain: int
    units: float
    cost_usd: float
    open_date: pd.Timestamp
    row: dict
    py_prev: float = 0.0
    interest_sy: float = 0.0
    rewards_usd: float = 0.0


class Engine:
    def __init__(self, panel, sy_index, cash_apy, costs: CostModel, p: Params,
                 capital=10_000.0, start=None, end=None):
        self.p = p
        self.c = costs
        self.capital = capital
        pan = panel[panel.chain.isin(p.chains)]
        if start is not None:
            pan = pan[pan.date >= pd.Timestamp(start)]
        if end is not None:
            pan = pan[pan.date <= pd.Timestamp(end)]
        self.dates = sorted(pan.date.unique())
        self.rows = {d: {r["key"]: r for r in g.to_dict("records")}
                     for d, g in pan.groupby("date")}
        sy = sy_index.set_index(["sy", "date"])
        self.sy_tab = {k: v for k, v in sy[["index", "py_index", "asset_usd_sy"]]
                       .dropna().to_dict("index").items()}
        self.sy_last = {}
        self.cash_apy = cash_apy
        self.cash_ref = cash_apy.ewm(halflife=14).mean()   # decisions use a smoothed rate
        self.cash = capital
        self.pos = {}
        self.sy_wallet = {}
        self.trades = []
        self.nav = []
        self.cost_tot = dict(gas=0.0, conversion=0.0, amm_fee=0.0, amm_impact=0.0,
                             yt_interest_fee=0.0)
        self.approved = set()

    # ------------------------------------------------------------ helpers
    def forecast(self, r):
        """Expected floating yield of this market's own SY (what a YT receives)."""
        f = r[self.p.forecast]
        m = r["mean90"]
        if self.p.blend_w < 1.0 and m == m:
            f = self.p.blend_w * f + (1 - self.p.blend_w) * m
        return f

    def cash_rate(self, d):
        return self.cash_ref.get(pd.Timestamp(d), 0.0) if self.p.idle_in_aave else 0.0

    def achievable(self, r, d):
        """Best floating alternative for the capital: own SY yield or USDC lending."""
        f = self.forecast(r)
        return max(f, self.cash_rate(d)) if self.p.achievable == "max_cash" else f

    def state(self, r):
        # the market prices SY at pyIndexCurrent = max(SY.exchangeRate(), pyIndexStored)
        return amm.PoolState(r["totalPt"], r["totalSy"], max(r["index"], r["py_index"]),
                             r["ln_implied"],
                             r["expiry_ts"].timestamp(), r["scalar_root"], r["ln_fee_root"],
                             r["reserve_fee_pct"])

    def now(self, d):
        return pd.Timestamp(d).timestamp()

    def gas(self, r, d, action, token=None):
        g = self.c.gas_usd(r["chain"], d, action)
        if token and (r["chain"], token) not in self.approved:
            g += self.c.gas_usd(r["chain"], d, "APPROVE")
        return g

    def cash_leg_gas(self, r, d):
        if not self.p.idle_in_aave:
            return 0.0
        return self.c.gas_usd(1, d, "CASH_MOVE") if r["chain"] == 1 else self.c.l2_tx_usd

    def sy_point(self, sy, d):
        v = self.sy_tab.get((sy, pd.Timestamp(d)))
        if v is not None:
            self.sy_last[sy] = v
            return v
        return self.sy_last.get(sy)

    def _log(self, d, r, action, usd, units, **kw):
        live = action.startswith(("BUY", "SELL"))
        rec = dict(date=d, market=r["name"], key=r["key"], chain=r["chain"],
                   expiry=r["expiry_ts"], action=action, usd=usd, units=units,
                   implied=r["impliedApy"] if live else np.nan,
                   forecast=self.forecast(r) if live else np.nan)
        rec.update(kw)
        self.trades.append(rec)
        for k in ("gas", "conversion", "amm_fee", "amm_impact"):
            self.cost_tot[k] += kw.get(k, 0.0)

    def exposure(self, key=None, sy=None, kind=None):
        tot = 0.0
        for p in self.pos.values():
            if key and p.key != key:
                continue
            if sy and p.sy != sy:
                continue
            if kind and p.kind != kind:
                continue
            tot += p.cost_usd
        return tot

    # ------------------------------------------------------------ quotes
    def _usd_to_sy(self, r, d, usd, kind, from_wallet=0.0):
        """Spend ``usd`` of cash (+ optional SY from wallet). Returns sy_in and costs."""
        action = {"pt": "BUY_PT", "yt": "BUY_YT", "lp": "ADD_LIQUIDITY"}[kind]
        gas = self.gas(r, d, action, token=r["key"]) + (self.cash_leg_gas(r, d) if usd > 0 else 0)
        conv = max(usd - gas, 0.0) * self.c.conv(r["input_tokens"]) if usd > 0 else 0.0
        asset = max(usd - gas - conv, 0.0) / r["asset_usd"]
        sy_in = asset / r["index"] + from_wallet
        return sy_in, gas, conv

    def quote_buy(self, r, d, kind, usd, from_wallet=0.0):
        sy_in, gas, conv = self._usd_to_sy(r, d, usd, kind, from_wallet)
        if sy_in <= 0:
            return None
        s = self.state(r)
        t = self.now(d)
        eps = self.c.router_eps
        try:
            if kind == "pt":
                units, fee, _ = amm.swap_exact_sy_for_pt(s, t, sy_in)
                units *= 1 - eps
                mid_val = units * r["pt_mid"]
            elif kind == "yt":
                units, fee, _ = amm.swap_exact_sy_for_yt(s, t, sy_in)
                units *= 1 - eps
                mid_val = units * (1 - r["pt_mid"])
            else:
                units, fee, _, _ = amm.add_liquidity_single_sy(s, r["totalSupply"], t, sy_in)
                units *= 1 - eps
                mid_val = units * amm.lp_value_asset(s, r["totalSupply"], t)
        except amm.TradeError:
            return None
        asset_in = sy_in * r["index"]
        fee_usd = fee * max(r["index"], r["py_index"]) * r["asset_usd"]
        amm_cost = (asset_in - mid_val) * r["asset_usd"]
        return dict(units=units, sy_in=sy_in, gas=gas, conversion=conv, amm_fee=fee_usd,
                    amm_impact=amm_cost - fee_usd, usd=usd)

    def quote_sell(self, pos, r, d, to_cash=True):
        """Liquidate a live position against row ``r``. Returns usd, costs."""
        s = self.state(r)
        t = self.now(d)
        try:
            if pos.kind == "pt":
                sy_out, fee, _ = amm.swap_exact_pt_for_sy(s, t, pos.units)
                mid_val = pos.units * r["pt_mid"]
                action = "SELL_PT"
            elif pos.kind == "yt":
                sy_out, fee, _ = amm.swap_exact_yt_for_sy(s, t, pos.units)
                mid_val = pos.units * (1 - r["pt_mid"])
                action = "SELL_YT"
            else:
                sy_out, fee = amm.remove_liquidity_single_sy(s, r["totalSupply"], t, pos.units)
                mid_val = pos.units * amm.lp_value_asset(s, r["totalSupply"], t)
                action = "REMOVE_LIQUIDITY"
        except amm.TradeError:
            return None
        sy_out += pos.interest_sy
        gas = self.gas(r, d, action)
        if pos.kind in ("yt",) and pos.interest_sy > 0:
            gas += self.gas(r, d, "CLAIM")
        if pos.kind == "lp" and pos.rewards_usd > 0:
            gas += self.gas(r, d, "CLAIM")
        asset = sy_out * r["index"]
        gross = asset * r["asset_usd"]
        conv = gross * self.c.conv(r["output_tokens"])
        usd = gross - conv - gas + pos.rewards_usd
        if to_cash:
            usd -= self.cash_leg_gas(r, d)
        fee_usd = fee * max(r["index"], r["py_index"]) * r["asset_usd"]
        amm_cost = (mid_val + pos.interest_sy * r["index"] - asset) * r["asset_usd"]
        return dict(usd=usd, gas=gas, conversion=conv, amm_fee=fee_usd,
                    amm_impact=amm_cost - fee_usd, sy_out=sy_out)

    # ------------------------------------------------------------ economics
    def pt_allin_apy(self, r, d, q):
        """Fixed APY locked in USD after all entry and (expected) exit costs."""
        tau = r["tau"]
        exit_gas = self.c.gas_usd(r["chain"], d, "REDEEM_PT") + self.cash_leg_gas(r, d)
        out = q["units"] * r["asset_usd"] * (1 - self.c.conv(r["output_tokens"])) - exit_gas
        if out <= 0 or q["usd"] <= 0:
            return -1.0
        return (out / q["usd"]) ** (1 / tau) - 1

    def pt_hold_apy(self, pos, r, d, q_sell):
        """APY earned from today by holding to maturity instead of selling now."""
        tau = r["tau"]
        exit_gas = self.c.gas_usd(r["chain"], d, "REDEEM_PT") + self.cash_leg_gas(r, d)
        out = pos.units * r["asset_usd"] * (1 - self.c.conv(r["output_tokens"])) - exit_gas
        if q_sell["usd"] <= 0:
            return 9.99
        return (max(out, 1e-9) / q_sell["usd"]) ** (1 / tau) - 1

    def yt_breakeven(self, r, d, units, usd):
        """Underlying APY that makes ``units`` YT worth ``usd`` after fee and costs."""
        tau = r["tau"]
        claim = self.c.gas_usd(r["chain"], d, "CLAIM") + self.cash_leg_gas(r, d)
        k = units * (1 - r["interest_fee"]) * r["asset_usd"] * (1 - self.c.conv(r["output_tokens"]))
        if k <= 0:
            return 9.99
        return (1 + (usd + claim) / k) ** (1 / tau) - 1

    def lp_expected_apy(self, r):
        w_pt = r["totalPt"] * r["pt_mid"] / (r["totalPt"] * r["pt_mid"] + r["totalSy"] * r["index"])
        pendle = min(r.get("pendle7", r["pendleApy"]) or 0.0, 0.5)
        fees = min(r.get("swapfee7", r["swapFeeApy"]) or 0.0, 0.2)
        return (w_pt * r["impliedApy"] + (1 - w_pt) * self.forecast(r) + fees
                + pendle * (1 - self.c.pendle_reward_haircut))

    # ------------------------------------------------------------ book keeping
    def nav_mid(self, d):
        tot = self.cash + sum(self._wallet_usd(d).values())
        for p in self.pos.values():
            r = p.row
            tau = max((r["expiry_ts"] - pd.Timestamp(d)).total_seconds() / YEAR_S, 0.0)
            pt_px = math.exp(-r["ln_implied"] * tau)
            if p.kind == "pt":
                v = p.units * pt_px
            elif p.kind == "yt":
                v = p.units * (1 - pt_px) + p.interest_sy * r["index"]
            else:
                v = p.units * (r["totalPt"] * pt_px + r["totalSy"] * r["index"]) / r["totalSupply"]
            tot += v * r["asset_usd"] + p.rewards_usd
        return tot

    def nav_liq(self, d, today):
        """Exit value: AMM sale net of all costs. When the pool cannot absorb the sale
        (e.g. PT share at the 96% cap after a sell-off) the position is marked at its
        hold-to-maturity value, discounted at implied + 5%, net of exit costs."""
        tot = self.cash + sum(self._wallet_usd(d).values())
        for p in self.pos.values():
            r = today.get(p.key, p.row)
            q = self.quote_sell(p, r, d)
            if q is not None:
                tot += max(q["usd"], 0.0)
            else:
                tot += self.hold_value(p, r, d)
        return tot

    def hold_value(self, p, r, d):
        tau = max((r["expiry_ts"] - pd.Timestamp(d)).total_seconds() / YEAR_S, 0.0)
        disc = (1 + max(r["impliedApy"], 0.0) + 0.05) ** tau
        conv = 1 - self.c.conv(r["output_tokens"])
        exit_gas = self.c.gas_usd(r["chain"], d, "REDEEM_PT") + self.cash_leg_gas(r, d)
        if p.kind == "pt":
            v = p.units * r["asset_usd"] * conv / disc
        elif p.kind == "yt":
            v = (p.interest_sy * r["index"] + 0.5 * p.units * (1 - math.exp(-r["ln_implied"] * tau))
                 ) * r["asset_usd"] * conv
        else:
            share = p.units / r["totalSupply"]
            v = share * (r["totalPt"] + r["totalSy"] * r["index"]) * r["asset_usd"] * conv / disc
            v += p.rewards_usd
        return max(v - exit_gas, 0.0)

    def _mid_value(self, p, d):
        save = self.pos
        self.pos = {0: p}
        cash, wallet = self.cash, self.sy_wallet
        self.cash, self.sy_wallet = 0.0, {}
        v = self.nav_mid(d)
        self.pos, self.cash, self.sy_wallet = save, cash, wallet
        return v

    def _wallet_usd(self, d):
        out = {}
        for sy, (amt, chain, r) in self.sy_wallet.items():
            pt = self.sy_point(sy, d) or {"index": r["index"], "asset_usd_sy": r["asset_usd"]}
            out[sy] = amt * pt["index"] * pt["asset_usd_sy"]
        return out

    # ------------------------------------------------------------ lifecycle
    def accrue(self, d):
        apy = self.cash_apy.get(pd.Timestamp(d), np.nan)
        if apy == apy and self.p.idle_in_aave:
            self.cash *= (1 + apy) ** (1 / 365)
        for p in self.pos.values():
            if p.kind == "yt":
                pt = self.sy_point(p.sy, d)
                if pt is None or pd.Timestamp(d) > p.row["expiry_ts"]:
                    continue
                py = pt["py_index"]
                if py > p.py_prev > 0:
                    gross = p.units * (1 / p.py_prev - 1 / py)
                    fee = gross * p.row["interest_fee"]
                    p.interest_sy += gross - fee
                    self.cost_tot["yt_interest_fee"] += fee * pt["index"] * pt["asset_usd_sy"]
                p.py_prev = max(py, p.py_prev)
            elif p.kind == "lp":
                r = p.row
                pendle = min(r.get("pendle7", r["pendleApy"]) or 0.0, 0.5)
                val = self._mid_value(p, d) - p.rewards_usd
                p.rewards_usd += val * pendle / 365 * (1 - self.c.pendle_reward_haircut)

    def settle(self, d):
        for k in list(self.pos):
            p = self.pos[k]
            if pd.Timestamp(d) < p.row["expiry_ts"]:
                continue
            r = p.row
            pt = self.sy_point(p.sy, p.row["expiry_ts"]) or self.sy_point(p.sy, d) or {
                "index": r["index"], "py_index": r["py_index"], "asset_usd_sy": r["asset_usd"]}
            if p.kind == "pt":
                sy_amt = p.units / pt["py_index"]
                gas = self.gas(r, d, "REDEEM_PT")
            elif p.kind == "yt":
                sy_amt = p.interest_sy
                gas = self.gas(r, d, "CLAIM")
            else:
                share = p.units / r["totalSupply"]
                sy_amt = share * r["totalSy"] + share * r["totalPt"] / pt["py_index"]
                gas = self.gas(r, d, "REMOVE_LIQUIDITY") + self.gas(r, d, "CLAIM")
            self.cash -= gas
            self.cash += p.rewards_usd
            amt, chain, _ = self.sy_wallet.get(p.sy, (0.0, p.chain, r))
            self.sy_wallet[p.sy] = (amt + sy_amt, p.chain, r)
            self._log(d, r, f"MATURE_{p.kind.upper()}", sy_amt * pt["index"] * pt["asset_usd_sy"],
                      p.units, gas=gas, pnl=sy_amt * pt["index"] * pt["asset_usd_sy"]
                      + p.rewards_usd - gas - p.cost_usd)
            del self.pos[k]

    def flush_wallet(self, d):
        for sy, (amt, chain, r) in list(self.sy_wallet.items()):
            pt = self.sy_point(sy, d) or {"index": r["index"], "asset_usd_sy": r["asset_usd"]}
            gross = amt * pt["index"] * pt["asset_usd_sy"]
            conv = gross * self.c.conv(r["output_tokens"])
            leg = self.cash_leg_gas(r, d)
            self.cash += gross - conv - leg
            self._log(d, r, "SY_TO_CASH", gross, amt, conversion=conv, gas=leg)
            del self.sy_wallet[sy]

    # ------------------------------------------------------------ strategy
    def eligible(self, r, kind, d):
        p = self.p
        tau_days = r["tau"] * 365
        if r["tvl"] < p.min_tvl or r["age_days"] < p.min_age or r["days_hist"] != r["days_hist"]:
            return False
        if r["days_hist"] < p.min_hist or r[p.forecast] != r[p.forecast]:
            return False
        if p.min_under_tvl and r.get("under_tvl", 0.0) < p.min_under_tvl:
            return False
        if r["impliedApy"] <= 0:
            return False
        if kind in ("pt", "lp") and tau_days < p.min_tau_pt:
            return False
        if kind == "yt" and tau_days < p.min_tau_yt:
            return False
        return True

    def size_cap(self, r, kind, nav):
        p = self.p
        cap = min(p.max_frac_market * nav - self.exposure(key=r["key"]),
                  p.max_frac_sy * nav - self.exposure(sy=r["sy"]),
                  p.max_pool_frac * r["tvl"])
        if kind == "yt":
            cap = min(cap, p.yt_max_frac * nav - self.exposure(kind="yt"))
        if kind == "lp":
            cap = min(cap, p.lp_max_frac * nav - self.exposure(kind="lp"))
        return cap

    def rate_impact_ok(self, r, q, kind):
        """Fee + price impact of the AMM leg only, expressed in implied-rate terms."""
        asset_in = q["sy_in"] * r["index"]
        if kind == "pt":
            exec_ln = math.log(q["units"] / asset_in) / r["tau"]
            return r["ln_implied"] - exec_ln <= self.p.max_rate_impact
        if kind == "yt":
            # yield at which the YT bought equals the asset paid (no gas / conversion)
            be = (1 + asset_in / q["units"]) ** (1 / r["tau"]) - 1
            return be - r["impliedApy"] <= self.p.max_yt_impact
        return True

    def score(self, r, d, kind, q):
        """Return (eligible, score) where score is the expected excess APY used for ranking."""
        p = self.p
        cash = self.cash_rate(d)
        f = self.forecast(r)
        if kind == "pt":
            a = self.pt_allin_apy(r, d, q)
            if p.naive:
                return a - cash >= p.pt_min_carry, a - cash
            ok = (a - self.achievable(r, d) >= p.pt_entry) and (a - cash >= p.pt_min_carry)
            return ok, a - cash
        if kind == "yt":
            be = self.yt_breakeven(r, d, q["units"], q["usd"])
            edge = f - be
            exp_payoff = q["units"] * ((1 + f) ** r["tau"] - 1) * (1 - r["interest_fee"]) * r["asset_usd"]
            exp_apy = (max(exp_payoff, 1e-9) / q["usd"]) ** (1 / r["tau"]) - 1
            return edge >= p.yt_entry, exp_apy - cash
        lp = self.lp_expected_apy(r)
        rt_cost = (q["gas"] + q["conversion"] + q["amm_fee"] + q["amm_impact"]) / q["usd"]
        exit_cost = self.c.conv(r["output_tokens"]) + (
            self.c.gas_usd(r["chain"], d, "REMOVE_LIQUIDITY") + self.cash_leg_gas(r, d)) / q["usd"]
        lp_allin = lp - (rt_cost + exit_cost) / max(r["tau"], 30 / 365)
        ok = (lp_allin - cash >= p.lp_entry) and abs(r["impliedApy"] - f) <= p.lp_band
        return ok, lp_allin - cash

    def do_exits(self, d, sig, today):
        p = self.p
        cash = self.cash_rate(d)
        for k in list(self.pos):
            pos = self.pos[k]
            r = today.get(pos.key)
            s = sig.get(pos.key)
            if r is None or s is None:
                continue
            if (pd.Timestamp(d) - pos.open_date).days < p.min_hold_days:
                continue
            q = self.quote_sell(pos, r, d)
            if q is None:
                continue
            f = self.forecast(s)
            exit_now = False
            if pos.kind == "pt":
                hold = self.pt_hold_apy(pos, r, d, q)
                ref = cash if p.naive else self.achievable(s, d)
                exit_now = hold - ref < p.pt_exit
            elif pos.kind == "yt":
                be = self.yt_breakeven(r, d, pos.units, max(q["usd"], 1e-9))
                exit_now = (f - be < p.yt_exit) or (q["usd"] < (1 + p.stop_loss_yt) * pos.cost_usd)
            else:
                lp = self.lp_expected_apy(s)
                exit_now = (lp - cash < p.lp_entry - 0.02) or abs(s["impliedApy"] - f) > p.lp_band + 0.02
            if exit_now:
                self.cash += q["usd"]
                self._log(d, r, f"SELL_{pos.kind.upper()}", q["usd"], pos.units, gas=q["gas"],
                          conversion=q["conversion"], amm_fee=q["amm_fee"],
                          amm_impact=q["amm_impact"], pnl=q["usd"] - pos.cost_usd)
                del self.pos[k]

    def do_entries(self, d, sig, today):
        p = self.p
        nav = self.nav_mid(d)
        cands = []
        for key, r in today.items():
            s = sig.get(key)
            if s is None:
                continue
            for kind in p.instruments:
                if (key, kind) in self.pos or not self.eligible(s, kind, d):
                    continue
                cap = self.size_cap(r, kind, nav)
                wallet = self.sy_wallet.get(r["sy"], (0.0,))[0] if self.sy_wallet.get(
                    r["sy"], (0, r["chain"]))[1] == r["chain"] else 0.0
                wallet_usd = wallet * r["index"] * r["asset_usd"]
                usd = min(cap, self.cash + wallet_usd)
                if usd < p.min_trade_usd:
                    continue
                q = self._quote_mixed(r, d, kind, usd, wallet, wallet_usd)
                if q is None:
                    continue
                # use signal row for economics, executable quote for prices
                rs = dict(r)
                for col in (p.forecast, "mean90", "pendle7", "swapfee7"):
                    if col in s:
                        rs[col] = s[col]
                ok, sc = self.score(rs, d, kind, q)
                if ok:
                    cands.append((sc, key, kind, usd))
        cands.sort(reverse=True)
        for sc, key, kind, _ in cands:
            r = today[key]
            nav = self.nav_mid(d)
            cap = self.size_cap(r, kind, nav)
            wallet, wchain, _ = self.sy_wallet.get(r["sy"], (0.0, r["chain"], None))
            wallet = wallet if wchain == r["chain"] else 0.0
            wallet_usd = wallet * r["index"] * r["asset_usd"]
            usd = min(cap, self.cash + wallet_usd)
            if usd < p.min_trade_usd:
                continue
            q = self._quote_mixed(r, d, kind, usd, wallet, wallet_usd)
            # shrink until the entry respects the rate-impact cap
            tries = 0
            while q is not None and not self.rate_impact_ok(r, q, kind) and tries < 12:
                usd *= 0.6
                if usd < p.min_trade_usd:
                    q = None
                    break
                q = self._quote_mixed(r, d, kind, usd, wallet, wallet_usd)
                tries += 1
            if q is None or not self.rate_impact_ok(r, q, kind):
                continue
            rs = dict(r)
            s = sig[key]
            for col in (p.forecast, "mean90", "pendle7", "swapfee7"):
                if col in s:
                    rs[col] = s[col]
            ok, _ = self.score(rs, d, kind, q)
            if not ok:
                continue
            self._execute_buy(r, d, kind, q)

    def _quote_mixed(self, r, d, kind, usd, wallet, wallet_usd):
        """Use matured SY of the same token first (no conversion), then cash."""
        use_w = min(wallet, usd / (r["index"] * r["asset_usd"])) if wallet_usd > 0 else 0.0
        cash_usd = max(usd - use_w * r["index"] * r["asset_usd"], 0.0)
        if cash_usd < 1.0:
            cash_usd = 0.0
        q = self.quote_buy(r, d, kind, cash_usd, from_wallet=use_w)
        if q is None:
            return None
        if cash_usd == 0.0:
            q["gas"] = self.gas(r, d, {"pt": "BUY_PT", "yt": "BUY_YT", "lp": "ADD_LIQUIDITY"}[kind])
        q["wallet_sy"] = use_w
        q["cash_usd"] = cash_usd
        q["usd"] = cash_usd + use_w * r["index"] * r["asset_usd"]
        return q

    def _execute_buy(self, r, d, kind, q):
        cash_needed = q["cash_usd"] + (q["gas"] if q["cash_usd"] == 0 else 0.0)
        if cash_needed > self.cash + 1e-6:
            return
        self.cash -= cash_needed
        if q["wallet_sy"] > 0:
            amt, chain, rr = self.sy_wallet[r["sy"]]
            left = amt - q["wallet_sy"]
            if left <= 1e-9:
                del self.sy_wallet[r["sy"]]
            else:
                self.sy_wallet[r["sy"]] = (left, chain, rr)
        self.approved.add((r["chain"], r["key"]))
        pos = Position(kind, r["key"], r["sy"], r["chain"], q["units"],
                       q["usd"] + (q["gas"] if q["cash_usd"] == 0 else 0.0), pd.Timestamp(d), r,
                       py_prev=r["py_index"])
        self.pos[(r["key"], kind)] = pos
        self._log(d, r, f"BUY_{kind.upper()}", q["usd"], q["units"], gas=q["gas"],
                  conversion=q["conversion"], amm_fee=q["amm_fee"], amm_impact=q["amm_impact"],
                  rolled_sy=q["wallet_sy"])

    # ------------------------------------------------------------ main loop
    def run(self):
        prev = {}
        for d in self.dates:
            today = self.rows.get(d, {})
            for p in self.pos.values():
                if p.key in today:
                    p.row = today[p.key]
            self.accrue(d)
            self.settle(d)
            sig = prev if self.p.lag else today
            self.do_exits(d, sig, today)
            self.do_entries(d, sig, today)
            self.flush_wallet(d)
            self.nav.append(dict(date=d, nav_mid=self.nav_mid(d), nav_liq=self.nav_liq(d, today),
                                 cash=self.cash, n_pos=len(self.pos),
                                 pt=self.exposure(kind="pt"), yt=self.exposure(kind="yt"),
                                 lp=self.exposure(kind="lp")))
            prev = today
        # final: liquidate everything at the last snapshot
        d = self.dates[-1]
        nav = pd.DataFrame(self.nav).set_index("date")
        trades = pd.DataFrame(self.trades)
        return nav, trades, dict(self.cost_tot)
