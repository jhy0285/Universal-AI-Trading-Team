from __future__ import annotations

import json
import random
import zlib
from datetime import UTC, date, datetime, time, timedelta

import httpx
import pytest
from sqlalchemy import select

from trading_team.equity_backtest import BuyAndHold, build_market, parse_yahoo_chart, run_backtest
from trading_team.equity_paper import (
    NEW_YORK,
    STRATEGIES,
    SYMBOLS,
    EquityPaperError,
    _evaluation,
    digest_of,
    is_timely,
    last_complete_day,
    run_once,
    status,
    yahoo_fetcher,
)
from trading_team.storage import EquityPaperDecision, EquityPaperNav, EquityPaperRun

START = date(2024, 1, 1)  # a Monday
BENCH, MOM = "spy_buy_hold", "mom12_top5_def"


def weekdays(n: int) -> list[date]:
    out, day = [], START
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def last_day(n: int) -> date:
    return weekdays(n)[-1]


def after_close(day: date, hour: int = 17) -> datetime:
    return datetime.combine(day, time(hour, 0), NEW_YORK).astimezone(UTC)


def payload(symbol: str, n: int, bump_last: float = 1.0) -> dict:
    """Yahoo-shaped daily bars: a seeded random walk, so n+1 bars start with the same n bars."""
    rng, price = random.Random(zlib.crc32(symbol.encode())), 100.0
    stamps, opens, closes = [], [], []
    for day in weekdays(n):
        opened = price * (1 + rng.gauss(0, 0.003))
        price = opened * (1 + rng.gauss(0.0003, 0.009))
        stamps.append(int(datetime.combine(day, time(14, 30), UTC).timestamp()))
        opens.append(opened)
        closes.append(price)
    closes[-1] *= bump_last
    return {
        "chart": {
            "error": None,
            "result": [
                {
                    "meta": {"symbol": symbol},
                    "timestamp": stamps,
                    "indicators": {
                        "quote": [{"open": opens, "close": closes}],
                        "adjclose": [{"adjclose": closes}],
                    },
                }
            ],
        }
    }


def fake(n: int, bump_spy: float = 1.0):
    def fetch(symbol: str):
        body = payload(symbol, n, bump_spy if symbol == "SPY" else 1.0)
        return body, "digest-" + symbol + json.dumps(body["chart"]["result"][0]["timestamp"][-1])

    return fetch


def decisions(db, key):
    with db.transaction() as s:
        return list(
            s.scalars(
                select(EquityPaperDecision)
                .where(EquityPaperDecision.strategy == key)
                .order_by(EquityPaperDecision.data_through)
            )
        )


def test_the_first_run_writes_the_initial_decision_and_has_no_paper_day_yet(db):
    n = 300
    report = run_once(db, fetch=fake(n), now=after_close(last_day(n)))
    assert report["data_through"] == last_day(n).isoformat() and report["paper_only"]
    found = status(db)["strategies"]
    assert set(found) == set(STRATEGIES)
    bench, mom = found[BENCH], found[MOM]
    assert bench["decisions"] == 1 and bench["late_decisions"] == 0 and bench["paper_days"] == 0
    assert bench["last_decision"]["weights"] == {"SPY": "1.0"}
    assert bench["last_decision"]["kind"] == "initial" and bench["last_decision"]["timely"]
    weights = [float(v) for v in mom["last_decision"]["weights"].values()]
    assert weights and sum(weights) == pytest.approx(1.0)  # a full portfolio, spare slots in SHY
    assert "latest" not in bench and "evaluation" not in bench  # the benchmark is not judged
    assert mom["evaluation"]["state"] == "insufficient_sample"


def test_a_new_day_adds_one_paper_day_that_matches_a_hand_calculation(db):
    n = 300
    run_once(db, fetch=fake(n), now=after_close(last_day(n)))
    report = run_once(db, fetch=fake(n + 1), now=after_close(last_day(n + 1)))
    spy = payload("SPY", n + 1)["chart"]["result"][0]["indicators"]
    opened, closed = spy["quote"][0]["open"][-1], spy["quote"][0]["close"][-1]
    expected = 10000 * (1 - 0.0005) * closed / opened  # buy at the open, hold to the close
    nav = status(db)["strategies"][BENCH]
    assert report["strategies"][BENCH]["new_nav_days"] == 1 and nav["paper_days"] == 1
    assert float(nav["latest"]["equity"]) == pytest.approx(expected, rel=1e-6)
    assert nav["latest"]["day"] == last_day(n + 1).isoformat()
    assert set(nav["latest"]["holdings"]) == {"SPY"}


def test_running_again_with_the_same_data_changes_nothing(db):
    n = 305
    now = after_close(last_day(n))
    run_once(db, fetch=fake(n), now=now)
    run_once(db, fetch=fake(n + 2), now=after_close(last_day(n + 2)))
    again = run_once(db, fetch=fake(n + 2), now=after_close(last_day(n + 2), 18))
    for entry in again["strategies"].values():
        assert entry["new_decisions"] == 0 and entry["new_nav_days"] == 0
        assert entry["revised_days"] == 0
    with db.transaction() as s:
        assert len(list(s.scalars(select(EquityPaperRun)))) == 2


def test_recorded_decisions_are_never_rewritten(db):
    n = 300
    first_time = after_close(last_day(n))
    run_once(db, fetch=fake(n), now=first_time)
    before = [(d.weights, d.recorded_at, d.data_digest) for d in decisions(db, MOM)]
    run_once(db, fetch=fake(n + 3), now=after_close(last_day(n + 3)))
    after = [(d.weights, d.recorded_at, d.data_digest) for d in decisions(db, MOM)]
    assert after[0] == before[0] and after[0][1] == first_time


def test_missing_days_are_caught_up_and_late_decisions_are_flagged(db):
    n = 300
    run_once(db, fetch=fake(n), now=after_close(last_day(n)))
    later = n + 45  # about two months later, so month ends were missed
    report = run_once(db, fetch=fake(later), now=after_close(last_day(later)))
    mom = report["strategies"][MOM]
    assert mom["new_nav_days"] == 45 and mom["new_decisions"] >= 1
    assert mom["late_decisions"] >= 1  # decided after the next session had opened
    rows = decisions(db, MOM)
    assert [d.timely for d in rows[1:] if d.data_through != last_day(later).isoformat()] == [
        False
    ] * (len(rows) - 1 - sum(1 for d in rows if d.data_through == last_day(later).isoformat()))
    # The benchmark has no rule decisions: its paper equity equals the plain backtest engine's.
    market = build_market({s: parse_yahoo_chart(payload(s, later), s) for s in SYMBOLS}, "SPY")
    start = market.days.index(last_day(n))
    engine = run_backtest(market, BuyAndHold(market, "SPY"), cost_bps=5, first=start + 1)
    latest = status(db)["strategies"][BENCH]["latest"]
    assert float(latest["equity"]) == pytest.approx(10000 * engine.equity[-1], rel=1e-6)


def test_a_bar_that_is_not_complete_yet_is_never_used(db):
    n = 300
    today = last_day(n)  # the data already contains today's bar...
    midday = datetime.combine(today, time(11, 0), NEW_YORK).astimezone(UTC)  # ...but it is 11:00
    assert last_complete_day(midday) == today - timedelta(days=1)
    report = run_once(db, fetch=fake(n), now=midday)
    assert report["data_through"] == weekdays(n)[-2].isoformat()
    assert last_complete_day(after_close(today)) == today


def test_too_little_history_records_nothing(db):
    with pytest.raises(EquityPaperError):
        run_once(db, fetch=fake(100), now=after_close(last_day(100)))
    with db.transaction() as s:
        assert list(s.scalars(select(EquityPaperRun))) == []


def test_a_provider_revision_is_counted_but_the_stored_day_is_not_rewritten(db):
    n = 300
    run_once(db, fetch=fake(n), now=after_close(last_day(n)))
    run_once(db, fetch=fake(n + 1), now=after_close(last_day(n + 1)))
    with db.transaction() as s:
        stored = s.get(EquityPaperNav, (BENCH, last_day(n + 1).isoformat())).equity
    revised = run_once(db, fetch=fake(n + 1, bump_spy=1.05), now=after_close(last_day(n + 1), 19))
    assert revised["strategies"][BENCH]["revised_days"] == 1
    with db.transaction() as s:
        assert s.get(EquityPaperNav, (BENCH, last_day(n + 1).isoformat())).equity == stored


def test_timeliness_follows_new_york_time_across_daylight_saving():
    friday = date(2026, 11, 6)  # winter time: the open is 14:30 UTC on Monday
    assert is_timely(friday, datetime(2026, 11, 9, 14, 29, tzinfo=UTC))
    assert not is_timely(friday, datetime(2026, 11, 9, 14, 31, tzinfo=UTC))
    summer = date(2026, 7, 10)  # summer time: the open is 13:30 UTC on Monday
    assert is_timely(summer, datetime(2026, 7, 13, 13, 29, tzinfo=UTC))
    assert not is_timely(summer, datetime(2026, 7, 13, 13, 31, tzinfo=UTC))


def test_the_forward_verdict_waits_for_a_year_and_for_a_real_drawdown():
    days = weekdays(300)
    assert _evaluation(days[:100], [1.0] * 100, [1.0] * 100)["state"] == "insufficient_sample"
    calm = [1.0 + 0.0005 * k for k in range(300)]
    out = _evaluation(days, calm, calm)
    assert out["state"] == "drawdown_not_testable" and out["risk_ok"] is None
    fall = calm[:150] + [calm[150] * (1 - 0.3 * min(1, (k + 1) / 20)) for k in range(150)]
    shallow = calm[:150] + [calm[150] * (1 - 0.1 * min(1, (k + 1) / 20)) for k in range(150)]
    out = _evaluation(days, shallow, fall)
    assert out["state"] == "evaluated" and out["risk_ok"] is True and out["return_ok"] is True


def test_the_digest_depends_on_every_symbol_but_not_on_their_order():
    a = digest_of({"SPY": "1", "QQQ": "2"})
    assert a == digest_of({"QQQ": "2", "SPY": "1"})
    assert a != digest_of({"SPY": "1", "QQQ": "3"})


def response(status_code=200, **kwargs):
    return httpx.Response(
        status_code, request=httpx.Request("GET", "https://example.test"), **kwargs
    )


def test_the_fetcher_identifies_itself_backs_off_on_429_and_never_trusts_a_bad_reply():
    body = payload("SPY", 5)
    sleeps, seen = [], []

    def get(url, **kwargs):
        seen.append(kwargs["headers"]["User-Agent"])
        return response(429) if len(seen) == 1 else response(json=body)

    fetch = yahoo_fetcher(get=get, sleep=sleeps.append, now=lambda: 1_800_000_000.0)
    parsed, digest = fetch("SPY")
    assert parsed == body and len(digest) == 64
    assert sleeps == [5] and seen == ["Universal-AI-Trading-Team/0.1 paper-research"] * 2
    fetch("QQQ")
    assert sleeps[-1] == 1.0  # a pause between symbols
    for bad in (response(500), response(content=b"not json"), response(content=b"x" * 8_000_001)):
        with pytest.raises(EquityPaperError):
            yahoo_fetcher(get=lambda *a, bad=bad, **k: bad, sleep=lambda s: None)("SPY")

    def boom(*a, **k):
        raise httpx.ConnectError("down")

    with pytest.raises(EquityPaperError):
        yahoo_fetcher(get=boom, sleep=lambda s: None)("SPY")
    with pytest.raises(EquityPaperError):
        yahoo_fetcher(get=lambda *a, **k: response(429), sleep=lambda s: None)("SPY")
