"""Transaction cost model: gas, token conversion, router approximation.

AMM swap fees and price impact are NOT here: they come out of the exact AMM
math in ``amm.py`` applied to each day's real pool reserves.

Sources
  gas units   medians of real Pendle Router V4 txs (data/raw/gas.json)
  gas price   daily median base fee + median priority fee over the ~1.7h window
              ending 14:00 UTC, smoothed with a trailing 3-day median
  ETH/USD     DefiLlama daily
  conversion  live KyberSwap USDC->token->USDC round trips at $10k
              (half the round trip loss = one-way cost)
"""
import datetime as dt
import json
import os
from dataclasses import dataclass, field

import pandas as pd

from pendle_bt.fetch_history import RAW

# fallbacks if measurement is missing (Router V4, includes aggregator hop)
DEFAULT_GAS = {"BUY_PT": 556_000, "SELL_PT": 524_000, "BUY_YT": 436_000, "SELL_YT": 447_000,
               "ADD_LIQUIDITY": 600_000, "REMOVE_LIQUIDITY": 550_000}
EXTRA_GAS = {
    "REDEEM_PT": 450_000,       # redeemPyToToken incl. aggregator hop out
    "CLAIM": 250_000,           # redeemDueInterestAndRewards
    "APPROVE": 50_000,          # first use of a token
    "CASH_MOVE": 200_000,       # Aave v3 supply / withdraw of idle USDC
    "DEX_SWAP": 180_000,        # plain aggregator swap (benchmarks)
}


@dataclass
class CostModel:
    gas_mult: float = 1.0          # stress multiplier on gas
    conv_mult: float = 1.0         # stress multiplier on conversion slippage
    default_conv: float = 0.0025   # 25 bps one-way when no live route exists
    min_conv: float = 0.0001       # 1 bp floor (aggregator fee / rounding)
    max_measured_rt: float = 0.05  # round trips worse than 5% mean "no route today"
    l2_tx_usd: float = 0.10        # Arbitrum all-in cost per tx
    router_eps: float = 1e-4       # router approximation haircut on exact-in legs
    pendle_reward_haircut: float = 0.05   # sell PENDLE emissions: slippage + claim gas
    gas_units: dict = field(default_factory=dict)
    _gas_daily: pd.DataFrame = None
    _conv: dict = None

    def __post_init__(self):
        path = os.path.join(RAW, "gas.json")
        g = json.load(open(path)) if os.path.exists(path) else {}
        units = dict(DEFAULT_GAS)
        for k, v in g.get("gas_used", {}).items():
            units[k] = v["median"]
        units.update(EXTRA_GAS)
        units.update(self.gas_units)
        self.gas_units = units
        if g.get("daily"):
            d = pd.DataFrame(g["daily"])
            d["date"] = pd.to_datetime(d["date"])
            px = pd.Series(g["eth_usd"])
            px.index = pd.to_datetime(px.index)
            d["eth_usd"] = d["date"].map(px).ffill().bfill()
            d["usd_per_gas"] = (d.base_gwei + d.tip_gwei) * 1e-9 * d.eth_usd
            # a bot can wait out a one-window spike: price gas at the trailing 3-day median
            raw = d.set_index("date")["usd_per_gas"].asfreq("D").ffill()
            self._gas_daily = raw.rolling(3, min_periods=1).median()
        cpath = os.path.join(RAW, "conversion_costs.json")
        self._conv = json.load(open(cpath)) if os.path.exists(cpath) else {}

    # ------------------------------------------------------------------ gas
    def usd_per_gas(self, date):
        if self._gas_daily is None:
            return 20e-9 * 3000           # 20 gwei at $3000 if no data
        d = pd.Timestamp(date)
        s = self._gas_daily
        if d < s.index[0]:
            return s.iloc[0]
        if d > s.index[-1]:
            return s.iloc[-1]
        return s.loc[d]

    def gas_usd(self, chain, date, action):
        if chain != 1:
            return self.l2_tx_usd * self.gas_mult
        return self.gas_units[action] * self.usd_per_gas(date) * self.gas_mult

    # ----------------------------------------------------------- conversion
    def _one_way(self, token):
        rt = self._conv.get(token)
        if rt is None or rt > self.max_measured_rt or rt < -0.01:
            return None
        return max(rt / 2.0, self.min_conv)

    def conv(self, tokens):
        """Cheapest one-way USDC<->token cost among the SY's accepted tokens."""
        best = None
        for t in [t for t in tokens.split(",") if t]:
            c = self._one_way(t)
            if c is not None and (best is None or c < best):
                best = c
        if best is None:
            best = self.default_conv
        return best * self.conv_mult
