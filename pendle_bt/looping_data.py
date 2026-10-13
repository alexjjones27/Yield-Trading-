"""Data for PT looping (leveraged PT) backtests.

Morpho Blue markets whose collateral is a stablecoin Pendle PT (Ethereum and
Arbitrum), with
  * daily borrow APY, borrowed and available liquidity (Morpho API)
  * the PT oracle type, read on-chain from the oracle's base feed:
      linear discount (``baseDiscountPerYear()``) or Pendle market TWAP
      (``twapDuration()``)
  * every liquidation in those markets
"""
import json
import os
import time

import pandas as pd
import requests

from pendle_bt.chain import multicall, selector, words
from pendle_bt.fetch_history import RAW

MORPHO = "https://api.morpho.org/graphql"
OUT = os.path.join(RAW, "looping")


def gql(query, variables=None):
    for attempt in range(6):
        try:
            r = requests.post(MORPHO, json={"query": query, "variables": variables or {}},
                              timeout=120)
            d = r.json()
            if "errors" in d and any("rate" in str(e).lower() for e in d["errors"]):
                time.sleep(5 * (attempt + 1))
                continue
            return d
        except Exception:
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("morpho api failed")


def fetch_markets(markets_all):
    stab = [m for m in markets_all
            if m["chainId"] in (1, 42161) and "stables" in m.get("categoryIds", [])]
    pts = {m["pt"].split("-")[1].lower(): m for m in stab}
    addrs, out = list(pts), []
    q = """query($a:[String!]){ markets(first:500, where:{collateralAssetAddress_in:$a,
      chainId_in:[1,42161]}){ items{ marketId lltv creationTimestamp chain{id}
      collateralAsset{address} loanAsset{symbol address}
      oracle{ address data{ ... on MorphoChainlinkOracleV2Data { baseFeedOne{ address } }
                            ... on MorphoChainlinkOracleData { baseFeedOne{ address } } } } } } }"""
    for i in range(0, len(addrs), 100):
        out += gql(q, {"a": addrs[i:i + 100]})["data"]["markets"]["items"]
    # classify PT price feed
    feeds = {}
    for x in out:
        f = (((x.get("oracle") or {}).get("data") or {}).get("baseFeedOne") or {}).get("address")
        x["feed"] = (f or "").lower()
        if f:
            feeds.setdefault(x["chain"]["id"], set()).add(f.lower())
    kind = {}
    for ch, fs in feeds.items():
        fs = sorted(fs)
        calls = []
        for f in fs:
            calls += [(f, "0x" + selector("baseDiscountPerYear()")),
                      (f, "0x" + selector("twapDuration()"))]
        res = []
        for i in range(0, len(calls), 100):
            res += multicall(ch, calls[i:i + 100])
        for k, f in enumerate(fs):
            (ok1, d1), (ok2, d2) = res[2 * k], res[2 * k + 1]
            if ok1 and len(d1) > 2:
                kind[f] = ("linear", words(d1)[0] / 1e18)
            elif ok2 and len(d2) > 2:
                kind[f] = ("market", None)
            else:
                kind[f] = ("unknown", None)
    rows = []
    for x in out:
        m = pts[x["collateralAsset"]["address"].lower()]
        k, disc = kind.get(x["feed"], ("unknown", None))
        rows.append(dict(market_id=x["marketId"], chain=x["chain"]["id"],
                         key=f"{m['chainId']}-{m['address']}", name=m["name"],
                         lltv=int(x["lltv"]) / 1e18, loan=x["loanAsset"]["symbol"],
                         created=pd.to_datetime(x["creationTimestamp"], unit="s"),
                         oracle=k, linear_discount=disc))
    return pd.DataFrame(rows)


def fetch_history(mkts):
    path = os.path.join(OUT, "morpho_history.json")
    hist = json.load(open(path)) if os.path.exists(path) else {}
    q = """query($id:String!,$c:Int!){ marketById(marketId:$id, chainId:$c){ historicalState{
      borrowApy(options:{interval:DAY}){ x y }
      liquidityAssetsUsd(options:{interval:DAY}){ x y }
      borrowAssetsUsd(options:{interval:DAY}){ x y } } } }"""
    for i, r in enumerate(mkts.itertuples()):
        if r.market_id in hist:
            continue
        d = gql(q, {"id": r.market_id, "c": int(r.chain)})
        hs = (((d.get("data") or {}).get("marketById") or {}).get("historicalState") or {})
        hist[r.market_id] = hs
        if i % 25 == 0:
            json.dump(hist, open(path, "w"))
            print(i, len(mkts), flush=True)
    json.dump(hist, open(path, "w"))
    rows = []
    for mid, hs in hist.items():
        series = {k: {p["x"]: p["y"] for p in (v or [])} for k, v in hs.items()}
        for ts in series.get("borrowApy", {}):
            rows.append(dict(market_id=mid, ts=ts, borrow_apy=series["borrowApy"][ts],
                             liquidity_usd=series.get("liquidityAssetsUsd", {}).get(ts),
                             borrow_usd=series.get("borrowAssetsUsd", {}).get(ts)))
    h = pd.DataFrame(rows)
    h["date"] = pd.to_datetime(h.ts, unit="s").dt.normalize()
    return h.groupby(["market_id", "date"]).last().reset_index()


def fetch_liquidations(mkts):
    path = os.path.join(OUT, "morpho_liquidations.json")
    if os.path.exists(path):
        return pd.DataFrame(json.load(open(path)))
    q = """query($ids:[String!],$skip:Int){ marketTransactions(first:1000, skip:$skip,
      where:{marketUniqueKey_in:$ids, type_in:[Liquidation]}){ items{ timestamp txHash
      user{ address } market{ marketId loanAsset{ decimals } collateralAsset{ decimals } }
      data{ ... on MarketTransactionLiquidationData {
        repaidAssets seizedAssets badDebtAssets } } } } }"""
    rows, ids = [], list(mkts.market_id)
    for i in range(0, len(ids), 50):
        skip = 0
        while True:
            d = gql(q, {"ids": ids[i:i + 50], "skip": skip})
            if "errors" in d:
                print(d["errors"][:1])
                break
            items = d["data"]["marketTransactions"]["items"]
            for it in items:
                dd = it.get("data") or {}
                ld = 10 ** it["market"]["loanAsset"]["decimals"]
                cd = 10 ** it["market"]["collateralAsset"]["decimals"]
                rows.append(dict(market_id=it["market"]["marketId"], ts=it["timestamp"],
                                 hash=it["txHash"], user=(it.get("user") or {}).get("address"),
                                 repaid=int(dd.get("repaidAssets") or 0) / ld,
                                 seized=int(dd.get("seizedAssets") or 0) / cd,
                                 bad_debt=int(dd.get("badDebtAssets") or 0) / ld))
            if len(items) < 1000:
                break
            skip += 1000
    json.dump(rows, open(path, "w"))
    return pd.DataFrame(rows)


def main():
    os.makedirs(OUT, exist_ok=True)
    mk = json.load(open(os.path.join(RAW, "markets_all.json")))
    mkts = fetch_markets(mk)
    mkts.to_csv(os.path.join(OUT, "morpho_pt_markets.csv"), index=False)
    print(mkts.oracle.value_counts(), flush=True)
    h = fetch_history(mkts)
    h.to_pickle(os.path.join(OUT, "morpho_history.pkl"))
    liq = fetch_liquidations(mkts)
    print("history rows", len(h), "liquidations", len(liq), flush=True)


if __name__ == "__main__":
    main()
