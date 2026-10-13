"""Minimal JSON-RPC helpers (eth_call / Multicall3) for archive reads."""
import time

import requests
from Crypto.Hash import keccak

RPC = {1: "https://eth.drpc.org", 42161: "https://arbitrum.drpc.org"}
ROUTER_V4 = "0x888888888889758f76e7103c6cbf23abbf58f946"
ROUTER_STATIC = "0x263833d47ea3fa4a30f269323aba6a107f9eb14c"
MULTICALL3 = "0xca11bde05977b3631167028862be2a173976ca11"


def selector(sig):
    k = keccak.new(digest_bits=256)
    k.update(sig.encode())
    return k.hexdigest()[:8]


def enc_addr(a):
    return a.lower().replace("0x", "").rjust(64, "0")


def enc_uint(x):
    return hex(int(x))[2:].rjust(64, "0")


def words(hexdata):
    h = hexdata[2:] if hexdata.startswith("0x") else hexdata
    return [int(h[i:i + 64], 16) for i in range(0, len(h), 64)]


def signed(x):
    return x - (1 << 256) if x >= 1 << 255 else x


def rpc(chain, method, params, retries=6):
    for attempt in range(retries):
        try:
            r = requests.post(RPC[chain], json=dict(jsonrpc="2.0", id=1, method=method,
                                                    params=params), timeout=60)
            j = r.json()
        except Exception:
            time.sleep(2 ** attempt)
            continue
        if "error" in j:
            msg = str(j["error"])
            if "rate" in msg.lower() or "limit" in msg.lower() or r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(msg)
        return j["result"]
    raise RuntimeError(f"rpc failed {method}")


def eth_call(chain, to, data, block="latest"):
    if isinstance(block, int):
        block = hex(block)
    return rpc(chain, "eth_call", [dict(to=to, data=data), block])


def multicall(chain, calls, block="latest"):
    """calls: list of (target, calldata). Returns list of (ok, returndata_hex)."""
    sel = selector("aggregate3((address,bool,bytes)[])")
    n = len(calls)
    head = enc_uint(32) + enc_uint(n)
    offsets, bodies, pos = [], [], 32 * n
    for target, data in calls:
        d = data[2:] if data.startswith("0x") else data
        dlen = len(d) // 2
        padded = d + "0" * ((64 - len(d) % 64) % 64)
        body = enc_addr(target) + enc_uint(1) + enc_uint(96) + enc_uint(dlen) + padded
        offsets.append(enc_uint(pos))
        bodies.append(body)
        pos += len(body) // 2
    payload = "0x" + sel + head + "".join(offsets) + "".join(bodies)
    out = eth_call(chain, MULTICALL3, payload, block)
    h = out[2:]
    w = lambda i: int(h[i * 64:(i + 1) * 64], 16)
    count = w(1)
    res = []
    for k in range(count):
        base = 128 + w(2 + k) * 2  # element offsets are relative to the word after length
        success = int(h[base:base + 64], 16) == 1
        data_off = int(h[base + 64:base + 128], 16) * 2
        dl = int(h[base + data_off:base + data_off + 64], 16)
        data = h[base + data_off + 64: base + data_off + 64 + dl * 2]
        res.append((success, "0x" + data))
    return res


def block_at(chain, ts):
    name = {1: "ethereum", 42161: "arbitrum"}[chain]
    for attempt in range(6):
        try:
            r = requests.get(f"https://coins.llama.fi/block/{name}/{int(ts)}", timeout=30)
            if r.status_code == 200:
                return r.json()["height"]
        except Exception:
            pass
        time.sleep(2 ** attempt)
    raise RuntimeError("block lookup failed")


def read_state(chain, market, block="latest"):
    """PendleMarket.readState(router) -> dict (float, 1e18 descaled)."""
    data = "0x" + selector("readState(address)") + enc_addr(ROUTER_V4)
    w = words(eth_call(chain, market, data, block))
    return dict(total_pt=signed(w[0]) / 1e18, total_sy=signed(w[1]) / 1e18,
                total_lp=signed(w[2]) / 1e18, scalar_root=signed(w[4]) / 1e18,
                expiry=w[5], ln_fee_root=w[6] / 1e18, reserve_fee_pct=w[7],
                ln_implied=w[8] / 1e18)
