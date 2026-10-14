"""Snapshot of short-dated PT opportunities for the scanner page.

    python -m pendle_bt.pt_scanner            # fetch, quote, write scanner/pt_scanner.html

For every live USD-denominated Pendle market maturing within ~90 days:
  * real entry quotes from Pendle's Convert API (USD stable -> PT, aggregator
    routing included) at several ticket sizes, so price impact is measured,
    not modelled
  * the accounting asset's USD price, the SY's current underlying APY, pool
    liquidity and the total Pendle TVL of the underlying
  * looping venues (lending markets accepting the PT) from Pendle's looping catalog
  * Aave v3 USDC supply APY as the cash benchmark and live Ethereum gas
The page does the per-ticket / per-leverage arithmetic in the browser.
"""
import datetime as dt
import json
import math
import os
import statistics
import time

import requests

from pendle_bt.chain import rpc
from pendle_bt.fetch_history import API, RAW

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "scanner")
CHAINS = {1: ("Ethereum", "ethereum"), 42161: ("Arbitrum", "arbitrum"), 8453: ("Base", "base"),
          56: ("BNB Chain", "bsc"), 146: ("Sonic", "sonic"), 999: ("HyperEVM", "hyperliquid"),
          9745: ("Plasma", "plasma"), 143: ("Monad", "monad"), 196: ("X Layer", "xlayer"),
          5000: ("Mantle", "mantle"), 80094: ("Berachain", "berachain"), 10: ("Optimism", "optimism")}
STABLES = ["USDC", "USDT0", "USD₮0", "USDT", "USDC.E", "DAI", "USDS", "USDG", "PYUSD", "USD1",
           "USDE", "FDUSD", "AUSD"]
SIZES = [100, 1_000, 10_000, 50_000]
MAX_DAYS = 92
DEAD = "0x000000000000000000000000000000000000dEaD"
GAS = {"buy": 606_000, "redeem": 450_000, "loop_in": 1_230_000, "loop_out": 970_000}
L2_TX_USD = {"buy": 0.05, "redeem": 0.05, "loop_in": 0.15, "loop_out": 0.15}

_cu = {"remaining": 200}


def get(path, **params):
    for attempt in range(6):
        r = requests.get(f"{API}{path}", params=params, timeout=90)
        if r.status_code == 429:
            time.sleep(15 * (attempt + 1))
            continue
        _throttle(r)
        r.raise_for_status()
        return r.json()
    raise RuntimeError(path)


def _throttle(r):
    rem = int(r.headers.get("x-ratelimit-remaining", "100"))
    _cu["remaining"] = rem
    if rem < 25:
        time.sleep(20)


def convert(chain, token_in, amount_raw, pt):
    body = dict(receiver=DEAD, slippage=0.005, enableAggregator=True,
                inputs=[dict(token=token_in, amount=str(int(amount_raw)))], outputs=[pt])
    for attempt in range(5):
        r = requests.post(f"{API}/v3/sdk/{chain}/convert", json=body, timeout=90)
        if r.status_code == 429:
            time.sleep(15 * (attempt + 1))
            continue
        _throttle(r)
        if r.status_code >= 400:
            return None
        best = 0
        for route in r.json().get("routes", []):
            for o in route.get("outputs", []):
                if o["token"].lower() == pt.lower():
                    best = max(best, int(o["amount"]))
        return best or None
    return None


def llama_tokens(chain, addrs):
    slug = CHAINS[chain][1]
    out = {}
    addrs = sorted(set(a.lower() for a in addrs))
    for i in range(0, len(addrs), 60):
        ids = ",".join(f"{slug}:{a}" for a in addrs[i:i + 60])
        try:
            d = requests.get(f"https://coins.llama.fi/prices/current/{ids}", timeout=60).json()
        except Exception:
            continue
        for k, v in d.get("coins", {}).items():
            out[k.split(":")[1].lower()] = v
    return out


def cash_apy():
    d = requests.get("https://yields.llama.fi/chart/aa70268e-4b52-42bf-a116-608b370f9501",
                     timeout=60).json()["data"]
    last7 = [x["apy"] for x in d[-7:]]
    return statistics.mean(last7) / 100.0


def eth_usd_per_gas():
    fh = rpc(1, "eth_feeHistory", [hex(300), "latest", [50]])
    base = statistics.median(int(x, 16) for x in fh["baseFeePerGas"][:-1])
    tip = statistics.median(int(r[0], 16) for r in fh["reward"])
    eth = requests.get("https://coins.llama.fi/prices/current/coingecko:ethereum",
                       timeout=30).json()["coins"]["coingecko:ethereum"]["price"]
    return (base + tip) * 1e-18 * eth, (base + tip) / 1e9, eth


def exit_cost(chain, out_tokens, token_meta, underlying):
    """One-way cost of turning the redeemed asset back into a USD stable."""
    for t in out_tokens:
        m = token_meta.get(t.lower())
        if m and m.get("symbol", "").upper() in STABLES[:6] and abs(m.get("price", 0) - 1) < 0.01:
            return 0.0001, f"redeems straight to {m['symbol']}"
    measured = json.load(open(os.path.join(RAW, "conversion_costs.json"))) if os.path.exists(
        os.path.join(RAW, "conversion_costs.json")) else {}
    rt = measured.get(f"{chain}-{underlying.lower()}")
    if rt is not None and 0 <= rt < 0.05:
        return max(rt / 2, 0.0001), "measured DEX round trip"
    return 0.0015, "estimate (no measured route)"


def build():
    now = dt.datetime.now(dt.timezone.utc)
    mkts = []
    skip = 0
    while True:
        d = get("/v2/markets/all", isActive="true", limit=100, skip=skip)
        mkts += d["results"]
        skip += 100
        if skip >= d["total"]:
            break
    for m in mkts:
        m["days"] = (dt.datetime.fromisoformat(m["expiry"].replace("Z", "+00:00")) - now
                     ).total_seconds() / 86400
    # total Pendle TVL per underlying (all live maturities): an ex-ante size/quality proxy
    under_tvl = {}
    for m in mkts:
        under_tvl[m["underlyingAsset"]] = under_tvl.get(m["underlyingAsset"], 0) + (
            m["details"].get("totalTvl") or 0)
    cand = [m for m in mkts if m["chainId"] in CHAINS and 1.5 <= m["days"] <= MAX_DAYS
            and (m["details"].get("liquidity") or 0) >= 20_000]

    prices = {}
    for ch in {m["chainId"] for m in cand}:
        p = get("/v1/prices/assets", chainId=ch)
        prices.update({k.lower(): v for k, v in p.get("prices", {}).items()})

    loop = {}
    for p in get("/v2/pt-looping/loop/pts/looping").get("results", []):
        opts = []
        for o in p["info"]["options"]:
            x = o["data"]
            r = o.get("risks") or {}
            opts.append(dict(protocol=o["protocol"], debt=o["debtSymbol"], lltv=x["lltv"],
                             borrow=x["borrowApy"], borrow7d=x["borrowApy7dAvg"],
                             max_lev=x["maxLeverage"], liquidity=x["liquidityUsd"],
                             pt_oracle=r.get("ptOracleType"), debt_oracle=r.get("debtOracleType"),
                             url=o.get("marketUrl") or o.get("url")))
        loop[f"{p['chainId']}-{p['ptAddress']}".lower()] = opts

    pt_dec = {}
    for a in get("/v1/assets/all").get("assets", []):
        if "PT" in (a.get("tags") or []):
            pt_dec[f"{a['chainId']}-{a['address']}".lower()] = a["decimals"]
    gas_ppg, gwei, eth_px = eth_usd_per_gas()
    cash = cash_apy()
    rows = []
    for i, m in enumerate(sorted(cand, key=lambda x: x["days"])):
        ch = m["chainId"]
        acc = m["accountingAsset"].lower()
        pt = m["pt"].split("-")[1]
        try:
            tk = get(f"/v1/sdk/{ch}/markets/{m['address']}/tokens")
        except Exception:
            continue
        meta = llama_tokens(ch, tk.get("tokensIn", []) + tk.get("tokensRedeemSy", [])
                            + [acc.split("-")[1]])
        asset_px = prices.get(acc) or (meta.get(acc.split("-")[1]) or {}).get("price")
        if asset_px is None:
            pt_px = prices.get(m["pt"].lower())
            imp = m["details"].get("impliedApy") or 0
            if pt_px:
                asset_px = pt_px / math.exp(-math.log1p(imp) * m["days"] / 365)
        usd = asset_px is not None and 0.9 <= asset_px <= 1.1
        if not usd:
            continue
        # cheapest-to-source USD stable accepted by the router
        ins = []
        for t in tk.get("tokensIn", []):
            mm = meta.get(t.lower())
            if mm and abs(mm.get("price", 0) - 1) < 0.02 and mm.get("symbol", "").upper() in STABLES:
                ins.append((STABLES.index(mm["symbol"].upper()), t, mm))
        if not ins:
            continue
        ins.sort()
        _, tin, tmeta = ins[0]
        quotes, flags = [], []
        liq = m["details"].get("liquidity") or 0
        dec = pt_dec.get(f"{ch}-{pt}".lower(), 18)
        imp = m["details"].get("impliedApy") or 0
        mid_ppu = 1 / (asset_px * math.exp(-math.log1p(imp) * m["days"] / 365))
        for size in SIZES:
            if size > max(liq * 0.25, 1_000):
                break
            raw = size / tmeta["price"] * 10 ** tmeta["decimals"]
            pt_out = convert(ch, tin, raw, pt)
            if not pt_out:
                continue
            ppu = pt_out / 10 ** dec / size                     # PT per USD in
            if ppu < 0.9 * mid_ppu:                             # broken quote: drop it
                continue
            if ppu > mid_ppu:
                # a pool purchase cannot beat the mid price; any excess comes from buying the
                # asset below its reference price on a DEX (which you would also sell at) or
                # from the API's implied rate lagging the pool, so never count it
                if ppu > 1.003 * mid_ppu and "dex_discount" not in flags:
                    flags.append("dex_discount")
                ppu = mid_ppu
            quotes.append([size, ppu])
        if not quotes:
            continue
        ex_cost, ex_note = exit_cost(ch, tk.get("tokensRedeemSy", []), meta,
                                     m["underlyingAsset"].split("-")[1])
        if ch == 1:
            gas = {k: v * gas_ppg for k, v in GAS.items()}
        else:
            gas = dict(L2_TX_USD)
        cats = m.get("categoryIds", [])
        rows.append(dict(
            name=m["name"], chain=ch, chain_name=CHAINS[ch][0], address=m["address"], pt=pt,
            expiry=m["expiry"][:10], days=round(m["days"], 2),
            liquidity=liq, under_tvl=under_tvl.get(m["underlyingAsset"], 0),
            implied=m["details"].get("impliedApy"), underlying=m["details"].get("underlyingApy"),
            fee_rate=m["details"].get("feeRate"), asset_px=asset_px,
            points=("points" in cats), categories=cats, protocol=m.get("protocol"),
            quote_token=tmeta.get("symbol"), quotes=quotes, mid_ppu=mid_ppu, flags=flags,
            exit_cost=ex_cost, exit_note=ex_note,
            gas=gas, loop=loop.get(f"{ch}-{pt}".lower(), [])))
        print(f"{i + 1}/{len(cand)} {m['name']} {CHAINS[ch][0]} {m['days']:.0f}d quotes={len(quotes)}",
              flush=True)
    return dict(asof=now.strftime("%Y-%m-%d %H:%M UTC"), cash_apy=cash, eth_gwei=gwei,
                eth_usd=eth_px, markets=rows)


def render(snapshot):
    tpl = open(os.path.join(OUT_DIR, "template.html")).read()
    data = json.dumps(snapshot, separators=(",", ":")).replace("</", "<\\/")
    html = tpl.replace("/*__SNAPSHOT__*/null", data)
    path = os.path.join(OUT_DIR, "pt_scanner.html")
    open(path, "w").write(html)
    return path


def main():
    snap = build()
    os.makedirs(OUT_DIR, exist_ok=True)
    json.dump(snap, open(os.path.join(OUT_DIR, "snapshot.json"), "w"), indent=1)
    print(render(snap), len(snap["markets"]), "markets")


if __name__ == "__main__":
    main()
