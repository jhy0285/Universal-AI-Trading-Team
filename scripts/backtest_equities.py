#!/usr/bin/env python3
"""Backtest fixed, rule-based strategies on daily US ETF bars and compare them with buy-and-hold SPY.

Research only: it downloads historical bars (Yahoo chart endpoint, unofficial and without any
stability promise), runs the strategies offline and prints a report. It never places an order.
The raw responses are cached and their SHA-256 is printed so a result can be traced to its data.
Strategy parameters are fixed in advance from the literature; nothing is tuned here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from trading_team.equity_backtest import (
    DEFAULT_UNIVERSE,
    TRADING_DAYS,
    BacktestDataError,
    BuyAndHold,
    DualMomentum,
    Market,
    Metrics,
    Result,
    RSI2Reversion,
    TrendFilter,
    build_market,
    parse_yahoo_chart,
    rolling_beat_rate,
    run_backtest,
    subperiod,
)

URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
# The same identifying User-Agent the app's public data client sends. Yahoo rejects the default one.
USER_AGENT = "Universal-AI-Trading-Team/0.1 paper-research"
HISTORY_FROM = date(2000, 1, 1)


def fetch(symbol: str, cache: Path, refresh: bool) -> tuple[dict, str, str]:
    path = cache / f"{symbol}.json"
    if refresh or not path.is_file():
        params = {
            "period1": int(datetime(HISTORY_FROM.year, 1, 1, tzinfo=UTC).timestamp()),
            "period2": int(time.time()),
            "interval": "1d",
            "events": "div|split",
        }
        for attempt in range(4):
            reply = httpx.get(
                URL.format(symbol=symbol),
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=40,
            )
            if reply.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            reply.raise_for_status()
            break
        else:
            raise SystemExit(f"{symbol}: still rate limited after retries")
        cache.mkdir(parents=True, exist_ok=True)
        path.write_bytes(reply.content)
        time.sleep(1)
    raw = path.read_bytes()
    fetched = datetime.fromtimestamp(path.stat().st_mtime, UTC).strftime("%Y-%m-%d %H:%MZ")
    return json.loads(raw), hashlib.sha256(raw).hexdigest(), fetched


def strategies(market: Market) -> dict[str, object]:
    return {
        "benchmark": BuyAndHold(market, "SPY"),
        "trend": TrendFilter(market, "SPY", 200),
        "momentum": DualMomentum(market, DEFAULT_UNIVERSE, top_n=3),
        "rsi2": RSI2Reversion(market, "SPY"),
    }


def pct(value: float | None) -> str:
    return "   n/a" if value is None else f"{value * 100:6.1f}%"


def num(value: float | None) -> str:
    return "  n/a" if value is None else f"{value:5.2f}"


def metrics_row(label: str, m: Metrics, extra: str = "") -> str:
    return (
        f"  {label:<10} CAGR {pct(m.cagr)} | vol {pct(m.volatility)} | Sharpe {num(m.sharpe)}"
        f" | max drawdown {pct(m.max_drawdown)} | longest underwater {m.longest_underwater_days:5d}d"
        f"{extra}"
    )


def run_all(market: Market, cost_bps: float) -> dict[str, Result]:
    return {
        key: run_backtest(market, strat, cost_bps=cost_bps, first=TRADING_DAYS)
        for key, strat in strategies(market).items()
    }


def asdict_metrics(m: Metrics) -> dict:
    return {f: getattr(m, f) for f in m.__slots__}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=Path(".state/equity-cache"))
    parser.add_argument("--refresh", action="store_true", help="download the bars again")
    parser.add_argument("--cost-bps", type=float, default=5.0, help="one-way cost per trade")
    parser.add_argument("--split", default="2013-01-01", help="start of the second sub-period")
    parser.add_argument("--json", action="store_true", help="print the raw numbers as JSON")
    args = parser.parse_args()

    series, provenance = {}, {}
    for symbol in DEFAULT_UNIVERSE:
        payload, digest, fetched = fetch(symbol, args.cache_dir, args.refresh)
        try:
            series[symbol] = parse_yahoo_chart(payload, symbol)
        except BacktestDataError as exc:
            raise SystemExit(f"unusable data: {exc}") from exc
        provenance[symbol] = (digest, fetched)
    market = build_market(series, "SPY")
    split = date.fromisoformat(args.split)
    results = run_all(market, args.cost_bps)
    days = results["benchmark"].days
    out: dict = {"window": [days[0].isoformat(), days[-1].isoformat()], "cost_bps": args.cost_bps}

    lines = ["DATA (daily, adjusted for dividends and splits; Yahoo chart, unofficial)"]
    for symbol, s in series.items():
        digest, fetched = provenance[symbol]
        lines.append(
            f"  {symbol:<4} {len(s.bars):5d} bars from {s.bars[0].day} | dropped {s.dropped}"
            f" | gaps {market.gaps[symbol]} | extra {market.extra[symbol]}"
            f" | sha256 {digest[:12]} | fetched {fetched}"
        )
    lines.append(
        f"\nWINDOW {days[0]} -> {days[-1]} ({len(days)} trading days); the first {TRADING_DAYS}"
        f" days of 2000 only warm up the indicators. One-way cost {args.cost_bps:g} bps."
    )
    spans = [("full", days[0], days[-1])]
    spans += [("before", days[0], max(d for d in days if d < split))]
    spans += [("since", min(d for d in days if d >= split), days[-1])]
    out["periods"] = {}
    for tag, start, end in spans:
        lines.append(f"\n{tag.upper()} {start} -> {end}")
        out["periods"][tag] = {}
        for key, res in results.items():
            m = subperiod(res, start, end)
            extra = ""
            if tag == "full":
                years = m.years
                extra = (
                    f" | invested {res.invested_days / len(res.days) * 100:4.0f}%"
                    f" | orders/yr {res.orders / years:5.1f}"
                )
            lines.append(metrics_row(key, m, extra) + f"  [{res.name}]" * (tag == "full"))
            out["periods"][tag][key] = asdict_metrics(m)

    lines.append("\nCOST SENSITIVITY (full-window CAGR)")
    out["cost_sensitivity"] = {}
    for bps in (0, 5, 10, 25):
        row = run_all(market, bps)
        cells = {k: subperiod(r, days[0], days[-1]).cagr for k, r in row.items()}
        out["cost_sensitivity"][str(bps)] = cells
        lines.append(f"  {bps:>3} bps  " + " | ".join(f"{k} {pct(v)}" for k, v in cells.items()))

    lines.append("\nROLLING 3-YEAR WINDOWS (share of windows whose CAGR beat buy-and-hold SPY)")
    out["rolling_3y"] = {}
    bench = results["benchmark"].equity
    for key, res in results.items():
        if key == "benchmark":
            continue
        rate, count = rolling_beat_rate(days, res.equity, bench)
        out["rolling_3y"][key] = {"beat_rate": rate, "windows": count}
        lines.append(f"  {key:<10} {pct(rate)} of {count} windows (overlapping, step 21 days)")

    lines.append(
        "\nREAD WITH CARE: 3 fixed strategies x 4 cost levels, parameters not tuned. Overlapping"
        " windows are not independent. Cash earns 0%. Taxes, FX and the future are not modelled."
    )
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print("\n".join(lines))


if __name__ == "__main__":
    main()
