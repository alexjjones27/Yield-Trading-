"""Charts for the results in ``results/`` (PNG, light theme)."""
import json
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from pendle_bt.costs import CostModel

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, "results")

# validated categorical palette (fixed order) + text tokens
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
INK, INK2, GRID, SURF = "#0b0b0b", "#52514e", "#e6e5e0", "#fcfcfb"


def _style(ax, title, ylabel=None):
    ax.set_facecolor(SURF)
    ax.figure.set_facecolor(SURF)
    ax.set_title(title, loc="left", color=INK, fontsize=12, fontweight="bold")
    if ylabel:
        ax.set_ylabel(ylabel, color=INK2)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=INK2)


def equity_chart(path):
    eq = pd.read_csv(os.path.join(RES, "oos_equity.csv"), index_col=0, parse_dates=True)
    order = ["PT dislocation", "LP carry", "PT carry (no signal)", "Combined PT/YT/LP",
             "YT dislocation", "Hold sUSDe", "Cash (Aave USDC)"]
    fig, ax = plt.subplots(figsize=(10, 5.2))
    for i, name in enumerate([o for o in order if o in eq]):
        s = eq[name].dropna() * 10_000
        ax.plot(s.index, s.values, color=SERIES[i], linewidth=2, label=name)
        ax.annotate(f"{name}  ${s.iloc[-1]:,.0f}", (s.index[-1], s.iloc[-1]),
                    xytext=(6, 0), textcoords="offset points", va="center",
                    fontsize=8, color=INK2)
    _style(ax, "Out-of-sample equity, $10,000 start (walk-forward, net of all costs)",
           "Liquidation value (USD)")
    ax.legend(frameon=False, fontsize=8, loc="upper left")
    ax.set_xlim(eq.index[0], eq.index[-1] + pd.Timedelta(days=150))
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def study_chart(path):
    t = pd.read_csv(os.path.join(RES, "dislocation_study.csv"))
    fig, ax = plt.subplots(figsize=(9, 4.4))
    x = range(len(t))
    ax.bar(x, t.mean_fwd_spread * 100, color=SERIES[0], width=0.6)
    for i, row in t.iterrows():
        ax.annotate(f"PT wins {row.pt_beats_floating:.0%}\nn={row.market_days:,}",
                    (i, row.mean_fwd_spread * 100), xytext=(0, 4), textcoords="offset points",
                    ha="center", fontsize=8, color=INK2)
    ax.axhline(0, color=INK2, linewidth=0.8)
    ax.set_xticks(list(x))
    ax.set_xticklabels([b.replace("(", "").replace("]", "").replace(", ", " to ")
                        for b in t.bucket], fontsize=8)
    _style(ax, "Implied minus realised-to-maturity yield, by observable trailing spread",
           "Mean forward spread (APY points)")
    ax.set_xlabel("Implied APY minus trailing 14d realised APY at the time (fraction)",
                  color=INK2)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def cost_chart(path):
    s = json.load(open(os.path.join(RES, "summary.json")))["capital_sizes"]
    caps = list(s)
    parts = ["gas", "conversion", "amm_fee", "amm_impact"]
    names = {"gas": "Gas", "conversion": "USDC conversion", "amm_fee": "Pendle swap fee",
             "amm_impact": "AMM price impact"}
    fig, ax = plt.subplots(figsize=(8.5, 4.4))
    bottom = [0.0] * len(caps)
    for i, part in enumerate(parts):
        vals = [s[c]["cost_pct"][part] * 100 for c in caps]
        ax.bar(range(len(caps)), vals, bottom=bottom, color=SERIES[i], width=0.6,
               label=names[part], edgecolor=SURF, linewidth=2)
        bottom = [b + v for b, v in zip(bottom, vals)]
    for i, (c, b) in enumerate(zip(caps, bottom)):
        ax.annotate(f"{b:.2f}%  |  CAGR {s[c]['cagr']:.1%}", (i, b), xytext=(0, 4),
                    textcoords="offset points", ha="center", fontsize=8, color=INK2)
    ax.set_xticks(range(len(caps)))
    ax.set_xticklabels([f"${int(c):,}" for c in caps])
    _style(ax, "Total trading costs over the backtest, by starting capital",
           "Cumulative cost (% of starting capital)")
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def gas_chart(path):
    c = CostModel()
    g = c._gas_daily
    g = g[g.index >= "2024-01-01"] * c.gas_units["BUY_PT"]
    fig, ax = plt.subplots(figsize=(9, 3.6))
    ax.plot(g.index, g.values, color=SERIES[0], linewidth=1.5)
    _style(ax, "Ethereum gas cost of one Pendle buy-PT transaction (556k gas)", "USD")
    ax.set_yscale("log")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def main():
    os.makedirs(os.path.join(RES, "charts"), exist_ok=True)
    equity_chart(os.path.join(RES, "charts", "oos_equity.png"))
    study_chart(os.path.join(RES, "charts", "dislocation_study.png"))
    cost_chart(os.path.join(RES, "charts", "costs_by_capital.png"))
    gas_chart(os.path.join(RES, "charts", "gas_per_trade.png"))


if __name__ == "__main__":
    main()
