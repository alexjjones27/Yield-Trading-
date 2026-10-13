"""Gas model inputs.

1. Gas used per Pendle action, measured from real router transactions
   (receipts of single-action txs sent straight to Router V4).
2. Daily Ethereum base fee and median priority fee (eth_feeHistory over the
   1024 blocks ending 14:00 UTC each day).
3. Daily ETH/USD from DefiLlama.
"""
import datetime as dt
import json
import os
import statistics
import time

import requests

from pendle_bt.chain import ROUTER_V4, block_at, rpc
from pendle_bt.fetch_history import API, RAW

ACTIONS = ["BUY_PT", "SELL_PT", "BUY_YT", "SELL_YT", "ADD_LIQUIDITY", "REMOVE_LIQUIDITY"]
SAMPLE_MARKETS = {
    1: ["0x47ad2cd1dd15739a7a035b9d3b7828d916fef77e", "0x177768caf9d0e036725a51d3f60d7e20f2d4d194",
        "0x4b912476356c914b154524c59b5a438a88e99429", "0x5e572498e9f83650f0ff24194999bddb4b390928"],
}


def gas_per_action():
    out = {}
    for chain, markets in SAMPLE_MARKETS.items():
        for action in ACTIONS:
            used = []
            for mk in markets:
                r = requests.get(f"{API}/v5/{chain}/transactions/{mk}",
                                 params=dict(action=action, limit=40), timeout=60).json()
                for tx in r.get("results", []):
                    if len(used) >= 60:
                        break
                    t = rpc(chain, "eth_getTransactionByHash", [tx["txHash"]])
                    if not t or (t.get("to") or "").lower() != ROUTER_V4:
                        continue
                    rc = rpc(chain, "eth_getTransactionReceipt", [tx["txHash"]])
                    used.append(int(rc["gasUsed"], 16))
            if used:
                out[action] = dict(n=len(used), median=statistics.median(used),
                                   p75=sorted(used)[int(0.75 * (len(used) - 1))])
            print(action, out.get(action), flush=True)
    return out


def _fee_history(blk):
    for count in (512, 256, 128):
        for attempt in range(3):
            try:
                return rpc(1, "eth_feeHistory", [hex(count), hex(blk), [50]])
            except Exception:
                time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"feeHistory failed at {blk}")


def _gas_day(day):
    t = dt.datetime.combine(day, dt.time(14), dt.timezone.utc).timestamp()
    blk = block_at(1, t)
    fh = _fee_history(blk)
    base = [int(x, 16) for x in fh["baseFeePerGas"][:-1]]
    tip = [int(r[0], 16) for r in fh["reward"]]
    return dict(date=day.isoformat(), base_gwei=statistics.median(base) / 1e9,
                tip_gwei=statistics.median(tip) / 1e9)


def daily_gas(start, end, done=None, save=None, workers=6):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    rows = list(done or [])
    seen = {r["date"] for r in rows}
    todo = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
    todo = [d for d in todo if d.isoformat() not in seen]
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(_gas_day, d): d for d in todo}
        for f in as_completed(futs):
            try:
                rows.append(f.result())
            except Exception as e:
                print("ERR", futs[f], e, flush=True)
            if save and len(rows) % 50 == 0:
                save(sorted(rows, key=lambda r: r["date"]))
                print(len(rows), flush=True)
    return sorted(rows, key=lambda r: r["date"])


def eth_price(start, end):
    s = int(dt.datetime.combine(start, dt.time(0), dt.timezone.utc).timestamp())
    n = (end - start).days + 1
    r = requests.get("https://coins.llama.fi/chart/coingecko:ethereum",
                     params=dict(start=s, span=n, period="1d"), timeout=60).json()
    pts = r["coins"]["coingecko:ethereum"]["prices"]
    return {dt.datetime.fromtimestamp(p["timestamp"], dt.timezone.utc).date().isoformat(): p["price"]
            for p in pts}


def main():
    path = os.path.join(RAW, "gas.json")
    data = json.load(open(path)) if os.path.exists(path) else {}
    if "gas_used" not in data:
        data["gas_used"] = gas_per_action()
        json.dump(data, open(path, "w"), indent=1)
    start, end = dt.date(2023, 6, 1), dt.date.today() - dt.timedelta(days=1)
    if "eth_usd" not in data:
        prices = {}
        d = start
        while d <= end:
            chunk_end = min(d + dt.timedelta(days=360), end)
            prices.update(eth_price(d, chunk_end))
            d = chunk_end + dt.timedelta(days=1)
        data["eth_usd"] = prices
        json.dump(data, open(path, "w"), indent=1)
    if len(data.get("daily", [])) < (end - start).days + 1:
        def save(rows):
            data["daily"] = rows
            json.dump(data, open(path, "w"), indent=1)
        save(daily_gas(start, end, data.get("daily"), save))
    print("done")


if __name__ == "__main__":
    main()
