"""Float port of Pendle V2 ``MarketMathCore`` plus the router's exact-in solvers.

Everything is denominated the way the contracts do it:
  * ``total_pt``  PT reserve of the pool
  * ``total_sy``  SY reserve of the pool
  * ``index``     PY index (asset per SY), so asset = sy * index
  * ``ln_implied``  last ln(1 + implied APY) stored by the market
  * ``scalar_root``, ``ln_fee_root`` immutable-ish curve parameters (1e18 scaled on chain)
  * ``reserve_fee_pct`` share of the swap fee sent to the treasury (0-100)

Validated against the on-chain RouterStatic quotes in ``tests/test_amm.py``.
"""
import math
from dataclasses import dataclass, replace

from scipy.optimize import brentq

YEAR = 365 * 86400
MAX_MARKET_PROPORTION = 0.96


class TradeError(ValueError):
    """Trade would revert on chain (proportion too high or rate below one)."""


@dataclass(frozen=True)
class PoolState:
    total_pt: float
    total_sy: float
    index: float
    ln_implied: float
    expiry: float          # unix seconds
    scalar_root: float
    ln_fee_root: float
    reserve_fee_pct: float = 80.0

    @staticmethod
    def from_implied_apy(total_pt, total_sy, index, implied_apy, expiry, scalar_root,
                         ln_fee_root, reserve_fee_pct=80.0):
        return PoolState(total_pt, total_sy, index, math.log1p(implied_apy), expiry,
                         scalar_root, ln_fee_root, reserve_fee_pct)

    def implied_apy(self):
        return math.expm1(self.ln_implied)

    def pt_price(self, now):
        """Mid PT price in asset units implied by the stored rate."""
        return math.exp(-self.ln_implied * (self.expiry - now) / YEAR)


@dataclass(frozen=True)
class PreCompute:
    rate_scalar: float
    total_asset: float
    rate_anchor: float
    fee_rate: float
    tte: float


def _log_proportion(p):
    if p >= 1.0:
        raise TradeError("proportion >= 1")
    return math.log(p / (1.0 - p))


def precompute(s: PoolState, now: float) -> PreCompute:
    tte = s.expiry - now
    if tte <= 0:
        raise TradeError("market expired")
    rate_scalar = s.scalar_root * YEAR / tte
    total_asset = s.total_sy * s.index
    if s.total_pt <= 0 or total_asset <= 0:
        raise TradeError("empty pool")
    new_exchange_rate = math.exp(s.ln_implied * tte / YEAR)
    proportion = s.total_pt / (s.total_pt + total_asset)
    rate_anchor = new_exchange_rate - _log_proportion(proportion) / rate_scalar
    fee_rate = math.exp(s.ln_fee_root * tte / YEAR)
    return PreCompute(rate_scalar, total_asset, rate_anchor, fee_rate, tte)


def _exchange_rate(s, c, net_pt_to_account):
    numerator = s.total_pt - net_pt_to_account
    if numerator < 0:
        raise TradeError("not enough PT in pool")
    proportion = numerator / (s.total_pt + c.total_asset)
    if proportion > MAX_MARKET_PROPORTION:
        raise TradeError("proportion too high")
    rate = _log_proportion(proportion) / c.rate_scalar + c.rate_anchor
    if rate < 1.0:
        raise TradeError("exchange rate below one")
    return rate


def calc_trade(s: PoolState, now: float, net_pt_to_account: float):
    """Return (net_sy_to_account, sy_fee, sy_to_reserve, new_state).

    ``net_pt_to_account`` > 0 buys PT from the pool, < 0 sells PT into it.
    """
    c = precompute(s, now)
    pre_rate = _exchange_rate(s, c, net_pt_to_account)
    pre_asset = -net_pt_to_account / pre_rate
    if net_pt_to_account > 0:
        if pre_rate / c.fee_rate < 1.0:
            raise TradeError("post-fee exchange rate below one")
        fee = pre_asset * (1.0 - c.fee_rate)
    else:
        fee = -(pre_asset * (1.0 - c.fee_rate)) / c.fee_rate
    asset_to_reserve = fee * s.reserve_fee_pct / 100.0
    asset_to_account = pre_asset - fee
    sy_to_account = asset_to_account / s.index
    sy_fee = fee / s.index
    sy_to_reserve = asset_to_reserve / s.index

    new_pt = s.total_pt - net_pt_to_account
    new_sy = s.total_sy - (sy_to_account + sy_to_reserve)
    tmp = replace(s, total_pt=new_pt, total_sy=new_sy)
    c2 = PreCompute(c.rate_scalar, new_sy * s.index, c.rate_anchor, c.fee_rate, c.tte)
    new_rate = _exchange_rate(tmp, c2, 0.0)
    new_ln = math.log(new_rate) * YEAR / c.tte
    return sy_to_account, sy_fee, sy_to_reserve, replace(tmp, ln_implied=new_ln)


# ---------------------------------------------------------------- exact-in helpers
def _root(f, lo, hi):
    return brentq(f, lo, hi, xtol=1e-12 * max(1.0, hi), rtol=1e-13, maxiter=200)


def max_pt_out(s: PoolState, now: float):
    """Largest PT purchase before the post-fee exchange rate hits 1 (closed form)."""
    c = precompute(s, now)
    z = (c.fee_rate - c.rate_anchor) * c.rate_scalar
    p_min = 1.0 / (1.0 + math.exp(-z)) if z > -700 else 0.0
    x = s.total_pt - p_min * (s.total_pt + c.total_asset)
    return max(min(x, s.total_pt) * (1 - 1e-9), 0.0)


def max_pt_in(s: PoolState, now: float):
    """Largest PT sale before the pool proportion cap (96% PT) binds."""
    c = precompute(s, now)
    return max((MAX_MARKET_PROPORTION * (s.total_pt + c.total_asset) - s.total_pt)
               * (1 - 1e-9), 0.0)


def swap_exact_sy_for_pt(s: PoolState, now: float, sy_in: float):
    """Router ``swapExactSyForPt``: returns (pt_out, sy_fee, new_state)."""
    hi = max_pt_out(s, now)
    if hi <= 0:
        raise TradeError("no PT available")

    def cost(pt):
        return -calc_trade(s, now, pt)[0] - sy_in   # >0 when this PT costs more than we have

    if cost(hi) <= 0:
        raise TradeError("trade larger than pool depth")
    pt_out = _root(cost, 0.0, hi)
    sy_to_acc, fee, _, new = calc_trade(s, now, pt_out)
    return pt_out, fee, new


def swap_exact_pt_for_sy(s: PoolState, now: float, pt_in: float):
    """Router ``swapExactPtForSy``: returns (sy_out, sy_fee, new_state)."""
    sy_out, fee, _, new = calc_trade(s, now, -pt_in)
    return sy_out, fee, new


def swap_exact_sy_for_yt(s: PoolState, now: float, sy_in: float):
    """Router ``swapExactSyForYt`` (flash: sell minted PT into the pool).

    Mint ``yt`` PY with ``yt / index`` SY; the pool pays SY for the ``yt`` PT.
    Solve sy_in + sy_from_selling(yt) = yt / index.
    """
    def gap(yt):
        return yt / s.index - calc_trade(s, now, -yt)[0] - sy_in   # >0 when we cannot afford

    hi = max_pt_in(s, now)
    if hi <= 0 or gap(hi) <= 0:
        raise TradeError("trade larger than pool depth")
    yt_out = _root(gap, 0.0, hi)
    _, fee, _, new = calc_trade(s, now, -yt_out)
    return yt_out, fee, new


def swap_exact_yt_for_sy(s: PoolState, now: float, yt_in: float):
    """Router ``swapExactYtForSy`` (flash: buy PT from pool, redeem PT+YT)."""
    sy_to_acc, fee, _, new = calc_trade(s, now, yt_in)   # buying PT costs SY
    sy_out = yt_in / s.index + sy_to_acc
    if sy_out < 0:
        raise TradeError("YT worthless after costs")
    return sy_out, fee, new


def spot_pt_price_asset(s: PoolState, now: float):
    return s.pt_price(now)


# ---------------------------------------------------------------- liquidity
def add_liquidity_single_sy(s: PoolState, total_lp: float, now: float, sy_in: float):
    """Router ``addLiquiditySingleSy``: buy just enough PT that the remaining SY
    and the PT match the post-swap pool ratio, then add both. Returns
    (lp_out, sy_fee, new_state, new_total_lp)."""
    def resid(pt):
        try:
            sy_to_acc, _, _, st = calc_trade(s, now, pt)
        except TradeError:
            return 1e9
        sy_left = sy_in + sy_to_acc
        if sy_left <= 0:
            return 1e9
        return pt / sy_left - st.total_pt / st.total_sy   # >0: bought too much PT

    hi = min(sy_in * s.index * 2, max_pt_out(s, now))
    if hi <= 0 or resid(0.0) >= 0 or resid(hi) <= 0:
        raise TradeError("trade larger than pool depth")
    pt = _root(resid, 0.0, hi)
    sy_to_acc, fee, _, st = calc_trade(s, now, pt)
    sy_left = sy_in + sy_to_acc
    lp = min(pt * total_lp / st.total_pt, sy_left * total_lp / st.total_sy)
    new = replace(st, total_pt=st.total_pt + pt, total_sy=st.total_sy + sy_left)
    return lp, fee, new, total_lp + lp


def remove_liquidity_single_sy(s: PoolState, total_lp: float, now: float, lp_in: float):
    """Router ``removeLiquiditySingleSy``: burn LP, sell the PT leg into the pool
    (or redeem it 1:1 in asset after expiry). Returns (sy_out, sy_fee)."""
    share = lp_in / total_lp
    pt, sy = share * s.total_pt, share * s.total_sy
    if now >= s.expiry:
        return sy + pt / s.index, 0.0
    st = replace(s, total_pt=s.total_pt - pt, total_sy=s.total_sy - sy)
    sy_from_pt, fee, _ = swap_exact_pt_for_sy(st, now, pt)
    return sy + sy_from_pt, fee


def lp_value_asset(s: PoolState, total_lp: float, now: float):
    """Mid value of one LP token in asset terms."""
    pt_px = s.pt_price(now) if now < s.expiry else 1.0
    return (s.total_pt * pt_px + s.total_sy * s.index) / total_lp
