"""Forward paper run of US ETF strategies: write the decision down first, compute the result later.

PAPER ONLY and research only: it never sends an order and never touches the trade ledger. It keeps a
paper index per strategy in its own tables. Each day it (1) downloads the daily bars, (2) records the
target weights that the fixed rule gives from data through the last completed session, once and for
good, with the time and a digest of the data it used, and (3) derives the paper equity by replaying
the recorded decisions through the same engine as the backtest (decide at a close, trade at the next
open). A decision recorded after the next session opened is flagged `timely = False`.
"""

from __future__ import annotations

import hashlib
import json
import time as clock_module
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select

from trading_team.domain import utcnow
from trading_team.equity_backtest import (
    DEFAULT_UNIVERSE,
    DEFENSIVE_ASSET,
    TRADING_DAYS,
    BacktestDataError,
    BuyAndHold,
    DualMomentum,
    Market,
    RecordedDecisions,
    Series,
    Strategy,
    build_market,
    compute_metrics,
    judge,
    parse_yahoo_chart,
    run_backtest,
)
from trading_team.storage import (
    Database,
    EquityPaperDecision,
    EquityPaperNav,
    EquityPaperRun,
)

NEW_YORK = ZoneInfo("America/New_York")
USER_AGENT = "Universal-AI-Trading-Team/0.1 paper-research"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
MAX_RESPONSE_BYTES = 8_000_000
SYMBOLS = (*DEFAULT_UNIVERSE, DEFENSIVE_ASSET)
COMPLETE_AFTER = time(16, 30)  # a daily bar is final shortly after the 16:00 close
OPENS_AT = time(9, 30)
MIN_BARS = TRADING_DAYS + 30  # the momentum rule needs a year of history

# How the forward run is judged (fixed on 2026-11-10 with the strategy): the same criterion as the
# backtest, but only after a year of paper days, and the drawdown half only if the benchmark itself
# fell far enough for the comparison to mean anything.
FORWARD_MIN_DAYS = TRADING_DAYS
FORWARD_MIN_BENCHMARK_DRAWDOWN = 0.15

BENCHMARK = "spy_buy_hold"
STRATEGIES: dict[str, tuple[str, Callable[[Market], Strategy]]] = {
    "mom12_top5_def": (
        "12-1 month momentum, top 5 of 16 ETFs, positive momentum only, empty slots in SHY"
        " (chosen 2026-11-10 on 2001-2019; narrowly missed the 2020-2026 hold-out)",
        lambda market: DualMomentum(market, top_n=5, defensive=DEFENSIVE_ASSET),
    ),
    BENCHMARK: ("benchmark: buy and hold SPY", lambda market: BuyAndHold(market, "SPY")),
}

Fetcher = Callable[[str], tuple[object, str]]  # symbol -> (parsed response, sha256 of its bytes)


class EquityPaperError(RuntimeError):
    """The data for a run is unusable. Nothing is recorded from unusable data."""


def yahoo_fetcher(
    *,
    get: Callable[..., httpx.Response] = httpx.get,
    history_from: date = date(2000, 1, 1),
    pause: float = 1.0,
    sleep: Callable[[float], None] = clock_module.sleep,
    now: Callable[[], float] = clock_module.time,
) -> Fetcher:
    """Daily bars from Yahoo's chart endpoint (unofficial, no stability promise), paced politely."""
    calls = 0

    def fetch(symbol: str) -> tuple[object, str]:
        nonlocal calls
        if calls:
            sleep(pause)
        calls += 1
        params = {
            "period1": int(datetime.combine(history_from, time(), UTC).timestamp()),
            "period2": int(now()),
            "interval": "1d",
            "events": "div|split",
        }
        for attempt in range(4):
            try:
                reply = get(
                    YAHOO_URL.format(symbol=symbol),
                    params=params,
                    headers={"User-Agent": USER_AGENT},
                    timeout=40,
                )
            except httpx.HTTPError as exc:
                raise EquityPaperError(f"{symbol}: request failed ({type(exc).__name__})") from None
            if reply.status_code == 429:
                sleep(5 * (attempt + 1))
                continue
            if reply.status_code != 200:
                raise EquityPaperError(f"{symbol}: HTTP {reply.status_code}")
            if len(reply.content) > MAX_RESPONSE_BYTES:
                raise EquityPaperError(f"{symbol}: response is too large")
            try:
                payload = reply.json()
            except ValueError:
                raise EquityPaperError(f"{symbol}: response is not JSON") from None
            return payload, hashlib.sha256(reply.content).hexdigest()
        raise EquityPaperError(f"{symbol}: still rate limited after retries")

    return fetch


def last_complete_day(now: datetime) -> date:
    """The latest calendar date whose daily bar can be final: nothing earlier than 16:30 New York."""
    local = now.astimezone(NEW_YORK)
    return local.date() if local.time() >= COMPLETE_AFTER else local.date() - timedelta(days=1)


def next_weekday(day: date) -> date:
    following = day + timedelta(days=1)
    while following.weekday() >= 5:
        following += timedelta(days=1)
    return following


def is_timely(data_through: date, recorded_at: datetime) -> bool:
    """Recorded before the open of the next weekday's session (a holiday only makes this stricter)."""
    opens = datetime.combine(next_weekday(data_through), OPENS_AT, NEW_YORK)
    return recorded_at < opens


def _trim(series: Series, last_day: date) -> Series:
    return Series(series.symbol, tuple(b for b in series.bars if b.day <= last_day), series.dropped)


def digest_of(digests: Mapping[str, str]) -> str:
    text = "\n".join(f"{symbol}:{digests[symbol]}" for symbol in sorted(digests))
    return hashlib.sha256(text.encode()).hexdigest()


def initial_weights(strategy: Strategy, i: int) -> dict[str, float]:
    """The portfolio the rule holds after day i, used once at the start of a run."""
    target = getattr(strategy, "target", None)
    found = target(i) if target is not None else strategy.weights(i)
    return {} if found is None else found


def plan_decisions(
    market: Market, strategy: Strategy, start_day: date, known: set[date]
) -> list[tuple[date, dict[str, float], str]]:
    """Decisions the rule makes from `start_day` on that are not written down yet, in order."""
    if start_day not in market.days:
        raise EquityPaperError(f"the run starts on {start_day}, which is not in the data")
    first = market.days.index(start_day)
    planned = []
    for i in range(first, len(market.days)):
        day = market.days[i]
        if day in known:
            continue
        weights = initial_weights(strategy, i) if i == first else strategy.weights(i)
        if weights is not None:
            planned.append((day, weights, "initial" if i == first else "rule"))
    return planned


def _weights_json(weights: Mapping[str, float]) -> str:
    return json.dumps({s: repr(float(w)) for s, w in sorted(weights.items())})


def _weights_from_json(text: str) -> dict[str, float]:
    return {s: float(w) for s, w in json.loads(text).items()}


def _usd(value: float) -> Decimal:
    return Decimal(f"{value:.6f}")


def run_once(
    db: Database,
    *,
    fetch: Fetcher,
    now: datetime | None = None,
    cost_bps: float = 5.0,
    initial_cash: Decimal = Decimal("10000"),
    symbols: tuple[str, ...] = SYMBOLS,
) -> dict:
    """One idempotent pass: record any new decisions, then add the paper equity of new days."""
    now = now or utcnow()
    cutoff = last_complete_day(now)
    series: dict[str, Series] = {}
    digests: dict[str, str] = {}
    for symbol in symbols:
        payload, digest = fetch(symbol)
        try:
            series[symbol] = _trim(parse_yahoo_chart(payload, symbol), cutoff)
        except BacktestDataError as exc:
            raise EquityPaperError(str(exc)) from None
        digests[symbol] = digest
    try:
        market = build_market(series, "SPY")
    except BacktestDataError as exc:
        raise EquityPaperError(str(exc)) from None
    if len(market.days) < MIN_BARS:
        raise EquityPaperError(f"only {len(market.days)} bars; at least {MIN_BARS} are needed")
    data_digest = digest_of(digests)
    last_day = market.days[-1]
    report: dict = {
        "data_through": last_day.isoformat(),
        "paper_only": True,
        "strategies": {},
    }
    for key, (description, factory) in STRATEGIES.items():
        strategy = factory(market)
        with db.transaction() as session:
            run = session.get(EquityPaperRun, key)
            if run is None:
                run = EquityPaperRun(
                    strategy=key,
                    description=description,
                    start_day=last_day.isoformat(),
                    initial_cash=initial_cash,
                    cost_bps=Decimal(str(cost_bps)),
                )
                session.add(run)
            start_day = date.fromisoformat(run.start_day)
            stored = {
                date.fromisoformat(d.data_through): _weights_from_json(d.weights)
                for d in session.scalars(
                    select(EquityPaperDecision).where(EquityPaperDecision.strategy == key)
                )
            }
            planned = plan_decisions(market, strategy, start_day, set(stored))
            late = 0
            for day, weights, kind in planned:
                timely = is_timely(day, now)
                late += 0 if timely else 1
                session.add(
                    EquityPaperDecision(
                        strategy=key,
                        data_through=day.isoformat(),
                        kind=kind,
                        weights=_weights_json(weights),
                        recorded_at=now,
                        timely=timely,
                        data_digest=data_digest,
                    )
                )
                stored[day] = weights
            added = revised = 0
            first = market.days.index(start_day) + 1
            equity_now = None
            if first < len(market.days):
                result = run_backtest(
                    market,
                    RecordedDecisions(market, stored),
                    cost_bps=float(run.cost_bps),
                    first=first,
                )
                have = {
                    row.day: row.equity
                    for row in session.scalars(
                        select(EquityPaperNav).where(EquityPaperNav.strategy == key)
                    )
                }
                base = float(run.initial_cash)
                for k, (day, value) in enumerate(zip(result.days, result.equity, strict=True)):
                    stamp, equity = day.isoformat(), _usd(base * value)
                    if stamp in have:  # never rewritten; a provider revision is only counted
                        if abs(have[stamp] - equity) > Decimal("0.000001") + abs(equity) / 10**6:
                            revised += 1
                        continue
                    latest = k == len(result.days) - 1
                    session.add(
                        EquityPaperNav(
                            strategy=key,
                            day=stamp,
                            equity=equity,
                            holdings=json.dumps(
                                {s: f"{base * v:.6f}" for s, v in sorted(result.holdings.items())}
                                if latest
                                else {}
                            ),
                            pending=(
                                None
                                if not latest or result.pending is None
                                else _weights_json(result.pending)
                            ),
                            recorded_at=now,
                            data_digest=data_digest,
                        )
                    )
                    added += 1
                equity_now = _usd(base * result.equity[-1])
        report["strategies"][key] = {
            "start_day": start_day.isoformat(),
            "new_decisions": len(planned),
            "late_decisions": late,
            "new_nav_days": added,
            "revised_days": revised,
            "equity": None if equity_now is None else str(equity_now),
        }
    return report


def _evaluation(days: list[date], mine: list[float], theirs: list[float]) -> dict:
    if len(days) < FORWARD_MIN_DAYS:
        return {"state": "insufficient_sample", "days": len(days), "needed_days": FORWARD_MIN_DAYS}
    m, b = compute_metrics(days, mine), compute_metrics(days, theirs)
    verdict = judge(m, b)
    testable = b.max_drawdown <= -FORWARD_MIN_BENCHMARK_DRAWDOWN
    return {
        "state": "evaluated" if testable else "drawdown_not_testable",
        "days": len(days),
        "return_ok": verdict.return_ok,
        "risk_ok": verdict.risk_ok if testable else None,
        "cagr": m.cagr,
        "benchmark_cagr": b.cagr,
        "max_drawdown": m.max_drawdown,
        "benchmark_max_drawdown": b.max_drawdown,
    }


def status(db: Database) -> dict:
    """What the forward run has recorded so far (read only)."""
    out: dict = {"paper_only": True, "strategies": {}}
    with db.transaction() as session:
        runs = {r.strategy: r for r in session.scalars(select(EquityPaperRun))}
        navs: dict[str, list[EquityPaperNav]] = {}
        decisions: dict[str, list[EquityPaperDecision]] = {}
        for key in runs:
            navs[key] = list(
                session.scalars(
                    select(EquityPaperNav)
                    .where(EquityPaperNav.strategy == key)
                    .order_by(EquityPaperNav.day)
                )
            )
            decisions[key] = list(
                session.scalars(
                    select(EquityPaperDecision)
                    .where(EquityPaperDecision.strategy == key)
                    .order_by(EquityPaperDecision.data_through)
                )
            )
    series = {
        key: ([date.fromisoformat(r.day) for r in rows], [float(r.equity) for r in rows])
        for key, rows in navs.items()
    }
    for key, run in runs.items():
        rows, picks = navs[key], decisions[key]
        days, equity = series[key]
        initial = float(run.initial_cash)
        entry: dict = {
            "description": run.description,
            "start_day": run.start_day,
            "initial_cash": str(run.initial_cash),
            "cost_bps": str(run.cost_bps),
            "decisions": len(picks),
            "late_decisions": sum(1 for d in picks if not d.timely),
            "paper_days": len(rows),
        }
        if picks:
            last = picks[-1]
            entry["last_decision"] = {
                "data_through": last.data_through,
                "kind": last.kind,
                "timely": last.timely,
                "weights": json.loads(last.weights),
            }
        if rows:
            latest = rows[-1]
            entry["latest"] = {
                "day": latest.day,
                "equity": str(latest.equity),
                "return": float(latest.equity) / initial - 1.0,
                "holdings": json.loads(latest.holdings),
                "pending": None if latest.pending is None else json.loads(latest.pending),
            }
            if len(rows) >= 2:
                m = compute_metrics(days, [e / initial for e in equity])
                entry["max_drawdown"] = m.max_drawdown
        if key != BENCHMARK and BENCHMARK in series:
            bdays, bequity = series[BENCHMARK]
            common = sorted(set(days) & set(bdays))
            mine = dict(zip(days, equity, strict=True))
            theirs = dict(zip(bdays, bequity, strict=True))
            binitial = float(runs[BENCHMARK].initial_cash)
            entry["evaluation"] = _evaluation(
                common, [mine[d] / initial for d in common], [theirs[d] / binitial for d in common]
            )
        out["strategies"][key] = entry
    return out
