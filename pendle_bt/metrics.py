"""Performance statistics on a daily NAV series."""
import numpy as np
import pandas as pd


def stats(nav: pd.Series, start=None, end=None):
    s = nav.dropna()
    if start is not None:
        s = s[s.index >= pd.Timestamp(start)]
    if end is not None:
        s = s[s.index <= pd.Timestamp(end)]
    if len(s) < 3:
        return dict(ret=np.nan, cagr=np.nan, vol=np.nan, sharpe=np.nan, maxdd=np.nan, days=len(s))
    days = (s.index[-1] - s.index[0]).days
    ret = s.iloc[-1] / s.iloc[0] - 1
    cagr = (1 + ret) ** (365 / max(days, 1)) - 1
    r = s.pct_change().dropna()
    vol = r.std() * np.sqrt(365)
    sharpe = (r.mean() * 365) / vol if vol > 0 else np.nan
    dd = (s / s.cummax() - 1).min()
    return dict(ret=ret, cagr=cagr, vol=vol, sharpe=sharpe, maxdd=dd, days=days)


def excess_sharpe(nav: pd.Series, cash_apy: pd.Series, start=None, end=None):
    """Sharpe of returns in excess of the Aave USDC rate."""
    s = nav.dropna()
    if start is not None:
        s = s[s.index >= pd.Timestamp(start)]
    if end is not None:
        s = s[s.index <= pd.Timestamp(end)]
    r = s.pct_change().dropna()
    c = (1 + cash_apy.reindex(r.index).ffill().fillna(0)) ** (1 / 365) - 1
    ex = r - c
    sd = ex.std() * np.sqrt(365)
    return (ex.mean() * 365) / sd if sd > 0 else np.nan
