"""What do real YT traders do, and who makes money?

Downloads every BUY_YT / SELL_YT router trade (with the wallet that sent it) for
expired stablecoin markets, rebuilds each wallet's round trips FIFO, and values
them with the realised SY index:

  cost        notional x YT price at entry   (YT price = 1 - exp(-ln(1+implied) x tau))
  interest    notional x (i_exit / i_entry - 1) x (1 - YT fee)   accrued while held
  proceeds    notional x YT price at exit, or 0 if held to maturity

All in accounting-asset units (~USD for stables). Points and external reward
tokens are NOT included: Pendle data carries no value for them.
"""
import json
import math
import os
import time
from collections import defaultdict, deque

import numpy as np
import pandas as pd
import requests

from pendle_bt.fetch_history import API, RAW

OUT = os.path.join(RAW, "yt_trades")
YEAR_S = 365 * 86400


def fetch_market(chain, addr):
    path = os.path.join(OUT, f"{chain}-{addr}.json")
    if os.path.exists(path):
        return json.load(open(path))
    rows = []
    for action in ("BUY_YT", "SELL_YT"):
        token = None
        while True:
            params = dict(action=action, limit=1000)
            if token:
                params["resumeToken"] = token
            for attempt in range(6):
                r = requests.get(f"{API}/v5/{chain}/transactions/{addr}", params=params, timeout=90)
                if r.status_code == 429:
                    time.sleep(20 * (attempt + 1))
                    continue
                r.raise_for_status()
                break
            d = r.json()
            rows += d["results"]
            if int(r.headers.get("x-ratelimit-remaining", "100")) < 20:
                time.sleep(20)
            token = d.get("resumeToken")
            if not token or len(d["results"]) < 1000:
                break
    json.dump(rows, open(path, "w"))
    return rows


def fetch_all(panel, min_peak_tvl=1e6):
    os.makedirs(OUT, exist_ok=True)
    now = pd.Timestamp.now()
    peak = panel.groupby("key").agg(tvl=("tvl", "max"), expiry=("expiry_ts", "first"))
    keys = peak[(peak.tvl >= min_peak_tvl) & (peak.expiry < now)].index
    for i, k in enumerate(keys):
        chain, addr = k.split("-")
        rows = fetch_market(int(chain), addr)
        if i % 20 == 0:
            print(i, len(keys), k, len(rows), flush=True)


def load_trades(panel):
    meta = panel.groupby("key").agg(expiry=("expiry_ts", "first"), sy=("sy", "first"),
                                    name=("name", "first"), fee=("interest_fee", "first"),
                                    start=("date", "min"), yld=("underlyingApy", "median"))
    out = []
    for f in os.listdir(OUT):
        key = f[:-5]
        if key not in meta.index:
            continue
        for t in json.load(open(os.path.join(OUT, f))):
            n = (t.get("notional") or {}).get("pt")
            if not n or t.get("impliedApy") is None or not t.get("txOrigin"):
                continue
            out.append(dict(key=key, ts=pd.Timestamp(t["timestamp"]).tz_localize(None),
                            wallet=t["txOrigin"].lower(), side=t["action"], notional=float(n),
                            implied=float(t["impliedApy"]), tx=t["txHash"]))
    tr = pd.DataFrame(out).drop_duplicates(["tx", "key", "side", "wallet"])
    tr = tr.join(meta, on="key")
    tr["tau"] = (tr.expiry - tr.ts).dt.total_seconds() / YEAR_S
    tr = tr[(tr.tau > 0) & tr.implied.between(-0.5, 5)]
    tr["yt_px"] = 1 - np.exp(-np.log1p(tr.implied) * tr.tau)
    tr["points_mkt"] = tr.yld < 0.005          # no real underlying yield: YT is a points play
    return tr.sort_values("ts").reset_index(drop=True)


def round_trips(tr, sy_index):
    """FIFO-match each wallet's YT buys with later sells in the same market."""
    idx = sy_index.set_index(["sy", "date"])["py_index"]

    def index_at(sy, ts):
        d = pd.Timestamp(ts).normalize()
        for k in range(4):
            v = idx.get((sy, d - pd.Timedelta(days=k)))
            if v is not None and v == v:
                return v
        return np.nan

    trips = []
    for (key, wallet), g in tr.groupby(["key", "wallet"], sort=False):
        lots = deque()
        sy, expiry, fee = g.sy.iloc[0], g.expiry.iloc[0], g.fee.iloc[0]
        for t in g.itertuples():
            if t.side == "BUY_YT":
                lots.append([t.notional, t.ts, t.yt_px, t.implied, t.tau])
                continue
            left = t.notional
            while left > 1e-9 and lots:
                lot = lots[0]
                q = min(left, lot[0])
                trips.append((key, wallet, q, lot[1], t.ts, lot[2], t.yt_px, lot[3], lot[4], False))
                lot[0] -= q
                left -= q
                if lot[0] <= 1e-9:
                    lots.popleft()
        for lot in lots:                       # never sold: held to maturity
            trips.append((key, wallet, lot[0], lot[1], expiry, lot[2], 0.0, lot[3], lot[4], True))
    rt = pd.DataFrame(trips, columns=["key", "wallet", "notional", "t_in", "t_out", "px_in",
                                      "px_out", "implied_in", "tau_in", "to_maturity"])
    meta = tr.groupby("key").agg(sy=("sy", "first"), fee=("fee", "first"), name=("name", "first"),
                                 points_mkt=("points_mkt", "first"), start=("start", "first"),
                                 expiry=("expiry", "first"))
    rt = rt.join(meta, on="key")
    rt["i_in"] = [index_at(s, t) for s, t in zip(rt.sy, rt.t_in)]
    rt["i_out"] = [index_at(s, t) for s, t in zip(rt.sy, rt.t_out)]
    rt["cost"] = rt.notional * rt.px_in
    rt["interest"] = rt.notional * (rt.i_out / rt.i_in - 1).clip(lower=0) * (1 - rt.fee.fillna(0.05))
    rt["proceeds"] = rt.notional * rt.px_out
    rt["pnl"] = rt.interest + rt.proceeds - rt.cost
    rt["ret"] = rt.pnl / rt.cost
    rt["hold_days"] = (rt.t_out - rt.t_in).dt.total_seconds() / 86400
    rt["days_to_exp_in"] = rt.tau_in * 365
    rt["age_in"] = (rt.t_in - rt.start).dt.total_seconds() / 86400
    return rt.dropna(subset=["i_in", "i_out"])


if __name__ == "__main__":
    from pendle_bt import panel as panel_mod
    df, _ = panel_mod.load()
    fetch_all(df)


# ------------------------------------------------------------------ Pendle's own wallet P&L
PNL_DIR = os.path.join(RAW, "wallet_pnl")


def fetch_wallet_pnl(wallets):
    """Pendle's per-market gained P&L for each wallet (all actions, actual token flows,
    claimed rewards included; points not included)."""
    os.makedirs(PNL_DIR, exist_ok=True)
    out = {}
    for w in wallets:
        path = os.path.join(PNL_DIR, f"{w}.json")
        if not os.path.exists(path):
            for attempt in range(6):
                r = requests.get(f"{API}/v1/pnl/gained/{w}/positions", timeout=90)
                if r.status_code == 429:
                    time.sleep(20 * (attempt + 1))
                    continue
                break
            if r.status_code != 200:
                continue
            json.dump(r.json(), open(path, "w"))
            if int(r.headers.get("x-ratelimit-remaining", "100")) < 20:
                time.sleep(20)
        out[w] = json.load(open(path))
    rows = []
    for w, d in out.items():
        for p in d.get("positions", []):
            rows.append(dict(wallet=w, key=f"{p['chainId']}-{p['market']}",
                             net_gain=p["pnl"]["netGain"]["usd"],
                             spent=p["pnl"]["totalSpent"]["usd"],
                             yt_spent=p["ytData"]["spent_v2"]["usd"],
                             pt_spent=p["ptData"]["spent_v2"]["usd"],
                             lp_spent=p["lpData"]["spent_v2"]["usd"]))
    return pd.DataFrame(rows)
