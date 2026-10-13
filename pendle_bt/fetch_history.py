"""Download daily market time series from the Pendle V2 API.

Universe: whitelisted Pendle markets tagged ``stables`` on Ethereum and
Arbitrum (both active and expired). One request per market returns the whole
life of the market at daily resolution (<= 1440 points).
"""
import json
import os
import sys
import time

import requests

API = "https://api-v2.pendle.finance/core"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "data", "raw")
FIELDS = ",".join([
    "timestamp", "impliedApy", "underlyingApy", "underlyingInterestApy",
    "underlyingRewardApy", "tvl", "totalTvl", "totalPt", "totalSy",
    "totalSupply", "ptPrice", "ytPrice", "syPrice", "lpPrice",
    "tradingVolume", "swapFeeApy", "pendleApy", "lpRewardApy", "baseApy",
])
CHAINS = (1, 42161)


def fetch_markets(refresh=False):
    path = os.path.join(RAW, "markets_all.json")
    if os.path.exists(path) and not refresh:
        return json.load(open(path))
    out = []
    for active in ("true", "false"):
        skip = 0
        while True:
            r = requests.get(f"{API}/v2/markets/all",
                             params=dict(isActive=active, limit=100, skip=skip), timeout=60)
            r.raise_for_status()
            d = r.json()
            out += d["results"]
            skip += 100
            if skip >= d["total"] or not d["results"]:
                break
    json.dump(out, open(path, "w"))
    return out


def universe(markets):
    return [m for m in markets
            if m["chainId"] in CHAINS and "stables" in m.get("categoryIds", [])]


def fetch_history(market):
    chain, addr = market["chainId"], market["address"]
    path = os.path.join(RAW, "history", f"{chain}-{addr}.json")
    if os.path.exists(path):
        return False
    for attempt in range(6):
        r = requests.get(f"{API}/v3/{chain}/markets/{addr}/historical-data",
                         params=dict(time_frame="day", fields=FIELDS), timeout=90)
        if r.status_code == 429:
            time.sleep(30 * (attempt + 1))
            continue
        if r.status_code >= 500:
            time.sleep(5 * (attempt + 1))
            continue
        r.raise_for_status()
        json.dump(r.json(), open(path, "w"))
        remaining = int(r.headers.get("x-ratelimit-remaining", "100"))
        if remaining < 20:
            time.sleep(20)
        return True
    raise RuntimeError(f"failed {chain}-{addr}")


def main():
    os.makedirs(os.path.join(RAW, "history"), exist_ok=True)
    mk = universe(fetch_markets())
    print(f"{len(mk)} markets in universe", flush=True)
    for i, m in enumerate(mk):
        try:
            got = fetch_history(m)
        except Exception as e:  # keep going; report at the end
            print("ERR", m["chainId"], m["address"], e, flush=True)
            continue
        if got and i % 20 == 0:
            print(i, m["name"], m["expiry"], flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    sys.exit(main())
