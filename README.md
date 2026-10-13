# Pendle fixed vs floating yield dislocation backtest

Backtest and walk-forward optimisation of a **$10,000** USD portfolio that trades
Pendle PT, YT and LP positions when a market's implied (fixed) APY diverges from
the floating yield actually achievable on the underlying, with execution costs
modelled from on-chain data rather than flat assumptions.

Results and the write-up are in [`RESULTS.md`](RESULTS.md); charts and raw
outputs are in [`results/`](results/).

## What is modelled, and how

| Cost / effect | How it is modelled | Source |
|---|---|---|
| Pendle swap fee + price impact | Exact float port of Pendle V2 `MarketMathCore` (logit curve, rate anchor, `lnFeeRateRoot` fee, 80% reserve share) applied to each day's real pool reserves. PT, YT (flash mint/redeem) and single-sided LP zaps. | `pendle_bt/amm.py` |
| AMM correctness | 66 quotes recorded from Pendle's on-chain `RouterStatic` at one block across 9 live pools: PT→SY and YT→SY match to 1e-15; SY→PT and SY→YT within the router's ~1e-4 approximation tolerance (1 bp haircut applied). | `tests/test_amm.py` |
| Curve parameters | `scalarRoot`, `lnFeeRateRoot`, `reserveFeePercent` read per market via `readState(router)` at a mid-life archive block. | `pendle_bt/fetch_params.py` |
| YT interest fee | Read from each YT's factory `interestFeeRate()` (3% on older factories, 5% on newer ones; one factory switched 3%→5% in 2025). | on-chain |
| Gas units | Medians of 360 real Pendle Router V4 transactions: buy PT 556k, sell PT 524k, buy YT 436k, sell YT 447k, add LP 563k, remove LP 556k. Plus redeem 450k, claim 250k, approval 50k, Aave move 200k. | `pendle_bt/fetch_gas.py` |
| Gas price | Daily median base fee + median priority fee (`eth_feeHistory`, 512 blocks ending 14:00 UTC), trailing 3-day median, × daily ETH/USD. Arbitrum: $0.10/tx. | archive RPC, DefiLlama |
| USDC ↔ underlying conversion | Live KyberSwap round-trip quotes USDC→token→USDC at $10k, half the loss = one-way cost, cheapest of the SY's accepted tokens; 25 bps default when no route exists today. | `pendle_bt/fetch_conversion.py` |
| Idle cash | Earns the Aave v3 USDC supply rate (daily), with gas for each deposit/withdrawal. | DefiLlama yields |
| Floating yield / YT accrual | SY→asset index backed out of Pendle's PT/SY USD prices (matches reported APY), made monotone like Pendle's PY index; YT interest = units × (1/i₀ − 1/i₁), less the YT fee. | `pendle_bt/panel.py` |
| LP valuation | Pool reserves per LP token (matches Pendle's `lpPrice` to 0.01% on 32k market-days) + PENDLE emissions (trailing 7d median, 5% haircut, claim gas). | `pendle_bt/engine.py` |
| Depegs | Positions are valued in USD through the accounting asset's USD price (from Pendle's PT price), so a depeg of the asset shows up in P&L. | `pendle_bt/panel.py` |
| Liquidity limits | Trade ≤ 2% of pool TVL, PT entry rate impact ≤ 40 bps, YT break-even impact cap; pools must have ≥ $1M liquidity. | `pendle_bt/engine.py` |

Equity is reported as **liquidation value**: every open position is marked at
what selling it through the AMM would return that day after fee, impact, gas and
conversion. If a pool cannot absorb the sale (e.g. PT at the 96% proportion cap
after a sell-off) the position is marked at hold-to-maturity value discounted at
implied + 5%.

## Strategy families

* **PT dislocation**: buy PT when the all-in fixed APY (after every entry and
  expected exit cost) exceeds the achievable floating yield, `max(trailing
  realised SY yield, smoothed Aave USDC rate)`, by `pt_entry`; sell early when the
  remaining hold-to-maturity yield falls below that benchmark by `pt_exit`;
  otherwise redeem at maturity and roll the SY into the next maturity of the same
  token without a conversion.
* **PT carry (no signal)**: control. Buy the best all-in carry over cash, hold to maturity.
* **YT dislocation**: buy YT when the trailing realised yield exceeds the YT
  break-even yield (after fee, impact, gas, conversion) by `yt_entry`.
* **LP carry**: provide liquidity when expected LP APY (PT leg at implied, SY leg
  at forecast, swap fees, PENDLE) beats cash and the market is not dislocated.
* **Combined**: all of the above sharing the book, ranked by expected excess APY.

## Avoiding look-ahead and overfitting

* Signals use only data available at that day's 00:00 UTC snapshot (trailing
  EMAs of realised yield, current pool state). `lag=1` stress-tests trading on
  the previous day's signal.
* Parameters are chosen by **walk-forward** (expanding window): train from
  2024-01-01, test the next 6 months, repeat. Reported strategy numbers are the
  chained out-of-sample segments only.
* Expired and live markets are both included (317 stablecoin markets on
  Ethereum and Arbitrum, 2023-03 to 2026-10).

## Reproduce

```bash
pip install -r requirements.txt
tar xzf data/raw_snapshot_2026-10-13.tar.gz -C data   # or re-download below
python -m pytest -q tests                              # AMM vs on-chain quotes
python -m pendle_bt.run                                # study + search + walk-forward (~30 min)
python -m pendle_bt.report                             # charts
```

Re-downloading the data (`python -m pendle_bt.fetch_history`, `fetch_params`,
`fetch_gas`, `fetch_conversion`) uses the public Pendle API, a free archive RPC
(drpc.org), DefiLlama and KyberSwap.

## Known limitations

* Points, external SY reward tokens and Pendle limit-order fills are not
  credited (conservative for YT/LP; limit orders can only improve fills).
* Our own trades do not persist in the next day's pool state; trade size is
  capped at 2% of pool TVL to keep this second-order.
* Conversion costs are measured on today's liquidity; tokens with no route today
  get a 25 bps default.
* Pendle's API omits some delisted markets (for example Elixir's deUSD markets
  are absent), so tail losses from collapsed stablecoins are under-represented.
  See the stress section of `RESULTS.md`.
* Smart-contract risk (Pendle, SY wrappers, underlying protocols) is not priced.
