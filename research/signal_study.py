"""Signal research: does any of this actually predict forward returns?

Every strategy we have built so far was assembled from indicators we *assumed*
carried signal, then backtested. That is expensive and slow, and it conflates
two questions: "does this signal predict anything" and "is this a good trading
rule". This measures the first question directly, before anyone writes a rule.

Method — standard cross-sectional factor research:

  1. Compute each candidate signal on every pair.
  2. Compute forward returns over several horizons.
  3. Measure the **information coefficient (IC)**: the Spearman rank
     correlation between signal value and forward return, pooled across pairs.
  4. Report the IC's t-statistic and, crucially, its **stability across
     sub-periods**. A signal that works in 2023 and inverts in 2025 is not a
     signal, and aggregate IC hides that — which is exactly how Supertrend
     looked good over 3.3 years while losing money in the last 20 months.

Interpreting IC, for context: in equities a |IC| of 0.02-0.05 is a real,
tradeable factor. Crypto is noisier and less efficient, so anything
persistently above ~0.02 with a consistent sign is worth building on.
Sign consistency across sub-periods matters more than magnitude.

Usage:
    python research/signal_study.py --timeframe 1h --horizons 4 12 24
    python research/signal_study.py --timeframe 1d --horizons 1 3 7 --top 25
"""

from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

from freqtrade.data.history import load_pair_history  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "user_data" / "data" / "kucoin"


# ---------------------------------------------------------------------------
# Signal definitions
# ---------------------------------------------------------------------------
# Each returns a Series aligned to the frame. Sign convention: positive means
# "expect price to rise". Mean-reversion signals are negated so that every
# signal is directly comparable — a positive IC always means "the signal works
# as intended", a negative IC means it works inverted.
def build_signals(df: pd.DataFrame, btc: pd.Series | None = None) -> dict[str, pd.Series]:
    close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]
    ret1 = close.pct_change()

    def z(series: pd.Series, n: int) -> pd.Series:
        mu = series.rolling(n).mean()
        sd = series.rolling(n).std()
        return (series - mu) / sd.replace(0, np.nan)

    def ema(n: int) -> pd.Series:
        return close.ewm(span=n, adjust=False).mean()

    atr = pd.concat([high - low, (high - close.shift()).abs(),
                     (low - close.shift()).abs()], axis=1).max(axis=1)
    atr_n = atr.ewm(alpha=1 / 14, adjust=False).mean()

    signals: dict[str, pd.Series] = {}

    # --- trend / momentum (lagging by construction) ------------------------
    for n in (12, 24, 48, 96):
        signals[f"mom_{n}"] = close.pct_change(n)
    signals["ema_ratio_9_21"] = ema(9) / ema(21) - 1
    signals["ema_ratio_24_96"] = ema(24) / ema(96) - 1
    macd = ema(12) - ema(26)
    signals["macd_hist"] = (macd - macd.ewm(span=9, adjust=False).mean()) / close
    signals["donchian_pos_48"] = (
        (close - low.rolling(48).min()) / (high.rolling(48).max() - low.rolling(48).min())
    ) - 0.5

    # --- mean reversion (negated: oversold should predict a rise) ----------
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta).clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = 100 * gain / (gain + loss).replace(0, np.nan)
    signals["rsi_reversion"] = -(rsi - 50) / 50
    signals["zscore_rev_24"] = -z(close, 24)
    signals["zscore_rev_96"] = -z(close, 96)
    bb_mid = close.rolling(20).mean()
    bb_sd = close.rolling(20).std()
    signals["bb_pctb_rev"] = -((close - bb_mid) / (2 * bb_sd).replace(0, np.nan))

    # --- volatility regime -------------------------------------------------
    signals["atr_pct"] = atr_n / close
    signals["vol_ratio_short_long"] = (
        ret1.rolling(24).std() / ret1.rolling(96).std().replace(0, np.nan)
    )
    signals["bb_width"] = (2 * bb_sd) / bb_mid.replace(0, np.nan)
    # Volatility compression: squeezes precede expansion, which is as close to
    # a leading signal as pure OHLCV offers.
    signals["squeeze"] = -z(signals["bb_width"], 96)

    # --- volume / flow (the least lagging family) --------------------------
    signals["vol_surge"] = vol / vol.rolling(24).mean().replace(0, np.nan) - 1
    obv = (np.sign(ret1.fillna(0)) * vol).cumsum()
    signals["obv_slope"] = z(obv.diff(12), 96)
    typical = (high + low + close) / 3
    mf = typical * vol
    pos_mf = mf.where(typical > typical.shift(), 0).rolling(14).sum()
    neg_mf = mf.where(typical < typical.shift(), 0).rolling(14).sum()
    signals["mfi"] = -(100 * pos_mf / (pos_mf + neg_mf).replace(0, np.nan) - 50) / 50
    # Amihud illiquidity: return per unit volume. High = thin, moves are cheap.
    signals["illiquidity"] = -z((ret1.abs() / vol.replace(0, np.nan)), 96)

    # --- candle structure --------------------------------------------------
    rng = (high - low).replace(0, np.nan)
    signals["body_ratio"] = (close - df["open"]) / rng
    signals["upper_wick"] = -((high - close.combine(df["open"], max)) / rng)
    signals["lower_wick"] = (close.combine(df["open"], min) - low) / rng

    # --- cross-asset lead-lag (genuinely forward-looking-ish) --------------
    # Does BTC's recent move predict this alt's next move? If alts follow BTC
    # with a lag, that lag is exploitable in a way self-referential TA is not.
    if btc is not None:
        btc_aligned = btc.reindex(df.index).ffill()
        btc_ret = btc_aligned.pct_change()
        signals["btc_mom_12"] = btc_aligned.pct_change(12)
        signals["btc_lead_1"] = btc_ret
        # Relative strength: this pair versus BTC over the same window.
        signals["rel_strength_24"] = close.pct_change(24) - btc_aligned.pct_change(24)

    return signals


def information_coefficient(signal: pd.Series, fwd: pd.Series) -> tuple[float, int]:
    """Spearman rank IC between a signal and forward returns."""
    both = pd.concat([signal, fwd], axis=1).replace([np.inf, -np.inf], np.nan).dropna()
    if len(both) < 100:
        return np.nan, 0
    ic = both.iloc[:, 0].corr(both.iloc[:, 1], method="spearman")
    return ic, len(both)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--horizons", type=int, nargs="+", default=[4, 12, 24])
    parser.add_argument("--pairs", type=int, default=25, help="max pairs to pool")
    parser.add_argument("--top", type=int, default=20, help="rows to print")
    parser.add_argument("--periods", type=int, default=3, help="sub-periods for stability")
    args = parser.parse_args()

    files = sorted(DATA_DIR.glob(f"*-{args.timeframe}.feather"))
    if not files:
        sys.exit(f"No {args.timeframe} data in {DATA_DIR}")
    pairs = [f.name.replace(f"-{args.timeframe}.feather", "").replace("_", "/")
             for f in files][: args.pairs]

    # BTC is the market factor for the lead-lag signals.
    btc_df = load_pair_history("BTC/USDT", args.timeframe, DATA_DIR)
    btc_close = btc_df.set_index("date")["close"] if len(btc_df) else None

    print(f"timeframe={args.timeframe}  pairs={len(pairs)}  horizons={args.horizons}")

    rows: list[dict] = []
    for pair in pairs:
        df = load_pair_history(pair, args.timeframe, DATA_DIR)
        if len(df) < 300:
            continue
        df = df.set_index("date")
        sigs = build_signals(df, btc_close)

        # Split into sub-periods so we can see whether a signal is stable or
        # just happened to work in one regime.
        bounds = np.array_split(np.arange(len(df)), args.periods)

        for horizon in args.horizons:
            fwd = df["close"].shift(-horizon) / df["close"] - 1
            for name, sig in sigs.items():
                ic_all, n = information_coefficient(sig, fwd)
                if not np.isfinite(ic_all):
                    continue
                sub = []
                for idx in bounds:
                    s_ic, s_n = information_coefficient(sig.iloc[idx], fwd.iloc[idx])
                    if np.isfinite(s_ic):
                        sub.append(s_ic)
                rows.append({"pair": pair, "horizon": horizon, "signal": name,
                             "ic": ic_all, "n": n, "sub": sub})

    if not rows:
        sys.exit("no results — not enough data")

    data = pd.DataFrame(rows)
    out = []
    for (sig, horizon), grp in data.groupby(["signal", "horizon"]):
        ics = grp["ic"].to_numpy()
        mean_ic = ics.mean()
        # t-stat across pairs: is the mean IC distinguishable from zero?
        tstat = mean_ic / (ics.std(ddof=1) / np.sqrt(len(ics))) if len(ics) > 1 else np.nan
        subs = np.array([s for row in grp["sub"] for s in row])
        # Sign consistency: fraction of sub-period ICs sharing the overall sign.
        consistency = (np.sign(subs) == np.sign(mean_ic)).mean() if len(subs) else np.nan
        out.append({"signal": sig, "h": horizon, "IC": mean_ic,
                    "t": tstat, "consist": consistency, "pairs": len(ics)})

    res = pd.DataFrame(out)
    res["absIC"] = res["IC"].abs()
    res = res.sort_values("absIC", ascending=False)

    print(f"\n{'signal':22s} {'h':>3s} {'IC':>8s} {'t':>7s} {'consist':>8s}  verdict")
    print("-" * 72)
    for _, r in res.head(args.top).iterrows():
        # A signal is only interesting if it is large enough, statistically
        # distinguishable from zero, AND keeps its sign across sub-periods.
        if abs(r["IC"]) > 0.02 and abs(r["t"]) > 2 and r["consist"] > 0.75:
            verdict = "*** worth building on"
        elif abs(r["IC"]) > 0.015 and abs(r["t"]) > 2:
            verdict = "*  weak but real"
        elif r["consist"] < 0.6:
            verdict = "   unstable — sign flips"
        else:
            verdict = "   noise"
        print(f"{r['signal']:22s} {int(r['h']):>3d} {r['IC']:>+8.4f} {r['t']:>+7.2f} "
              f"{r['consist']:>7.0%}  {verdict}")

    print("\nIC = Spearman rank correlation of signal with forward return.")
    print("consist = share of sub-period ICs keeping the overall sign.")
    print("Sign consistency matters more than magnitude: a signal that inverts")
    print("between regimes is not tradeable no matter how large its average IC.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
