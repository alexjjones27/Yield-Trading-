"""Measure USDC <-> underlying conversion cost with live KyberSwap quotes.

For every SY input token used by a market in the universe we quote
USDC -> token -> USDC for a $10k notional and record the one-way cost as half
of the round-trip loss. Tokens with no route (dead / illiquid) are left out and
fall back to the default tier in ``costs.py``.
"""
import json
import os
import time

import requests

from pendle_bt.fetch_history import RAW, fetch_markets, universe

KYBER = "https://aggregator-api.kyberswap.com/{chain}/api/v1/routes"
CHAIN_NAME = {1: "ethereum", 42161: "arbitrum"}
USDC = {1: "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        42161: "0xaf88d065e77c8cc2239327c5edb3a432268e5831"}


def quote(chain, tin, tout, amount):
    for attempt in range(4):
        try:
            r = requests.get(KYBER.format(chain=CHAIN_NAME[chain]),
                             params=dict(tokenIn=tin, tokenOut=tout, amountIn=str(int(amount))),
                             headers={"x-client-id": "pendle-backtest"}, timeout=30)
            j = r.json()
            if j.get("code") == 0:
                return int(j["data"]["routeSummary"]["amountOut"])
            return None
        except Exception:
            time.sleep(2 ** attempt)
    return None


def round_trip(chain, token, usd=10_000):
    if token == USDC[chain]:
        return 0.0
    out = quote(chain, USDC[chain], token, usd * 1e6)
    if not out:
        return None
    back = quote(chain, token, USDC[chain], out)
    if not back:
        return None
    return 1 - back / (usd * 1e6)


def main():
    path = os.path.join(RAW, "conversion_costs.json")
    res = json.load(open(path)) if os.path.exists(path) else {}
    tokens = set()
    for m in universe(fetch_markets()):
        for t in m.get("inputTokens", []) + m.get("outputTokens", []) + [m["underlyingAsset"]]:
            tokens.add(t)
    for t in sorted(tokens):
        if t in res:
            continue
        chain, addr = t.split("-")
        rt = round_trip(int(chain), addr)
        res[t] = rt
        print(t, rt, flush=True)
        json.dump(res, open(path, "w"), indent=1)
        time.sleep(0.3)


if __name__ == "__main__":
    main()
