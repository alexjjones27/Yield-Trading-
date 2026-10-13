"""Read per-market AMM parameters and YT interest fee from chain.

For each market we read, at a block in the middle of its life:
  readState(router) -> scalarRoot, lnFeeRateRoot, reserveFeePercent
  YT.factory().interestFeeRate()  -> fee on YT yield (3% older, 5% newer)
"""
import datetime as dt
import json
import os

from pendle_bt.chain import (block_at, eth_call, enc_addr, multicall, selector, signed,
                             words, ROUTER_V4)
from pendle_bt.fetch_history import RAW, fetch_markets, universe

OUT = os.path.join(RAW, "market_params.json")


def ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def fetch_one(m, latest=False):
    chain = m["chainId"]
    mk = m["address"]
    yt = m["yt"].split("-")[1]
    start, end = ts(m["timestamp"]), ts(m["expiry"])
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    mid = min(start + 0.5 * (min(end, now) - start), now - 3600)
    blk = "latest" if latest else block_at(chain, mid)
    calls = [(mk, "0x" + selector("readState(address)") + enc_addr(ROUTER_V4)),
             (mk, "0x" + selector("scalarRoot()")),
             (yt, "0x" + selector("factory()"))]
    res = multicall(chain, calls, blk)
    out = dict(chain=chain, market=mk, block=blk)
    if res[0][0]:
        w = words(res[0][1])
        out.update(scalar_root=signed(w[4]) / 1e18, ln_fee_root=w[6] / 1e18,
                   reserve_fee_pct=w[7], source="readState")
    elif res[1][0]:
        out.update(scalar_root=signed(words(res[1][1])[0]) / 1e18, source="getters")
        for name in ("lnFeeRateRoot()", "reserveFeePercent()"):
            try:
                v = words(eth_call(chain, mk, "0x" + selector(name), blk))[0]
                out["ln_fee_root" if name.startswith("ln") else "reserve_fee_pct"] = (
                    v / 1e18 if name.startswith("ln") else v)
            except Exception:
                pass
    if res[2][0]:
        fac = "0x" + res[2][1][-40:]
        try:
            out["interest_fee"] = words(eth_call(chain, fac, "0x" + selector("interestFeeRate()"),
                                                 blk))[0] / 1e18
        except Exception:
            pass
    return out


def main():
    params = json.load(open(OUT)) if os.path.exists(OUT) else {}
    for m in universe(fetch_markets()):
        key = f"{m['chainId']}-{m['address']}"
        if key in params:
            continue
        try:
            params[key] = fetch_one(m)
        except Exception as e:
            # no archive state (e.g. Arbitrum on the free RPC): scalarRoot is immutable,
            # so read it (and the current fee) at the latest block instead
            try:
                params[key] = fetch_one(m, latest=True)
                params[key]["source"] += "@latest"
            except Exception as e2:
                print("ERR", key, m["name"], e, e2, flush=True)
                continue
        print(key, m["name"], params[key], flush=True)
        json.dump(params, open(OUT, "w"), indent=1)
    json.dump(params, open(OUT, "w"), indent=1)


if __name__ == "__main__":
    main()
