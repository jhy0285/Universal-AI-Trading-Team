"""Deterministic, offline backtests of rule-based strategies on daily US ETF/stock bars.

Research tool only. It reads historical bars, never places an order and never touches the
ledger. Signals use data up to the close of day t and trade at the open of day t+1, so a
signal cannot see the price it trades at. Prices are dividend- and split-adjusted. Unknown
values are never invented: a bar with a missing field is dropped and counted, and a rebalance
that needs a missing price is postponed and counted.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol

TRADING_DAYS = 252
# Broad index and sector ETFs, fixed in advance. Not chosen by their performance.
DEFAULT_UNIVERSE = (
    "SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "GLD",
    "XLE", "XLF", "XLK", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB",
)  # fmt: skip
_EPS = 1e-12


class BacktestDataError(ValueError):
    """The bars or the weights are unusable. Nothing is repaired or invented."""


@dataclass(frozen=True, slots=True)
class Bar:
    day: date
    open: float  # adjusted like the close, so overnight and intraday moves chain exactly
    close: float  # adjusted close


@dataclass(frozen=True, slots=True)
class Series:
    symbol: str
    bars: tuple[Bar, ...]
    dropped: int  # bars left out because a price was missing or not positive


def _positive(value: object) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def parse_yahoo_chart(payload: object, symbol: str) -> Series:
    """Daily bars from a Yahoo chart response (an unofficial endpoint, no stability promise)."""
    try:
        chart = payload["chart"]  # type: ignore[index]
        if chart.get("error"):
            raise BacktestDataError(f"{symbol}: the provider returned an error")
        result = chart["result"][0]
        meta, stamps = result["meta"], result["timestamp"]
        quote = result["indicators"]["quote"][0]
        adjusted = result["indicators"]["adjclose"][0]["adjclose"]
        opens, closes = quote["open"], quote["close"]
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise BacktestDataError(f"{symbol}: unexpected response shape") from exc
    if str(meta.get("symbol", "")).upper() != symbol.upper():
        raise BacktestDataError(f"{symbol}: the response is for {meta.get('symbol')!r}")
    if not (len(stamps) == len(opens) == len(closes) == len(adjusted)):
        raise BacktestDataError(f"{symbol}: price arrays differ in length")
    bars: list[Bar] = []
    dropped = 0
    previous: date | None = None
    for stamp, raw_open, raw_close, adj_close in zip(stamps, opens, closes, adjusted, strict=True):
        day = datetime.fromtimestamp(stamp, UTC).date()
        if previous is not None and day <= previous:
            raise BacktestDataError(f"{symbol}: timestamps are not strictly increasing")
        previous = day
        if not all(_positive(v) for v in (raw_open, raw_close, adj_close)):
            dropped += 1
            continue
        bars.append(Bar(day, raw_open * adj_close / raw_close, float(adj_close)))
    if not bars:
        raise BacktestDataError(f"{symbol}: no usable bars")
    return Series(symbol.upper(), tuple(bars), dropped)


@dataclass(frozen=True, slots=True)
class Market:
    days: tuple[date, ...]  # the reference symbol's trading days
    open: Mapping[str, tuple[float | None, ...]]
    close: Mapping[str, tuple[float | None, ...]]
    gaps: Mapping[str, int]  # days after a symbol's first bar on which it has no bar
    extra: Mapping[str, int]  # the symbol's bars on days the reference did not trade


def build_market(series: Mapping[str, Series], reference: str = "SPY") -> Market:
    if reference not in series:
        raise BacktestDataError(f"reference symbol {reference} is missing")
    days = tuple(bar.day for bar in series[reference].bars)
    known = set(days)
    opens: dict[str, tuple[float | None, ...]] = {}
    closes: dict[str, tuple[float | None, ...]] = {}
    gaps: dict[str, int] = {}
    extra: dict[str, int] = {}
    for symbol, one in series.items():
        by_day = {bar.day: bar for bar in one.bars}
        opens[symbol] = tuple(by_day[d].open if d in by_day else None for d in days)
        closes[symbol] = tuple(by_day[d].close if d in by_day else None for d in days)
        first = next((i for i, v in enumerate(closes[symbol]) if v is not None), len(days))
        gaps[symbol] = sum(1 for v in closes[symbol][first:] if v is None)
        extra[symbol] = sum(1 for d in by_day if d not in known)
    return Market(days, opens, closes, gaps, extra)


def sma_series(values: Sequence[float | None], window: int) -> list[float | None]:
    """Simple moving average ending at each index; None until a full window of real values."""
    if window < 1:
        raise ValueError("window must be at least 1")
    out: list[float | None] = [None] * len(values)
    total, missing = 0.0, 0
    for i, value in enumerate(values):
        if value is None:
            missing += 1
        else:
            total += value
        if i >= window:
            old = values[i - window]
            if old is None:
                missing -= 1
            else:
                total -= old
        if i >= window - 1 and missing == 0:
            out[i] = total / window
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 50.0 if avg_gain == 0 else 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def rsi_series(values: Sequence[float | None], period: int) -> list[float | None]:
    """Wilder RSI. Leading missing values are allowed, a gap inside the series is not."""
    first = next((i for i, v in enumerate(values) if v is not None), len(values))
    dense = values[first:]
    if any(v is None for v in dense):
        raise BacktestDataError("RSI needs a series without gaps")
    prices = [float(v) for v in dense if v is not None]
    out: list[float | None] = [None] * len(values)
    if len(prices) <= period:
        return out
    gain = loss = 0.0
    for k in range(1, period + 1):
        change = prices[k] - prices[k - 1]
        gain += max(change, 0.0)
        loss += max(-change, 0.0)
    avg_gain, avg_loss = gain / period, loss / period
    out[first + period] = _rsi_value(avg_gain, avg_loss)
    for k in range(period + 1, len(prices)):
        change = prices[k] - prices[k - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        out[first + k] = _rsi_value(avg_gain, avg_loss)
    return out


def month_end(days: Sequence[date], i: int) -> bool:
    """True on the last trading day of a month. Only the calendar is used, never a price."""
    return i + 1 < len(days) and days[i + 1].month != days[i].month


class Strategy(Protocol):
    name: str

    def weights(self, i: int) -> dict[str, float] | None:
        """Target weights decided at the close of day i from data up to i. None = no decision."""


class BuyAndHold:
    def __init__(self, market: Market, symbol: str = "SPY"):
        self._check(market, symbol)
        self.symbol = symbol
        self.name = f"buy and hold {symbol}"

    @staticmethod
    def _check(market: Market, symbol: str) -> None:
        if symbol not in market.close:
            raise BacktestDataError(f"{symbol} is not in the market")

    def weights(self, i: int) -> dict[str, float] | None:
        return {self.symbol: 1.0}


class TrendFilter:
    """Hold the symbol while its close is above its simple moving average, else cash."""

    def __init__(self, market: Market, symbol: str = "SPY", window: int = 200):
        BuyAndHold._check(market, symbol)
        self.symbol = symbol
        self.close = market.close[symbol]
        self.average = sma_series(self.close, window)
        self.name = f"trend filter {symbol} > SMA{window}"

    def weights(self, i: int) -> dict[str, float] | None:
        close, average = self.close[i], self.average[i]
        if close is None or average is None:
            return {}  # no signal means no position
        return {self.symbol: 1.0} if close > average else {}


class DualMomentum:
    """Monthly: hold the top-N symbols by 12-1 month return, only those with a positive one."""

    def __init__(
        self,
        market: Market,
        universe: Sequence[str] = DEFAULT_UNIVERSE,
        top_n: int = 3,
        lookback: int = TRADING_DAYS,
        skip: int = 21,
    ):
        if top_n < 1 or not 0 <= skip < lookback:
            raise ValueError("need top_n >= 1 and 0 <= skip < lookback")
        for symbol in universe:
            BuyAndHold._check(market, symbol)
        self.market, self.universe = market, tuple(universe)
        self.top_n, self.lookback, self.skip = top_n, lookback, skip
        self.name = f"momentum top{top_n} of {len(self.universe)} (12-1m, monthly)"

    def weights(self, i: int) -> dict[str, float] | None:
        if not month_end(self.market.days, i):
            return None
        scored: list[tuple[float, str]] = []
        for symbol in self.universe:
            close = self.market.close[symbol]
            recent, base = i - self.skip, i - self.lookback
            if base < 0 or close[i] is None or close[recent] is None or close[base] is None:
                continue
            momentum = close[recent] / close[base] - 1.0
            if momentum > 0:
                scored.append((-momentum, symbol))
        scored.sort()
        return {symbol: 1.0 / self.top_n for _, symbol in scored[: self.top_n]}


class RSI2Reversion:
    """Connors-style: buy a dip (RSI(2) < entry) inside an uptrend, sell once above SMA(5)."""

    def __init__(
        self,
        market: Market,
        symbol: str = "SPY",
        trend: int = 200,
        entry: float = 10.0,
        exit_window: int = 5,
        period: int = 2,
    ):
        BuyAndHold._check(market, symbol)
        self.symbol = symbol
        close = market.close[symbol]
        rsi = rsi_series(close, period)
        trend_line = sma_series(close, trend)
        exit_line = sma_series(close, exit_window)
        holding, held = False, []
        for i, price in enumerate(close):
            if price is not None:
                if holding:
                    holding = not (exit_line[i] is not None and price > exit_line[i])
                else:
                    rsi_now, trend_now = rsi[i], trend_line[i]
                    holding = bool(
                        rsi_now is not None
                        and trend_now is not None
                        and rsi_now < entry
                        and price > trend_now
                    )
            held.append(holding)
        self._held = held
        self.name = f"RSI({period}) < {entry:g} above SMA{trend}, exit > SMA{exit_window}"

    def weights(self, i: int) -> dict[str, float] | None:
        return {self.symbol: 1.0} if self._held[i] else {}


@dataclass(frozen=True, slots=True)
class Result:
    name: str
    days: tuple[date, ...]
    equity: tuple[float, ...]  # equity at each close, starting from 1.0 at the first open
    orders: int  # symbols traded across all rebalances
    traded: float  # sum of traded notional, in units of the starting equity
    cost: float  # total cost paid, in units of the starting equity
    invested_days: int
    carried: int  # marks left unchanged because a price was missing
    postponed: int  # rebalances delayed a day because a needed open price was missing


def _validated(weights: Mapping[str, float], market: Market) -> dict[str, float]:
    total = 0.0
    for symbol, weight in weights.items():
        if symbol not in market.open:
            raise BacktestDataError(f"weight for unknown symbol {symbol}")
        if not math.isfinite(weight) or weight < 0 or weight > 1 + _EPS:
            raise BacktestDataError(f"weight {weight} for {symbol} is outside [0, 1]")
        total += weight
    if total > 1 + 1e-9:
        raise BacktestDataError(f"weights sum to {total}: no leverage is allowed")
    return dict(weights)


def run_backtest(
    market: Market, strategy: Strategy, *, cost_bps: float, first: int = TRADING_DAYS
) -> Result:
    """Run from day index `first`. The decision for day `first` is made at the close before it."""
    n = len(market.days)
    if not 1 <= first < n:
        raise ValueError("first must leave at least one earlier day and one day to trade")
    if cost_bps < 0:
        raise ValueError("cost_bps must not be negative")
    rate = cost_bps / 10_000.0
    values: dict[str, float] = {}
    cash = 1.0
    last_close: dict[str, float] = {}
    first_weights = strategy.weights(first - 1)
    pending = None if first_weights is None else _validated(first_weights, market)
    equity: list[float] = []
    orders = invested = carried = postponed = 0
    traded = cost_paid = 0.0
    for i in range(first, n):
        for symbol in values:  # overnight move to today's open
            today, before = market.open[symbol][i], last_close.get(symbol)
            if today is None or before is None:
                carried += 1
            else:
                values[symbol] *= today / before
        if pending is not None:
            involved = set(values) | {s for s, w in pending.items() if w > 0}
            if any(market.open[s][i] is None for s in involved):
                postponed += 1
            else:
                equity_open = cash + sum(values.values())
                symbols = set(values) | set(pending)
                targets = {s: pending.get(s, 0.0) * equity_open for s in symbols}
                moves = {s: abs(targets[s] - values.get(s, 0.0)) for s in symbols}
                gross = sum(moves.values())
                cost = gross * rate
                scale = (equity_open - cost) / equity_open
                orders += sum(1 for m in moves.values() if m > _EPS)
                traded += gross
                cost_paid += cost
                cash = (equity_open - sum(targets.values())) * scale
                values = {s: v * scale for s, v in targets.items() if v > _EPS}
                pending = None
        for symbol in values:  # mark to today's close
            opened, closed = market.open[symbol][i], market.close[symbol][i]
            if opened is None or closed is None:
                carried += 1
            else:
                values[symbol] *= closed / opened
                last_close[symbol] = closed
        equity.append(cash + sum(values.values()))
        invested += 1 if values else 0
        if i + 1 < n:
            decision = strategy.weights(i)
            if decision is not None:
                pending = _validated(decision, market)
    return Result(
        strategy.name,
        market.days[first:],
        tuple(equity),
        orders,
        traded,
        cost_paid,
        invested,
        carried,
        postponed,
    )


@dataclass(frozen=True, slots=True)
class Metrics:
    years: float
    total_return: float
    cagr: float | None
    volatility: float
    sharpe: float | None  # risk-free rate taken as 0 for every strategy
    max_drawdown: float
    longest_underwater_days: int


def compute_metrics(days: Sequence[date], equity: Sequence[float], base: float = 1.0) -> Metrics:
    """Metrics of an equity path that starts from `base` just before days[0]."""
    if len(days) != len(equity) or len(days) < 2:
        raise ValueError("need matching days and equity with at least two points")
    steps = zip(equity, equity[1:], strict=False)  # consecutive pairs: the second list is shorter
    returns = [equity[0] / base - 1.0] + [b / a - 1.0 for a, b in steps]
    years = (days[-1] - days[0]).days / 365.25
    total = equity[-1] / base - 1.0
    cagr = (equity[-1] / base) ** (1.0 / years) - 1.0 if years > 0 and equity[-1] > 0 else None
    sigma = statistics.pstdev(returns) * math.sqrt(TRADING_DAYS)
    sharpe = statistics.fmean(returns) * TRADING_DAYS / sigma if sigma > 0 else None
    peak, worst, since, longest = base, 0.0, 0, 0
    for k, value in enumerate(equity):
        if value >= peak:
            peak, since = value, k
        else:
            worst = min(worst, value / peak - 1.0)
            longest = max(longest, k - since)
    return Metrics(years, total, cagr, sigma, sharpe, worst, longest)


def subperiod(result: Result, start: date, end: date) -> Metrics:
    """Metrics for the days in [start, end], rebased to the close before the first of them."""
    idx = [k for k, d in enumerate(result.days) if start <= d <= end]
    if len(idx) < 2:
        raise ValueError("the sub-period needs at least two trading days")
    a, b = idx[0], idx[-1]
    base = result.equity[a - 1] if a > 0 else 1.0
    return compute_metrics(result.days[a : b + 1], result.equity[a : b + 1], base)


def rolling_beat_rate(
    days: Sequence[date],
    strategy: Sequence[float],
    benchmark: Sequence[float],
    years: int = 3,
    step: int = 21,
) -> tuple[float | None, int]:
    """Share of rolling windows in which the strategy's CAGR beat the benchmark's."""
    window = years * TRADING_DAYS
    wins = total = 0
    for a in range(0, len(days) - window, step):
        b = a + window
        span = (days[b] - days[a]).days / 365.25
        mine = (strategy[b] / strategy[a]) ** (1 / span)
        theirs = (benchmark[b] / benchmark[a]) ** (1 / span)
        total += 1
        wins += 1 if mine > theirs else 0
    return (wins / total if total else None), total
