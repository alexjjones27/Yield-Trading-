"""Live scan: apply the backtested PT dislocation rules to today's markets.

    python -m pendle_bt.scan                 # $10k portfolio, $5k ticket
    python -m pendle_bt.scan --capital 25000

Pool reserves, implied rate and SY index come from chain at the latest block;
trailing yield forecasts come from the daily panel (data through today's
00:00 UTC snapshot). Each market is quoted for a real entry through the exact
AMM math plus gas and conversion, exactly as the backtest engine does.
"""
import argparse
import datetime as dt
import json
import math
import os
import statistics

import numpy as np
import pandas as pd

from pendle_bt import panel as panel_mod
from pendle_bt.chain import ROUTER_V4, enc_addr, multicall, rpc, selector, signed, words
from pendle_bt.costs import CostModel
from pendle_bt.engine import Engine, Params
from pendle_bt.fetch_history import RAW, fetch_markets, universe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
YEAR_S = 365 * 86400

# most recent walk-forward selection (results/summary.json -> recommended.params)
REC = dict(instruments=("pt",), forecast="ema30", blend_w=1.0, achievable="max_cash",
           pt_entry=0.01, pt_exit=0.0, pt_min_carry=0.02, min_tvl=1_000_000.0,
           min_under_tvl=0.0, max_frac_market=0.5, min_tau_pt=60)


def live_state(markets):
    """readState / SY.exchangeRate / YT.pyIndexStored at the latest block."""
    out = {}
    for chain in sorted({m["chainId"] for m in markets}):
        ms = [m for m in markets if m["chainId"] == chain]
        calls = []
        for m in ms:
            calls += [(m["address"], "0x" + selector("readState(address)") + enc_addr(ROUTER_V4)),
                      (m["sy"].split("-")[1], "0x" + selector("exchangeRate()")),
                      (m["yt"].split("-")[1], "0x" + selector("pyIndexStored()"))]
        blk = int(rpc(chain, "eth_blockNumber", []), 16) - 5
        ts = int(rpc(chain, "eth_getBlockByNumber", [hex(blk), False])["timestamp"], 16)
        res = []
        for i in range(0, len(calls), 60):
            res += multicall(chain, calls[i:i + 60], blk)
        for k, m in enumerate(ms):
            (ok1, st), (ok2, er), (ok3, pi) = res[3 * k:3 * k + 3]
            if not (ok1 and ok2):
                continue
            w = words(st)
            out[f"{chain}-{m['address']}"] = dict(
                total_pt=signed(w[0]) / 1e18, total_sy=signed(w[1]) / 1e18,
                ln_implied=w[8] / 1e18, ln_fee_root=w[6] / 1e18,
                index=words(er)[0] / 1e18,
                py_stored=words(pi)[0] / 1e18 if ok3 else 0.0, ts=ts, block=blk)
    return out


def _snap(chain_val, api_val):
    """Rescale a raw on-chain value (descaled by 1e18) to the API's human units.

    6-decimal tokens come back 1e12 too small; the two sources differ only by a
    power of ten plus intraday drift, so snap to the nearest power of ten."""
    if chain_val <= 0 or not api_val or api_val <= 0:
        return chain_val
    return chain_val * 10 ** round(math.log10(api_val / chain_val))


def live_gas_usd_per_gas(eth_usd):
    fh = rpc(1, "eth_feeHistory", [hex(300), "latest", [50]])
    base = statistics.median(int(x, 16) for x in fh["baseFeePerGas"][:-1])
    tip = statistics.median(int(r[0], 16) for r in fh["reward"])
    return (base + tip) * 1e-9 * 1e-9 * eth_usd


def scan(capital=10_000.0, params=None, refresh=True):
    p = Params(**(params or REC))
    if refresh:
        fetch_markets(refresh=True)
    meta = {f"{m['chainId']}-{m['address']}": m for m in fetch_markets()}
    now = pd.Timestamp.now(tz="UTC").tz_localize(None)
    live = [m for m in universe(meta.values())
            if pd.Timestamp(m["expiry"]).tz_localize(None) > now]

    df, sy = panel_mod.load()
    last = df.sort_values("date").groupby("key").tail(1).set_index("key")
    cash = panel_mod.cash_apy()
    costs = CostModel()
    g = json.load(open(os.path.join(RAW, "gas.json")))
    eth = g["eth_usd"][max(g["eth_usd"])]
    costs._gas_daily.loc[now.normalize()] = live_gas_usd_per_gas(eth)
    costs._gas_daily = costs._gas_daily.sort_index()

    recent = df[df.date >= df.date.max() - pd.Timedelta(days=2)]
    eng = Engine(recent, sy, cash, costs, p, capital=capital)
    state = live_state(live)
    cash_ref = eng.cash_rate(df.date.max())

    rows = []
    for m in live:
        key = f"{m['chainId']}-{m['address']}"
        if key not in last.index or key not in state:
            rows.append(dict(market=m["name"], chain=m["chainId"], expiry=m["expiry"][:10],
                             verdict="skip", reason="no history yet (new market)"))
            continue
        r = last.loc[key].to_dict()
        r["key"] = key
        s = state[key]
        idx = _snap(s["index"], r["index"])
        py = _snap(s["py_stored"], r["index"]) if s["py_stored"] > 0 else idx
        r.update(totalPt=_snap(s["total_pt"], r["totalPt"]),
                 totalSy=_snap(s["total_sy"], r["totalSy"]), ln_implied=s["ln_implied"],
                 impliedApy=math.expm1(s["ln_implied"]), index=idx, py_index=max(idx, py),
                 ln_fee_root=s["ln_fee_root"], tvl=m["details"].get("liquidity") or r["tvl"])
        r["tau"] = (r["expiry_ts"] - now).total_seconds() / YEAR_S
        r["pt_mid"] = math.exp(-r["ln_implied"] * r["tau"])
        base = dict(market=m["name"], chain={1: "Ethereum", 42161: "Arbitrum"}[m["chainId"]],
                    expiry=m["expiry"][:10], days=round(r["tau"] * 365),
                    liquidity=r["tvl"], under_tvl=r["under_tvl"],
                    implied=r["impliedApy"], own_yield=r[p.forecast],
                    achievable=eng.achievable(r, now.normalize()), fee=r["ln_fee_root"])
        reasons = []
        if r["tvl"] < p.min_tvl:
            reasons.append(f"liquidity ${r['tvl'] / 1e6:.2f}M < $1M")
        if r["tau"] * 365 < p.min_tau_pt:
            reasons.append(f"{r['tau'] * 365:.0f}d to expiry < 60")
        if (r["days_hist"] or 0) < p.min_hist:
            reasons.append("< 14d yield history")
        if r["impliedApy"] <= 0:
            reasons.append("implied <= 0")
        if r["ln_fee_root"] > p.max_rate_impact:
            reasons.append(f"Pendle fee {r['ln_fee_root']:.2%} > {p.max_rate_impact:.1%} rate cap")
        cap = eng.size_cap(r, "pt", capital)
        usd = min(cap, capital)
        q = eng._quote_mixed(r, now, "pt", usd, 0.0, 0.0) if usd > 0 else None
        if q is None:
            rows.append(dict(base, verdict="skip", reason="; ".join(reasons + ["no AMM quote"])))
            continue
        tries = 0
        while (r["ln_fee_root"] <= p.max_rate_impact and not eng.rate_impact_ok(r, q, "pt")
               and tries < 12):
            usd *= 0.6
            q = eng._quote_mixed(r, now, "pt", usd, 0.0, 0.0)
            tries += 1
            if q is None:
                break
        if q is None:
            rows.append(dict(base, verdict="skip", reason="; ".join(reasons + ["too thin"])))
            continue
        allin = eng.pt_allin_apy(r, now, q)
        edge = allin - base["achievable"]
        carry = allin - cash_ref
        if edge < p.pt_entry:
            reasons.append(f"edge {edge:+.1%} < +1%")
        if carry < p.pt_min_carry:
            reasons.append(f"carry over cash {carry:+.1%} < +2%")
        if usd < p.min_trade_usd:
            reasons.append("size too small")
        rows.append(dict(base, ticket=usd, allin=allin, edge=edge, carry=carry,
                         cost_bps=(q["gas"] + q["conversion"] + q["amm_fee"] + q["amm_impact"])
                         / usd * 1e4,
                         core=r["under_tvl"] >= 2.5e7,
                         verdict="BUY" if not reasons else "pass",
                         reason="; ".join(reasons)))
    out = pd.DataFrame(rows).sort_values(["verdict", "edge"], ascending=[True, False])
    return out, dict(cash_ref=cash_ref, asof=str(now), data_through=str(df.date.max().date()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=10_000.0)
    ap.add_argument("--no-refresh", action="store_true")
    a = ap.parse_args()
    out, info = scan(a.capital, refresh=not a.no_refresh)
    os.makedirs(os.path.join(ROOT, "results"), exist_ok=True)
    out.to_csv(os.path.join(ROOT, "results", "live_scan.csv"), index=False)
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)
    print(info)
    print(out.to_string(index=False))


if __name__ == "__main__":
    main()
