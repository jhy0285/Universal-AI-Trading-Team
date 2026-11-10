#!/usr/bin/env python3
"""Run the pre-registered candidate strategies and check them against the success criterion.

Protocol (fixed before running, see docs/equity-backtest.md and docs/equity-backtest-kr.md):
1. Every candidate is run on the development window (the data start to 2019) at the profile's
   cost and at the higher robustness cost (US: 5 and 10 bps, Korea: 10 and 20 bps).
2. A candidate passes if, in the development window, its CAGR trails buy-and-hold of the
   benchmark by at most 1 percentage point AND its maximum drawdown is at most 60% of the
   benchmark's, and the CAGR condition still holds at the higher cost.
3. Among the passing candidates the one with the highest development Sharpe is chosen (ties:
   fewer orders per year, then name). Nothing else is used to choose.
4. Only the chosen candidate is evaluated on the hold-out window (2020 onwards), once.
The hold-out numbers of the other candidates are never printed, so they cannot steer the choice.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from backtest_equities import fetch, pct

from trading_team.equity_backtest import (
    DRAWDOWN_SHARE,
    RETURN_TOLERANCE,
    TRADING_DAYS,
    BuyAndHold,
    Metrics,
    Result,
    build_market,
    candidates,
    judge,
    parse_yahoo_chart,
    run_backtest,
    since,
    subperiod,
)
from trading_team.equity_markets import PROFILES


def window(result: Result, start: date | None, end: date | None) -> Metrics:
    return subperiod(result, start or result.days[0], end or result.days[-1])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", choices=sorted(PROFILES), default="us")
    parser.add_argument("--cache-dir", type=Path, default=None, help="default: the profile's")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    profile = PROFILES[args.market]
    cache = args.cache_dir or Path(profile.cache_dir)
    low, high = profile.cost_bps, profile.robust_cost_bps
    ref = profile.reference

    series, digests = {}, {}
    for symbol in profile.symbols:
        payload, digest, _ = fetch(symbol, cache, args.refresh)
        parsed = parse_yahoo_chart(payload, symbol)
        series[symbol] = since(parsed, profile.history_start) if profile.history_start else parsed
        digests[symbol] = digest
    market = build_market(series, ref)
    strategies = {
        "benchmark": BuyAndHold(market, ref),
        **candidates(market, profile.defensive, symbol=ref, universe=profile.universe),
    }
    runs = {
        bps: {
            k: run_backtest(market, s, cost_bps=bps, first=TRADING_DAYS)
            for k, s in strategies.items()
        }
        for bps in (low, high)
    }
    days = runs[low]["benchmark"].days
    dev_last = max(d for d in days if d <= profile.dev_end)
    bench = {bps: window(r["benchmark"], None, profile.dev_end) for bps, r in runs.items()}

    print(f"MARKET {profile.label}: benchmark {ref}, defensive asset {profile.defensive}")
    print("DATA (daily, adjusted for dividends and splits; Yahoo chart, unofficial)")
    for symbol, one in series.items():
        print(
            f"  {symbol:<10} {len(one.bars):5d} bars from {one.bars[0].day} | dropped {one.dropped}"
            f" | gaps {market.gaps[symbol]} | extra {market.extra[symbol]}"
            f" | sha256 {digests[symbol][:12]}"
        )
    print(
        f"CRITERION (fixed before running): CAGR >= {ref} - {RETURN_TOLERANCE * 100:.0f} pp and"
        f" max drawdown >= {DRAWDOWN_SHARE:.0%} of {ref}'s. {len(strategies) - 1} candidates were run."
    )
    print(f"DEVELOPMENT {days[0]} -> {dev_last} (defensive asset {profile.defensive}, {low:g} bps)")
    print(
        f"  {'':<16} {'CAGR':>7} {'maxDD':>8} {'Sharpe':>7} {'orders/yr':>9}  return risk  cost-robust"
    )
    out: dict = {"market": profile.key, "development": {}}
    passing: list[tuple[float, float, str]] = []
    for key, res in runs[low].items():
        m_low = window(res, None, profile.dev_end)
        m_high = window(runs[high][key], None, profile.dev_end)
        verdict = judge(m_low, bench[low])
        robust = key != "benchmark" and judge(m_high, bench[high]).return_ok
        tag = (
            ""
            if key == "benchmark"
            else (
                f"  {'ok' if verdict.return_ok else 'no':>5} {'ok' if verdict.risk_ok else 'no':>4}  "
                f"{'ok' if robust else 'no'}"
            )
        )
        orders = res.orders / m_low.years
        print(
            f"  {key:<16} {pct(m_low.cagr)} {pct(m_low.max_drawdown)} "
            f"{(m_low.sharpe if m_low.sharpe is not None else float('nan')):7.2f} {orders:9.1f}{tag}"
        )
        out["development"][key] = {
            "cagr": m_low.cagr, "max_drawdown": m_low.max_drawdown, "sharpe": m_low.sharpe,
            "orders_per_year": orders, "passed": verdict.passed, "cost_robust": robust,
        }  # fmt: skip
        if key != "benchmark" and verdict.passed and robust:
            passing.append((-(m_low.sharpe or float("-inf")), orders, key))

    if not passing:
        print(
            "\nNo candidate passed the criterion on the development window. The hold-out window"
            " was NOT opened."
        )
        out["chosen"] = None
    else:
        passing.sort()
        chosen = passing[0][2]
        out["chosen"] = chosen
        print(
            f"\n{len(passing)} candidate(s) passed in development. Chosen by the highest"
            f" development Sharpe: {chosen}  [{strategies[chosen].name}]"
        )
        hold_start = min(d for d in days if d >= profile.holdout_start)
        print(f"HOLD-OUT {hold_start} -> {days[-1]} (opened once, chosen candidate only)")
        out["holdout"] = {}
        for bps in (low, high):
            hb = window(runs[bps]["benchmark"], hold_start, None)
            hc = window(runs[bps][chosen], hold_start, None)
            v = judge(hc, hb)
            print(
                f"  {bps:>2g} bps  {ref} buy and hold: CAGR {pct(hb.cagr)} maxDD {pct(hb.max_drawdown)}"
                f" | {chosen}: CAGR {pct(hc.cagr)} maxDD {pct(hc.max_drawdown)}"
                f" | return {'ok' if v.return_ok else 'no'}, risk {'ok' if v.risk_ok else 'no'}"
            )
            out["holdout"][f"{bps:g}"] = {
                "benchmark": [hb.cagr, hb.max_drawdown], "chosen": [hc.cagr, hc.max_drawdown],
                "return_ok": v.return_ok, "risk_ok": v.risk_ok,
            }  # fmt: skip
        first, second = days[0], hold_start
        print(f"  (windows: {first} -> {dev_last} and {second} -> {days[-1]}; overlapping nothing)")
    if args.json:
        print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
