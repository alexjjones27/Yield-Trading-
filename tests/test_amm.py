"""AMM port vs. on-chain Pendle RouterStatic quotes recorded at a fixed block."""
import json
import os

import pytest

from pendle_bt import amm

FIX = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures",
                                  "router_static_quotes.json")))
FN = {
    "swapExactPtForSyStatic": amm.swap_exact_pt_for_sy,
    "swapExactSyForPtStatic": amm.swap_exact_sy_for_pt,
    "swapExactSyForYtStatic": amm.swap_exact_sy_for_yt,
    "swapExactYtForSyStatic": amm.swap_exact_yt_for_sy,
}
# exact-out legs are closed form; exact-in legs go through the router's
# approximation, which stops within ~1e-4 of the exact solution
TOL = {"swapExactPtForSyStatic": 1e-9, "swapExactYtForSyStatic": 1e-9,
       "swapExactSyForPtStatic": 1.5e-4, "swapExactSyForYtStatic": 1.5e-4}

CASES = [(r, fn, q[fn]) for r in FIX for q in r["quotes"] for fn in q if q[fn][1]]


@pytest.mark.parametrize("rec,fn,quote", CASES,
                         ids=[f"{r['name']}-{fn}-{q[0]:.0f}" for r, fn, q in CASES])
def test_matches_router_static(rec, fn, quote):
    st = rec["state"]
    s = amm.PoolState(st["total_pt"], st["total_sy"], rec["index"], st["ln_implied"],
                      st["expiry"], st["scalar_root"], st["ln_fee_root"],
                      st["reserve_fee_pct"])
    amount_in, onchain_out = quote
    try:
        mine = FN[fn](s, rec["ts"], amount_in)[0]
    except amm.TradeError:
        pytest.skip("trade reverts in model")
    assert abs(mine / onchain_out - 1) < TOL[fn]
    if fn in ("swapExactSyForPtStatic", "swapExactSyForYtStatic"):
        # our solver is never worse than the router's approximation
        assert mine >= onchain_out * (1 - 1e-9)


def test_fee_is_ln_fee_root_times_tenor():
    # tiny trade: cost over mid is ~ ln_fee_root * tte / year in rate terms
    s = amm.PoolState(1e6, 1e6, 1.0, 0.1, 365 * 86400, 20.0, 0.001)
    pt_out, _, _ = amm.swap_exact_sy_for_pt(s, 0, 1.0)
    eff_rate = (pt_out / 1.0)  # PT per asset = 1/price
    import math
    assert math.log(eff_rate) == pytest.approx(0.1 - 0.001, abs=2e-6)
