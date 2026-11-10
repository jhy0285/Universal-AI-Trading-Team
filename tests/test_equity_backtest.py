from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from trading_team.equity_backtest import (
    BacktestDataError,
    BuyAndHold,
    DualMomentum,
    Market,
    RSI2Reversion,
    TrendFilter,
    compute_metrics,
    month_end,
    parse_yahoo_chart,
    rolling_beat_rate,
    rsi_series,
    run_backtest,
    sma_series,
    subperiod,
)

STAMP = 1704205800  # 2024-01-02 14:30 UTC, the open of a US trading day


def weekdays(n: int, start: date = date(2019, 1, 2)) -> tuple[date, ...]:
    out, day = [], start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return tuple(out)


def market_of(paths: dict[str, list[tuple[float | None, float | None]]]) -> Market:
    """A Market from (open, close) pairs per symbol; all symbols share one calendar."""
    n = len(next(iter(paths.values())))
    return Market(
        days=weekdays(n),
        open={s: tuple(o for o, _ in p) for s, p in paths.items()},
        close={s: tuple(c for _, c in p) for s, p in paths.items()},
        gaps=dict.fromkeys(paths, 0),
        extra=dict.fromkeys(paths, 0),
    )


def random_walk(n: int, seed: int, start: float = 100.0) -> list[tuple[float, float]]:
    rng, price, out = random.Random(seed), start, []
    for _ in range(n):
        opened = price * (1 + rng.gauss(0, 0.004))
        price = opened * (1 + rng.gauss(0.0003, 0.01))
        out.append((opened, price))
    return out


def payload(symbol="SPY", opens=(100.0, None, 102.0), closes=(101.0, 102.0, 103.0), adj=None):
    adj = adj or (50.5, 51.0, 51.5)
    return {
        "chart": {
            "error": None,
            "result": [
                {
                    "meta": {"symbol": symbol},
                    "timestamp": [STAMP + k * 86400 for k in range(len(closes))],
                    "indicators": {
                        "quote": [{"open": list(opens), "close": list(closes)}],
                        "adjclose": [{"adjclose": list(adj)}],
                    },
                }
            ],
        }
    }


def test_parse_adjusts_the_open_and_drops_incomplete_bars_without_filling_them():
    series = parse_yahoo_chart(payload(), "spy")
    assert series.symbol == "SPY" and series.dropped == 1 and len(series.bars) == 2
    first = series.bars[0]
    assert first.day == date(2024, 1, 2)
    assert first.close == 50.5 and first.open == pytest.approx(100.0 * 50.5 / 101.0)
    assert series.bars[1].day == date(2024, 1, 4)  # the dropped bar leaves a visible gap


@pytest.mark.parametrize(
    "bad",
    [
        {"chart": {"error": {"code": "Not Found"}, "result": None}},
        {"chart": {"result": []}},
        {},
        payload(symbol="QQQ"),
        payload(adj=(50.5, 51.0)),
    ],
)
def test_parse_rejects_unusable_responses(bad):
    with pytest.raises(BacktestDataError):
        parse_yahoo_chart(bad, "SPY")


def test_parse_rejects_timestamps_that_do_not_increase():
    bad = payload()
    bad["chart"]["result"][0]["timestamp"] = [STAMP, STAMP, STAMP + 86400]
    with pytest.raises(BacktestDataError):
        parse_yahoo_chart(bad, "SPY")


def test_parse_rejects_a_response_with_no_usable_bar():
    with pytest.raises(BacktestDataError):
        parse_yahoo_chart(payload(opens=(None, None, None)), "SPY")


def test_sma_needs_a_full_window_of_real_values():
    assert sma_series([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]
    assert sma_series([None, 2, 3, 4], 2) == [None, None, 2.5, 3.5]
    assert sma_series([1, None, 3, 4], 2) == [None, None, None, 3.5]


def test_rsi_extremes_and_a_hand_computed_value():
    assert rsi_series([1, 2, 3, 4, 5], 2)[-1] == 100.0
    assert rsi_series([5, 4, 3, 2, 1], 2)[-1] == 0.0
    assert rsi_series([7, 7, 7, 7], 2)[-1] == 50.0
    assert rsi_series([1, 2, 1, 2], 2) == [None, None, 50.0, pytest.approx(75.0)]
    with pytest.raises(BacktestDataError):
        rsi_series([1, 2, None, 3], 2)


def test_buy_and_hold_accounting_matches_a_hand_calculation():
    opens = [100.0, 100.0, 101.0, 102.0, 103.0, 104.0]
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    market = market_of({"SPY": list(zip(opens, closes, strict=True))})
    free = run_backtest(market, BuyAndHold(market), cost_bps=0, first=1)
    assert free.equity[-1] == pytest.approx(105.0 / 100.0)  # open of day 1 to close of the last
    paid = run_backtest(market, BuyAndHold(market), cost_bps=10, first=1)
    assert paid.equity[-1] == pytest.approx(0.999 * 105.0 / 100.0)
    assert paid.cost == pytest.approx(0.001) and paid.traded == pytest.approx(1.0)
    assert paid.orders == 1 and paid.invested_days == 5 and paid.carried == 0


class Flip:
    name = "flip"

    def weights(self, i):
        return {"SPY": 1.0} if i % 2 == 0 else {}


def test_every_switch_pays_the_cost_on_the_traded_notional():
    market = market_of({"SPY": [(100.0, 100.0)] * 12})
    result = run_backtest(market, Flip(), cost_bps=10, first=1)
    trades = len(market.days) - 1  # a decision at every close but the last, each executed
    assert result.orders == trades
    assert result.equity[-1] == pytest.approx(0.999**trades)
    assert result.cost == pytest.approx(1 - 0.999**trades)


def test_a_missing_open_postpones_the_buy_and_a_missing_close_carries_the_value():
    paths = [(100.0, 100.0), (None, 101.0), (102.0, 102.0), (103.0, None), (104.0, 105.0)]
    market = market_of({"SPY": paths})
    result = run_backtest(market, BuyAndHold(market), cost_bps=0, first=1)
    assert result.postponed == 1  # no open on day 1, so the buy waits for day 2
    assert result.equity[0] == 1.0  # still all cash after day 1
    assert result.carried >= 1  # day 3 has no close: the value is carried, not guessed
    assert result.invested_days == 3


@pytest.mark.parametrize(
    "weights", [{"SPY": 0.7, "QQQ": 0.5}, {"SPY": -0.1}, {"XYZ": 0.5}, {"SPY": float("nan")}]
)
def test_leverage_shorts_and_unknown_symbols_are_refused(weights):
    market = market_of({"SPY": random_walk(30, 1), "QQQ": random_walk(30, 2)})

    class Bad:
        name = "bad"

        def weights(self, i):
            return weights

    with pytest.raises(BacktestDataError):
        run_backtest(market, Bad(), cost_bps=0, first=2)


def make_strategies(market):
    return [
        BuyAndHold(market),
        TrendFilter(market, "SPY", 50),
        DualMomentum(market, ("SPY", "QQQ", "TLT"), top_n=2, lookback=120, skip=10),
        RSI2Reversion(market, "SPY", trend=50),
    ]


def test_no_strategy_can_see_a_price_after_the_day_it_decides():
    n = 700
    base = {s: random_walk(n, seed) for s, seed in (("SPY", 1), ("QQQ", 2), ("TLT", 3))}
    for cut in (200, 301, 450, 599):
        # Same history through day `cut`, different prices afterwards.
        other = {
            s: p[: cut + 1] + random_walk(n - cut - 1, 99 + cut, p[cut][1]) for s, p in base.items()
        }
        a, b = market_of(base), market_of(other)
        for sa, sb in zip(make_strategies(a), make_strategies(b), strict=True):
            assert sa.weights(cut) == sb.weights(cut), (sa.name, cut)


def test_the_whole_run_is_deterministic():
    market = market_of({s: random_walk(400, k) for k, s in enumerate(("SPY", "QQQ", "TLT"))})
    one = [run_backtest(market, s, cost_bps=5, first=130).equity for s in make_strategies(market)]
    two = [run_backtest(market, s, cost_bps=5, first=130).equity for s in make_strategies(market)]
    assert one == two


def drifting(n, daily):
    price, out = 100.0, []
    for _ in range(n):
        out.append((price, price * (1 + daily)))
        price *= 1 + daily
    return out


def test_trend_filter_holds_above_the_average_and_sits_in_cash_below_it():
    up = market_of({"SPY": drifting(120, 0.002)})
    down = market_of({"SPY": drifting(120, -0.002)})
    assert TrendFilter(up, "SPY", 50).weights(100) == {"SPY": 1.0}
    assert TrendFilter(down, "SPY", 50).weights(100) == {}
    assert TrendFilter(up, "SPY", 50).weights(10) == {}  # not enough history: no position


def test_momentum_picks_the_top_n_positive_symbols_once_a_month():
    market = market_of(
        {"A": drifting(300, 0.001), "B": drifting(300, 0.0005), "C": drifting(300, -0.001)}
    )
    strategy = DualMomentum(market, ("A", "B", "C"), top_n=2)
    i = next(k for k in range(260, 299) if month_end(market.days, k))
    assert strategy.weights(i) == {"A": 0.5, "B": 0.5}
    assert strategy.weights(i + 1) is None if not month_end(market.days, i + 1) else True
    falling = market_of({"A": drifting(300, -0.001), "B": drifting(300, -0.002)})
    assert DualMomentum(falling, ("A", "B"), top_n=2).weights(i) == {}
    with pytest.raises(ValueError):
        DualMomentum(market, ("A",), top_n=0)


def test_rsi2_buys_a_dip_in_an_uptrend_and_sells_the_rebound_but_never_buys_a_downtrend():
    closes = [100.0 * 1.002**k for k in range(300)]
    for _ in range(3):
        closes.append(closes[-1] * 0.97)
    for _ in range(5):
        closes.append(closes[-1] * 1.04)
    closes += [closes[-1] * 1.002**k for k in range(1, 12)]
    market = market_of({"SPY": [(c, c) for c in closes]})
    held = RSI2Reversion(market, "SPY")._held
    assert not held[299] and any(held[300:303]) and not held[-1]
    falling = market_of({"SPY": drifting(400, -0.002)})
    assert not any(RSI2Reversion(falling, "SPY")._held)


def test_metrics_for_drawdown_underwater_time_and_growth():
    days = weekdays(5)
    m = compute_metrics(days, [1.1, 1.0, 1.2, 0.6, 0.9])
    assert m.max_drawdown == pytest.approx(-0.5) and m.longest_underwater_days == 2
    assert m.total_return == pytest.approx(-0.1)
    yearly = compute_metrics([date(2020, 1, 1), date(2021, 1, 1)], [1.5, 2.0])
    assert yearly.cagr == pytest.approx(1.0, abs=0.01)
    with pytest.raises(ValueError):
        compute_metrics(days[:1], [1.0])


def test_a_sub_period_is_rebased_to_the_close_before_it():
    market = market_of({"SPY": drifting(40, 0.01) + [(p, p) for p in [150.0] * 20]})
    result = run_backtest(market, BuyAndHold(market), cost_bps=0, first=1)
    late = subperiod(result, result.days[45], result.days[-1])
    assert late.total_return == pytest.approx(0.0, abs=1e-9)
    with pytest.raises(ValueError):
        subperiod(result, result.days[0], result.days[0])


def test_rolling_beat_rate_counts_strictly_better_windows():
    days = weekdays(1000)
    fast = [1.0004**k for k in range(1000)]
    slow = [1.0002**k for k in range(1000)]
    assert rolling_beat_rate(days, fast, slow) == (1.0, 12)
    assert rolling_beat_rate(days, slow, slow) == (0.0, 12)
    assert rolling_beat_rate(days[:100], fast[:100], slow[:100]) == (None, 0)
