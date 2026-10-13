# Leveraged PT (looping) for small accounts ($100 to $1,000)

**Looping:** buy PT, post it as collateral on a lending market (Morpho, Aave,
Euler), borrow stablecoins, buy more PT, and repeat, usually in one
flash-loan transaction. Equity earns roughly:

> PT fixed APY + (leverage − 1) × (PT fixed APY − borrow APY) − costs

Code: `pendle_bt/looping_data.py`, `pendle_bt/looping.py`. Outputs:
`results/loop_portfolios.csv` and `results/loop_live_opportunities.csv`.

## Data and modelling

| Input | Source |
|---|---|
| Lending venues | 328 Morpho Blue markets that accept a stablecoin Pendle PT as collateral (Ethereum and Arbitrum). Only USDC, USDT, DAI, USDS and PYUSD loans are used. |
| Borrow cost | Each market's actual daily borrow APY and available liquidity (Morpho API) |
| Liquidation price | PT oracle type read on-chain: linear discount (fixed curve to par) or Pendle market TWAP; underlying valued at its USD price, so depegs count. Health factor = collateral × LLTV / debt; below 1 the position is liquidated with Morpho's penalty. |
| Entry / exit | PT bought through the exact Pendle AMM math on the full levered notional, plus USDC↔underlying conversion on the full notional |
| Gas | Measured from 180 real Morpho PT loop/unwind transactions: about 1.23M gas to open, 0.97M to close, at the historical daily gas price |
| Liquidation reality check | All 388 liquidations in those Morpho markets |

Backtest window: Mar 2025 to 13 Oct 2026, the low-gas era with enough
lending venues. Positions still open at the end are marked at what unwinding
through the AMM would return.

## Results: one loop at a time, compounding

The rules: take the opportunity with the best expected return after costs,
require PT fixed APY minus borrow APY ≥ 2 points, at least 60 days to maturity,
expected return ≥ 5%/yr, and hold to maturity.

| Start | No leverage | 3x | 5x | Max (~5.5-7.7x) | Worst drawdown at max |
|---:|---:|---:|---:|---:|---:|
| $1,000 | $1,175 (+10.5%/yr) | $1,258 (+15%/yr) | $1,395 (+23%/yr) | **$1,535 (+30%/yr)** | −7% |
| $500 | $568 (+8%/yr) | $616 (+14%/yr) | $665 (+19%/yr) | **$723 (+26%/yr)** | −7% |
| $250 | $277 (+7%/yr) | $281 (+8%/yr) | $319 (+16%/yr) | **$337 (+20%/yr)** | −13% |
| $100 | $100 (0%) | $91 (−5%/yr) | $45 (liquidated once) | $49 (liquidated once) | −63% |

For comparison over the same window: Aave USDC returned about +6.8% and
holding sUSDe about +10%.

Each run is only 4 to 6 trades, so the specific numbers depend on which trades
came up. The trade-level study (about 11,000 entries, weekly, every loopable
market) shows the same pattern:

| $1,000, ~7x, by spread at entry | Median trade APY | Losing trades |
|---|---:|---:|
| PT fixed − borrow ≤ 2 points | 6.3% (no better than unlevered) | 27% |
| 2 to 4 points | 15.9% | 17% |
| 4 to 8 points | 28.7% | 5% |

## Why $100 does not work and $500 to $1,000 does

* **Gas is fixed per loop.** A loop plus unwind is about 2.2M gas. At 1 gwei and
  $2,500 ETH that is about $5.50, which is 5.5% of $100 per trade. In 2024 it
  was $35 to $70.
* **Conversion and AMM fees scale with leverage.** A 7x loop pays them on 7x
  your capital. Typically 1 to 2% of equity per round trip, which a thin
  spread cannot cover.
* **One liquidation erases years of carry.** The $100 run lost 48% when a 5.5x
  fxSAVE loop on a market-price oracle was liquidated in mid-2025.
* **Arbitrum is cheap but had few loopable stablecoin PTs.** At $100, Arbitrum
  loops returned about +9%/yr median against −5% on Ethereum, but there were
  only 44 such entries.

## Liquidation risk observed on Morpho

| PT oracle type | Liquidated debt per borrowed dollar-year | Events |
|---|---:|---:|
| Linear discount | 0.14% | 120 |
| Market TWAP | 0.71% | 80 |
| Other / unclassified | 25% (one event dominates) | 140 |
| All, excluding the reUSD event | 0.29% | 340 |
| All | 6.2% | 388 |

**reUSD, 25 Aug 2026:** 34 loops were liquidated within 8 minutes, $36M of
debt, in one Morpho market (LLTV 91.5%). Pendle's daily data shows
nothing unusual that day (reUSD at $1.00, implied rate 10.6% → 11.7%), so the
trigger was intraday and oracle-driven. The cause is not verified here. A daily
backtest cannot see moves like this, which is why the empirical liquidation
record is reported separately.

## Live opportunities (13 Oct 2026, Morpho, after all costs)

These assume the borrow rate stays at its 7-day average, the position is held
to maturity, and nothing gets liquidated.

| PT (expiry) | Days | Leverage | Oracle | PT fixed | Borrow (7d) | Expected equity APY | $1,000 → (to maturity) |
|---|---:|---:|---|---:|---:|---:|---:|
| USD3 (17 Dec) | 65 | 5.5x | unclassified | 14.5% | 7.9% | ~48% | ~$1,072 |
| siUSD (7 Jan) | 86 | 5.5x | unclassified | 10.8% | 4.6% | ~42% | ~$1,086 |
| reUSD (10 Dec) | 58 | 7.7x | unclassified | 11.5% | 7.9% | ~38% | ~$1,052 |
| strUSD (26 Nov) | 44 | 5.2x | linear | 10.7% | 7.7% | ~23% | ~$1,025 |
| USDat (14 Jan) | 93 | 7.7x | market | 8.1% | 5.8% | ~23% | ~$1,055 |
| sUSDe / sUSDS (26 Nov) | 44 | 7.5x | mixed | 4.5-4.7% | 3.5-3.8% | ~10% | ~$1,012 |

At max leverage these open at a health factor of 1.05: a 5% fall in the
oracle price of the collateral liquidates you.

## Practical rules if you do this

1. **Use at least $500 on Ethereum**, or use an L2 venue. Below that, gas and fees eat the carry.
2. **Only loop when PT fixed − borrow APY is at least 3 to 4 points** and there
   are 60+ days to maturity, so costs are spread over enough time.
3. **Prefer linear-discount oracles.** They cannot be liquidated by a PT price
   crash; market-price oracles can.
4. **Keep the health factor at 1.15 or higher** (about 4-5x, not 7-8x). Watch
   borrow rates: if borrowing costs more than the PT earns for days on end,
   deleverage.
5. **Size for a total loss of the looped amount.** Issuer default, a depeg or an
   oracle event can liquidate even a well-run loop. Several of the
   best-yielding PTs are young synthetic dollars.
6. **Expect high APYs but small dollars.** +30%/yr on $1,000 is about
   $300/yr.

## Caveats

* Only Morpho venues are in the backtest (Aave and Euler PT markets exist too).
* The loop's own borrowing does not move the market's rate; at $100 to $1,000 that
  is negligible.
* Daily data misses intraday oracle moves (see the reUSD event).
* Points, PENDLE incentives and lender rewards are not counted.
* The parameters (2-point spread, 60 days) were chosen with sight of these
  results; a 4-point threshold did slightly worse, so they are not finely tuned.
