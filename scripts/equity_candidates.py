#!/usr/bin/env python3
"""Run the pre-registered candidate strategies and check them against the success criterion.

Protocol (fixed before running, see docs/equity-backtest.md):
1. Every candidate is run on the development window (2001 to 2019) at 5 bps and 10 bps.
2. A candidate passes if, in the development window, its CAGR trails buy-and-hold SPY by at most
   1 percentage point AND its maximum drawdown is at most 60% of SPY's, and the CAGR condition
   still holds at 10 bps.
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
    DEFAULT_UNIVERSE,
    DEFENSIVE_ASSET,
    DEV_END,
    DRAWDOWN_SHARE,
    HOLDOUT_START,
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
    subperiod,
)


def window(result: Result, start: date | None, end: date | None) -> Metrics:
    return subperiod(result, start or result.days[0], end or result.days[-1])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=Path(".state/equity-cache"))
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    symbols = (*DEFAULT_UNIVERSE, DEFENSIVE_ASSET)
    series = {}
    for symbol in symbols:
        payload, _, _ = fetch(symbol, args.cache_dir, args.refresh)
        series[symbol] = parse_yahoo_chart(payload, symbol)
    market = build_market(series, "SPY")
    strategies = {"benchmark": BuyAndHold(market, "SPY"), **candidates(market)}
    runs = {
        bps: {
            k: run_backtest(market, s, cost_bps=bps, first=TRADING_DAYS)
            for k, s in strategies.items()
        }
        for bps in (5, 10)
    }
    days = runs[5]["benchmark"].days
    dev_last = max(d for d in days if d <= DEV_END)
    bench = {bps: window(r["benchmark"], None, DEV_END) for bps, r in runs.items()}

    print(
        f"CRITERION (fixed before running): CAGR >= SPY - {RETURN_TOLERANCE * 100:.0f} pp and max"
        f" drawdown >= {DRAWDOWN_SHARE:.0%} of SPY's. {len(strategies) - 1} candidates were run."
    )
    print(f"DEVELOPMENT {days[0]} -> {dev_last} (defensive asset {DEFENSIVE_ASSET}, 5 bps)")
    print(
        f"  {'':<16} {'CAGR':>7} {'maxDD':>8} {'Sharpe':>7} {'orders/yr':>9}  return risk  cost-robust"
    )
    out: dict = {"development": {}}
    passing: list[tuple[float, float, str]] = []
    for key, res in runs[5].items():
        m5 = window(res, None, DEV_END)
        m10 = window(runs[10][key], None, DEV_END)
        verdict = judge(m5, bench[5])
        robust = key != "benchmark" and judge(m10, bench[10]).return_ok
        tag = (
            ""
            if key == "benchmark"
            else (
                f"  {'ok' if verdict.return_ok else 'no':>5} {'ok' if verdict.risk_ok else 'no':>4}  "
                f"{'ok' if robust else 'no'}"
            )
        )
        orders = res.orders / m5.years
        print(
            f"  {key:<16} {pct(m5.cagr)} {pct(m5.max_drawdown)} "
            f"{(m5.sharpe if m5.sharpe is not None else float('nan')):7.2f} {orders:9.1f}{tag}"
        )
        out["development"][key] = {
            "cagr": m5.cagr, "max_drawdown": m5.max_drawdown, "sharpe": m5.sharpe,
            "orders_per_year": orders, "passed": verdict.passed, "cost_robust": robust,
        }  # fmt: skip
        if key != "benchmark" and verdict.passed and robust:
            passing.append((-(m5.sharpe or float("-inf")), orders, key))

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
        hold_start = min(d for d in days if d >= HOLDOUT_START)
        print(f"HOLD-OUT {hold_start} -> {days[-1]} (opened once, chosen candidate only)")
        out["holdout"] = {}
        for bps in (5, 10):
            hb = window(runs[bps]["benchmark"], hold_start, None)
            hc = window(runs[bps][chosen], hold_start, None)
            v = judge(hc, hb)
            print(
                f"  {bps:>2} bps  SPY buy and hold: CAGR {pct(hb.cagr)} maxDD {pct(hb.max_drawdown)}"
                f" | {chosen}: CAGR {pct(hc.cagr)} maxDD {pct(hc.max_drawdown)}"
                f" | return {'ok' if v.return_ok else 'no'}, risk {'ok' if v.risk_ok else 'no'}"
            )
            out["holdout"][str(bps)] = {
                "benchmark": [hb.cagr, hb.max_drawdown], "chosen": [hc.cagr, hc.max_drawdown],
                "return_ok": v.return_ok, "risk_ok": v.risk_ok,
            }  # fmt: skip
        first, second = days[0], hold_start
        print(f"  (windows: {first} -> {dev_last} and {second} -> {days[-1]}; overlapping nothing)")
    if args.json:
        print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
