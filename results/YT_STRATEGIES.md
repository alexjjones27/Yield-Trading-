# How people actually trade Pendle YT, and who makes money

Two sources:

1. **Public material**: Pendle docs and blogs, research write-ups, Dune analyses.
2. **Pendle's own trade and P&L data** for 232 expired Ethereum and Arbitrum
   stablecoin markets:
   * 291,601 router YT trades from 82,791 wallets, rebuilt into 243,395 round
     trips (`pendle_bt/yt_flows.py`).
   * Pendle's per-wallet P&L for the 500 largest YT wallets (57% of YT capital)
     plus 250 random ones, corrected for expired YT (see Method notes).

Code: `pendle_bt/yt_flows.py`, `pendle_bt/yt_strategies.py`.

## Strategies people use with YT

| Strategy | What it bets on | Evidence in the data |
|---|---|---|
| **Long yield**: buy YT, hold, collect the floating yield | Realised yield beats the implied rate paid | Loses on average. Holding to expiry returned −16% on capital. |
| **Points / airdrop farming**: buy YT in points markets | YT earns points on its full notional, giving about 10-30x the points per dollar | Buyers paying more than 15 points over the real yield lost 24% on Pendle-measured P&L. Only 10% of positions were profitable. Airdrops paid outside Pendle are not counted, so this is the one strategy the data can't fully score. |
| **Macro / funding-regime bet**: buy YT before funding or yields spike | A known catalyst (bull market, incentive launch) | 49% of all winning YT profit came from positions opened in Q4 2024, mostly sUSDe YT bought Sep-Nov 2024 before the post-election funding spike. That cohort was about break-even in aggregate (−0.8%): early buyers won big, late buyers gave it back. |
| **Swing trading the implied rate**: buy near fair value, sell within weeks | Implied-rate moves, small mispricings | The only style with consistent winners, and they trade mostly through limit orders (see below). |
| **Selling YT** (mint PT+YT, sell the YT) | Yield comes in below implied | Economically the same as buying PT, which is the profitable side (see `RESULTS.md`). 8.1B of YT notional was sold this way against 14.8B bought. |
| **Limit-order market making / incentives** | Earning the spread plus PENDLE incentives for resting YT orders (Pendle reported limit orders at 71% of swap volume in 2026) | Consistent YT winners route about 53% of YT trades through limit orders, against about 6% for losing multi-market wallets. |
| **Boros hedge** (Pendle's funding-rate market) | Hedge or isolate funding exposure, e.g. long sUSDe YT plus short funding on Boros | Pieces are documented publicly; no evidence of the combined trade in this data. |
| **Mint/redeem arbitrage** | PT + YT ≠ SY | Enforced by the contracts, so it is a bot game; not separable in this data. |

## Results: Pendle-measured P&L, pure-YT positions, expired markets

| Group | Positions | Return on capital | Profitable |
|---|---:|---:|---:|
| All | 2,308 | **−12.1%** | ~20% |
| Bought at or below actual yield (≤ +2pt) | 332 | −6.8% | 31% |
| Bought at +2 to +5pt | 262 | −5.9% | 26% |
| Bought at +5 to +15pt | 1,035 | −9.6% | 22% |
| Points-priced (> +15pt) | 578 | −23.8% | 10% |
| Exited before expiry | 1,301 | −6.5% | 28% |
| Held to expiry | 1,007 | −16.2% | 10% |
| Entered 2023 (early Pendle, small) | 15 | +19.2% | 73% |
| Entered 2024 | 843 | −10.1% | 29% |
| Entered 2025 | 1,345 | −14.1% | 14% |

Of the 145 wallets with five or more pure-YT positions, only 9.7% were
profitable overall, and only 8 were both profitable and won at least half of
their positions. Their profile, compared with everyone else:

* **Bought cheaper:** median premium paid over the actual yield of 3.9 points,
  against 8.6.
* **Exited earlier:** 70% of positions closed before expiry, against 60%.
* **Traded wide and often:** 11-19 markets each, holds of 2-6 weeks, about
  1,100 YT trades per wallet.
* **Used limit orders** for about half of their YT trades.
* **Earned modest returns:** +6% to +14% on capital over 1.5-2.5 years.

## Takeaways

1. There is no evidence of a broadly profitable systematic YT strategy that uses
   the AMM. Buying YT is on average paying for the PT carry the other side earns.
2. Points farming is the dominant YT use. On Pendle-measured P&L it lost 24%,
   so it only works if the off-Pendle airdrop is worth more than that.
3. The repeatable winners look like **limit-order YT traders**: they buy close
   to fair value, scale in and out over weeks, and avoid paying AMM fees and
   impact. That matches the backtest, where AMM costs (5-14% round trip on
   $1-2.5k) were what killed every YT rule.
4. Big YT wins come from **calling a yield regime change** (the Q4 2024 funding
   spike). That is a discretionary macro call, not a rule the data supports.

## Method notes and caveats

* **Corrected P&L.** Pendle's `netGain` books claimed yield and realised sale
  gains but never writes off YT that expires unsold. For expired markets the
  true P&L is `netGain − open YT cost basis`. Example: one wallet showed
  +$4.5M netGain on sUSDe Dec-2024; after the correction it lost $363k ($4.9M
  of YT bought, $4.5M of yield claimed).
* **Pure-YT positions** are wallet-markets with no PT or LP left. Capital is
  Pendle's `maxCapital`.
* **The trade-only reconstruction** in `yt_flows.round_trips` cannot see
  limit-order fills where the wallet is the maker, or exits via PT+YT merges.
  It is used for classifying styles, not for scoring.
* **Not valued:** points and airdrops paid outside Pendle.
* **Small samples:** the limit-order comparison covers 6 consistent winners
  against 8 losers. Treat it as a strong hint, not proof.
