"""Backtest of a multi-timeframe supply-and-demand day trade on 5-minute bars.

The setup, as described by the person:

    4-hour bias, a 30-minute move against it, wait for price to reach a demand
    zone (or supply in a downtrend), a 5-minute structure shift, absorption or
    exhaustion on the tape, target VWAP.

Every judgement call is pinned to a rule so it can be tested:

- 4-hour bias: the last completed 4-hour close is above its 20-bar EMA and the EMA
  is higher than three bars earlier (mirror for a down bias). 4-hour bars are
  9:30-13:30 and 13:30-16:00 New York.
- 30-minute pullback: the last completed 30-minute close is at least one 30-minute
  ATR(14) below the highest high of the last 16 completed 30-minute bars.
- Demand zone: a 30-minute bar followed by a strong bullish bar (body > 1.5 ATR),
  from that base bar's low to the top of its body. It must be under 3 days old,
  untouched, and not closed through.
- Zone hit: a 5-minute low reaches the zone top. A 5-minute close more than half a
  30-minute ATR below the zone low cancels it.
- Absorption or exhaustion (the tape, as far as bars can show it): within the setup,
  a 5-minute bar with at least 1.5x the volume of the previous 20 bars that closes
  in the upper half of its range. Real tape reading needs order-book data; this is
  only a proxy.
- 5-minute structure shift: a close above the highest high of the 6 bars before the
  lowest low since the zone was hit, within 12 bars (one hour) of the hit.
- Entry at the next bar's open; stop 0.1 5-minute ATR under the lowest low; target
  the session VWAP at the signal. Exit at the stop, the target, or 15:55. If a bar
  reaches both, the stop is assumed first. No new entries after 15:30.
- Costs: 2 basis points a side.

Short trades mirror every rule. Three variants are reported: the full setup, the
setup without the volume proxy, and the full setup with a 2R target instead of VWAP.

Run it with the Trading God Python, which has pandas and yfinance:

    %USERPROFILE%\\.trading-god\\venvs\\vibe\\Scripts\\python.exe trading-god\\research\\supply_demand_backtest.py

Yahoo serves 5-minute bars for the last 60 days only, so this is a small sample.
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass, field
from datetime import time

import pandas as pd

SYMBOLS = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD"]
COST = 0.0002  # per side
LAST_ENTRY = time(15, 30)
FORCED_EXIT = time(15, 55)


@dataclass(frozen=True)
class Variant:
    name: str
    need_volume: bool = True
    target_r: float | None = None  # None: VWAP


VARIANTS = [
    Variant("full setup, VWAP target"),
    Variant("without the volume proxy", need_volume=False),
    Variant("full setup, 2R target", target_r=2.0),
]


@dataclass
class Trade:
    symbol: str
    side: str
    entry_time: pd.Timestamp
    entry: float
    stop: float
    target: float
    exit_time: pd.Timestamp | None = None
    exit: float | None = None
    reason: str = ""

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def planned_r(self) -> float:
        return abs(self.target - self.entry) / self.risk if self.risk else 0.0

    @property
    def r(self) -> float:
        sign = 1 if self.side == "long" else -1
        gross = sign * ((self.exit or self.entry) - self.entry)
        cost = COST * (self.entry + (self.exit or self.entry))
        return (gross - cost) / self.risk if self.risk else 0.0

    @property
    def pct(self) -> float:
        sign = 1 if self.side == "long" else -1
        return 100.0 * (sign * ((self.exit or self.entry) - self.entry) / self.entry - 2 * COST)


# -- bars ---------------------------------------------------------------------------------


def regular_session(bars: pd.DataFrame) -> pd.DataFrame:
    """5-minute bars from 9:30 to 15:55 New York, with a session VWAP and a 5-minute ATR."""
    bars = bars.copy()
    if bars.index.tz is None:
        bars.index = bars.index.tz_localize("America/New_York")
    bars.index = bars.index.tz_convert("America/New_York")
    clock = bars.index.time
    bars = bars[(clock >= time(9, 30)) & (clock < time(16, 0))]
    bars = bars[["Open", "High", "Low", "Close", "Volume"]].dropna()
    day = bars.index.date
    typical = (bars["High"] + bars["Low"] + bars["Close"]) / 3
    volume = bars["Volume"].clip(lower=1)
    bars["vwap"] = (typical * volume).groupby(day).cumsum() / volume.groupby(day).cumsum()
    bars["atr"] = true_range(bars).rolling(14, min_periods=5).mean()
    bars["vol_avg"] = bars["Volume"].shift(1).rolling(20, min_periods=10).mean()
    return bars


def true_range(bars: pd.DataFrame) -> pd.Series:
    previous = bars["Close"].shift(1)
    return pd.concat(
        [bars["High"] - bars["Low"], (bars["High"] - previous).abs(), (bars["Low"] - previous).abs()], axis=1
    ).max(axis=1)


def higher_bars(bars: pd.DataFrame, minutes: list[int]) -> pd.DataFrame:
    """Aggregate session bars into buckets that start at the given minutes after 9:30.

    Each row is stamped with the time it closes, so a 5-minute bar may only use rows
    whose ``closes_at`` is at or before its own close.
    """
    start = bars.index.normalize() + pd.Timedelta(hours=9, minutes=30)
    offset = ((bars.index - start).total_seconds() // 60).astype(int)
    bucket = pd.Series(0, index=bars.index)
    for number, edge in enumerate(minutes):
        bucket[offset >= edge] = number
    keys = [bars.index.date, bucket.values]
    grouped = bars.groupby(keys, sort=True)
    out = pd.DataFrame(
        {
            "Open": grouped["Open"].first(),
            "High": grouped["High"].max(),
            "Low": grouped["Low"].min(),
            "Close": grouped["Close"].last(),
            "closes_at": grouped.apply(lambda g: g.index[-1] + pd.Timedelta(minutes=5)),
        }
    )
    out = out.sort_values("closes_at").reset_index(drop=True)
    return out


def thirty_minute(bars: pd.DataFrame) -> pd.DataFrame:
    out = higher_bars(bars, list(range(0, 390, 30)))
    out["atr"] = true_range(out).rolling(14, min_periods=5).mean()
    out["recent_high"] = out["High"].rolling(16, min_periods=4).max()
    return out


def four_hour(bars: pd.DataFrame) -> pd.DataFrame:
    out = higher_bars(bars, [0, 240])
    out["ema"] = out["Close"].ewm(span=20, adjust=False).mean()
    out["bias_up"] = (out["Close"] > out["ema"]) & (out["ema"] > out["ema"].shift(3))
    return out


def mirror(bars: pd.DataFrame) -> pd.DataFrame:
    """Turn a short setup into a long one: negate prices and swap highs and lows."""
    out = bars.copy()
    out["Open"], out["Close"], out["vwap"] = -bars["Open"], -bars["Close"], -bars["vwap"]
    out["High"], out["Low"] = -bars["Low"], -bars["High"]
    return out


# -- the setup ----------------------------------------------------------------------------


@dataclass
class Zone:
    low: float
    top: float
    formed_at: pd.Timestamp
    expires_at: pd.Timestamp


@dataclass
class Setup:
    zone: Zone
    hit_index: int
    lowest: float
    lowest_index: int
    volume_seen: bool = False


@dataclass
class Book:
    trades: list[Trade] = field(default_factory=list)
    skipped_no_room: int = 0


def demand_zones(thirty: pd.DataFrame, upto: int) -> list[Zone]:
    """Bases before strong bullish 30-minute bars, among the completed bars before ``upto``."""
    zones = []
    for i in range(max(1, upto - 40), upto):
        atr = thirty["atr"].iat[i]
        if not (math.isfinite(atr) and atr > 0):
            continue
        if thirty["Close"].iat[i] - thirty["Open"].iat[i] > 1.5 * atr:
            closes_at = thirty["closes_at"].iat[i]
            zones.append(
                Zone(
                    low=float(thirty["Low"].iat[i - 1]),
                    top=float(max(thirty["Open"].iat[i - 1], thirty["Close"].iat[i - 1])),
                    formed_at=closes_at,
                    expires_at=closes_at + pd.Timedelta(days=3),
                )
            )
    return zones


def run_long(symbol: str, side: str, bars: pd.DataFrame, variant: Variant, book: Book) -> None:
    """Walk the 5-minute bars once, using only higher-timeframe bars that have closed."""
    thirty, four = thirty_minute(bars), four_hour(bars)
    o, h, lo, c = (bars[col].to_numpy(float) for col in ("Open", "High", "Low", "Close"))
    vol, vol_avg = bars["Volume"].to_numpy(float), bars["vol_avg"].to_numpy(float)
    vwap, atr5 = bars["vwap"].to_numpy(float), bars["atr"].to_numpy(float)
    stamps = bars.index
    clock = [ts.time() for ts in stamps]
    day = [ts.date() for ts in stamps]
    closes = stamps + pd.Timedelta(minutes=5)
    t30_close, t4_close = list(thirty["closes_at"]), list(four["closes_at"])
    used: set[pd.Timestamp] = set()
    zones: list[Zone] = []
    setup: Setup | None = None
    trade: Trade | None = None
    t30 = t4 = -1
    n = len(bars)
    for k in range(n):
        now = closes[k]
        moved = False
        while t30 + 1 < len(thirty) and t30_close[t30 + 1] <= now:
            t30 += 1
            moved = True
        while t4 + 1 < len(four) and t4_close[t4 + 1] <= now:
            t4 += 1
        if moved:
            zones = demand_zones(thirty, t30 + 1)

        if trade is not None:
            if lo[k] <= trade.stop:
                trade.exit, trade.reason = min(trade.stop, o[k]), "stop"
            elif h[k] >= trade.target:
                trade.exit, trade.reason = max(trade.target, o[k]), "target"
            elif clock[k] >= FORCED_EXIT or k + 1 == n or day[k + 1] != day[k]:
                trade.exit, trade.reason = c[k], "close"
            if trade.exit is not None:
                trade.exit_time = stamps[k]
                book.trades.append(_unmirror(trade, side))
                trade = None
            continue

        if t30 < 20 or t4 < 4 or clock[k] >= LAST_ENTRY or not bool(four["bias_up"].iat[t4]):
            setup = None
            continue
        atr30 = float(thirty["atr"].iat[t30])

        if setup is None:
            if thirty["Close"].iat[t30] > thirty["recent_high"].iat[t30] - atr30:
                continue  # no 30-minute pullback against the 4-hour bias
            for zone in reversed(zones):
                if zone.formed_at in used or now > zone.expires_at:
                    continue
                if lo[k] <= zone.top and c[k] >= zone.low - 0.5 * atr30:
                    used.add(zone.formed_at)
                    setup = Setup(zone, k, float(lo[k]), k)
                    break
            if setup is None:
                continue

        if c[k] < setup.zone.low - 0.5 * atr30 or k - setup.hit_index > 12:
            setup = None
            continue
        if lo[k] < setup.lowest:
            setup.lowest, setup.lowest_index = float(lo[k]), k
        if vol_avg[k] > 0 and vol[k] >= 1.5 * vol_avg[k] and c[k] >= (h[k] + lo[k]) / 2:
            setup.volume_seen = True
        if setup.lowest_index == 0 or k <= setup.lowest_index:
            continue
        swing = h[max(0, setup.lowest_index - 6) : setup.lowest_index].max()
        if c[k] <= swing or (variant.need_volume and not setup.volume_seen):
            continue
        if k + 1 >= n or day[k + 1] != day[k]:
            setup = None
            continue
        entry = float(o[k + 1])
        stop = setup.lowest - 0.1 * (float(atr5[k]) if math.isfinite(atr5[k]) else 0.0)
        risk = entry - stop
        target = entry + variant.target_r * risk if variant.target_r else float(vwap[k])
        setup = None
        if risk <= 0 or target <= entry:
            book.skipped_no_room += 1
            continue
        trade = Trade(symbol, side, stamps[k + 1], entry, stop, target)


def _unmirror(trade: Trade, side: str) -> Trade:
    if side == "long":
        return trade
    return Trade(
        trade.symbol, "short", trade.entry_time, -trade.entry, -trade.stop, -trade.target,
        trade.exit_time, -trade.exit if trade.exit is not None else None, trade.reason,
    )  # fmt: skip


def backtest(data: dict[str, pd.DataFrame], variant: Variant) -> Book:
    book = Book()
    for symbol, raw in data.items():
        bars = regular_session(raw)
        if len(bars) < 300:
            continue
        run_long(symbol, "long", bars, variant, book)
        run_long(symbol, "short", mirror(bars), variant, book)
    return book


# -- report -------------------------------------------------------------------------------


def summary(book: Book) -> dict[str, float]:
    rs = [t.r for t in book.trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    equity = pd.Series(rs).cumsum() if rs else pd.Series([0.0])
    drawdown = float((equity.cummax().clip(lower=0) - equity).max())
    return {
        "trades": len(rs),
        "win_rate": 100.0 * len(wins) / len(rs) if rs else 0.0,
        "avg_win_r": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss_r": sum(losses) / len(losses) if losses else 0.0,
        "expectancy_r": sum(rs) / len(rs) if rs else 0.0,
        "total_r": sum(rs),
        "profit_factor": sum(wins) / -sum(losses) if losses and sum(losses) < 0 else math.inf,
        "max_drawdown_r": drawdown,
        "avg_planned_r": sum(t.planned_r for t in book.trades) / len(rs) if rs else 0.0,
        "skipped_no_room": book.skipped_no_room,
    }


def report(results: dict[str, Book]) -> str:
    lines = []
    for name, book in results.items():
        s = summary(book)
        lines.append(f"== {name}")
        lines.append(
            f"   trades {s['trades']}, win rate {s['win_rate']:.0f}%, avg win {s['avg_win_r']:.2f}R, "
            f"avg loss {s['avg_loss_r']:.2f}R, expectancy {s['expectancy_r']:+.3f}R per trade after costs"
        )
        lines.append(
            f"   total {s['total_r']:+.1f}R, profit factor {s['profit_factor']:.2f}, max drawdown "
            f"{s['max_drawdown_r']:.1f}R, average planned reward {s['avg_planned_r']:.2f}R, "
            f"skipped because the target was behind the entry: {s['skipped_no_room']}"
        )
        by_symbol = pd.DataFrame([{"symbol": t.symbol, "side": t.side, "r": t.r} for t in book.trades])
        if not by_symbol.empty:
            table = by_symbol.groupby(["symbol"])["r"].agg(["count", "sum", "mean"]).round(2)
            lines.append("   by symbol (count, total R, mean R):")
            lines += ["     " + row for row in table.to_string().splitlines()[1:]]
            sides = by_symbol.groupby("side")["r"].agg(["count", "mean"]).round(3)
            lines.append(
                "   by side (count, mean R): "
                + "; ".join(f"{i} {r['count']:.0f}, {r['mean']:+.3f}" for i, r in sides.iterrows())
            )
    return "\n".join(lines)


def download(symbols: list[str]) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    data = {}
    for symbol in symbols:
        frame = yf.Ticker(symbol).history(period="60d", interval="5m", prepost=False, auto_adjust=False)
        if not frame.empty:
            data[symbol] = frame
        else:
            print(f"no 5-minute data for {symbol}")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("symbols", nargs="*", default=SYMBOLS)
    parser.add_argument("--trades-csv", help="also write every trade of the full setup to this CSV file")
    args = parser.parse_args()
    data = download(args.symbols)
    days = sorted({d for frame in data.values() for d in regular_session(frame).index.date})
    print(f"{len(data)} symbols, {len(days)} trading days ({days[0]} to {days[-1]})" if days else "no data")
    results = {variant.name: backtest(data, variant) for variant in VARIANTS}
    print(report(results))
    if args.trades_csv:
        rows = [t.__dict__ | {"r": t.r, "pct": t.pct} for t in results[VARIANTS[0].name].trades]
        pd.DataFrame(rows).to_csv(args.trades_csv, index=False)
        print(f"trades written to {args.trades_csv}")


if __name__ == "__main__":
    main()
