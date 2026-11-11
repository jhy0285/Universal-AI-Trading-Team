#!/usr/bin/env python3
"""Forward returns of a run's gate decisions, measured with external 15-minute candles (research only).

Why: the learning loop's own forward returns (`learning.gate_regret`) need a later snapshot of the SAME
pool, so a pool that dropped out of the observed set (died, lost liquidity) has no sample: survivor bias.
GeckoTerminal candles exist for every pool that traded, so this check is less biased. It is price only:
no fills, no depth, no costs. Read-only on the run database. Candles are cached, so a re-run does not call
the API again (free public API, about 30 calls a minute, so a pool costs 2.3 s).

    .venv/bin/python scripts/forward_from_candles.py --db path/to/worker.sqlite3 --work-dir path/to/dir

Prints a table and writes result.json in the work directory. Means are dominated by a few extreme values
(one pool showed +5,000,000%), so read the median and the trimmed mean.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

import httpx

DB: Path
OUT: Path
CACHE: Path
HURDLE = 0.049  # round trip fees and slippage cap, as in the learning loop
HORIZONS = {"1h": 3600, "6h": 21600, "24h": 86400}
STRICT_AGE = 3600  # strict: a candle (a trade) within one hour before the target time
BUCKET = 900
URL = "https://api.geckoterminal.com/api/v2/networks/solana/pools/{}/ohlcv/minute"
HEAD = {
    "Accept": "application/json;version=20230302",
    "User-Agent": "Universal-AI-Trading-Team/0.1 paper-research",
}


def load():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = c.execute(
        "select d.asset_id, d.allowed, d.reason, d.created_at, s.payload "
        "from decisions d join data_snapshots s on s.id = d.snapshot_id where s.kind = 'market'"
    ).fetchall()
    out = []
    for asset, allowed, reason, created, payload in rows:
        p = json.loads(payload)
        if not p.get("pair_id") or not p.get("price"):
            continue
        t = dt.datetime.fromisoformat(created).replace(tzinfo=dt.UTC).timestamp()
        out.append(
            {
                "asset": asset,
                "allowed": bool(allowed),
                "reason": reason,
                "t": t,
                "pool": p["pair_id"],
                "snap_price": float(p["price"]),
            }
        )
    return out


def fetch(pool: str):
    path = CACHE / f"{pool}.json"
    if path.exists():
        return json.loads(path.read_text())
    for attempt in range(5):
        r = httpx.get(
            URL.format(pool),
            params={"aggregate": 15, "limit": 1000, "currency": "usd"},
            headers=HEAD,
            timeout=30,
        )
        if r.status_code == 200:
            lst = sorted(
                (int(x[0]), float(x[4])) for x in r.json()["data"]["attributes"]["ohlcv_list"]
            )
            path.write_text(json.dumps(lst))
            time.sleep(2.3)
            return lst
        if r.status_code == 404:
            path.write_text("[]")
            time.sleep(2.3)
            return []
        if r.status_code == 429:
            time.sleep(15 * (attempt + 1))
            continue
        raise SystemExit(f"unexpected status {r.status_code} for a pool: {r.text[:120]}")
    raise SystemExit("rate limited five times in a row; stopping")


def price_at(candles, when: float):
    """Close of the last 15-minute candle that ended at or before `when`, and how old it is."""
    best = None
    for ts, close in candles:
        if ts + BUCKET <= when:
            best = (close, when - (ts + BUCKET))
        else:
            break
    return best


def stats(vals, assets):
    if not vals:
        return {"n": 0}
    per_asset = defaultdict(list)
    for asset, v in assets:
        per_asset[asset].append(v)
    ordered = sorted(vals)
    cut = max(1, len(ordered) // 100)
    trimmed = ordered[cut:-cut] if len(ordered) > 2 * cut else ordered
    return {
        "trim_mean": round(sum(trimmed) / len(trimmed), 4),
        "p10": round(ordered[len(ordered) // 10], 4),
        "p90": round(ordered[(len(ordered) * 9) // 10], 4),
        "max": round(ordered[-1], 2),
        "min": round(ordered[0], 4),
        "n": len(vals),
        "assets": len(per_asset),
        "mean": round(sum(vals) / len(vals), 4),
        "median": round(statistics.median(vals), 4),
        "over_hurdle": round(sum(v > HURDLE for v in vals) / len(vals), 3),
        "asset_mean": round(sum(sum(x) / len(x) for x in per_asset.values()) / len(per_asset), 4),
        "asset_share_positive": round(
            sum(sum(x) / len(x) > 0 for x in per_asset.values()) / len(per_asset), 3
        ),
    }


def main():
    global DB, OUT, CACHE
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--db", type=Path, required=True, help="the run's SQLite database (opened read-only)"
    )
    parser.add_argument(
        "--work-dir", type=Path, required=True, help="where candles are cached and result.json goes"
    )
    args = parser.parse_args()
    DB, OUT = args.db, args.work_dir
    CACHE = OUT / "candles"
    CACHE.mkdir(parents=True, exist_ok=True)
    decisions = load()
    pools = sorted({d["pool"] for d in decisions})
    print(f"{len(decisions)} decisions, {len(pools)} pools", flush=True)
    candles = {}
    for i, pool in enumerate(pools, 1):
        candles[pool] = fetch(pool)
        if i % 20 == 0:
            print(f"fetched {i}/{len(pools)}", flush=True)
    now = dt.datetime.now(dt.UTC).timestamp()
    ratios, no_base = [], 0
    samples = defaultdict(lambda: defaultdict(lambda: {"strict": [], "carry": [], "missing": 0}))
    for d in decisions:
        cs = candles[d["pool"]]
        base = price_at(cs, d["t"])
        if base is None or base[0] <= 0:
            no_base += 1
            continue
        ratios.append(d["snap_price"] / base[0])
        for name, h in HORIZONS.items():
            target = d["t"] + h
            if target > now - BUCKET:
                continue
            got = price_at(cs, target)
            bucket = samples[(d["allowed"], d["reason"])][name]
            if got is None:
                bucket["missing"] += 1
                continue
            ret = got[0] / base[0] - 1
            bucket["carry"].append((d["asset"], ret))
            if got[1] <= STRICT_AGE:
                bucket["strict"].append((d["asset"], ret))
    result = {
        "decisions": len(decisions),
        "pools": len(pools),
        "no_base_price": no_base,
        "snapshot_vs_candle_price_ratio_median": round(statistics.median(ratios), 3)
        if ratios
        else None,
        "groups": {},
    }
    everything = defaultdict(lambda: {"strict": [], "carry": [], "missing": 0})
    for key, hs in samples.items():
        label = ("ALLOWED " if key[0] else "blocked ") + key[1]
        result["groups"][label] = {}
        for name, b in hs.items():
            result["groups"][label][name] = {
                "strict": stats([v for _, v in b["strict"]], b["strict"]),
                "carry": stats([v for _, v in b["carry"]], b["carry"]),
                "missing": b["missing"],
            }
            e = everything[name]
            e["strict"] += b["strict"]
            e["carry"] += b["carry"]
            e["missing"] += b["missing"]
    result["all_decisions"] = {
        name: {
            "strict": stats([v for _, v in e["strict"]], e["strict"]),
            "carry": stats([v for _, v in e["carry"]], e["carry"]),
            "missing": e["missing"],
        }
        for name, e in everything.items()
    }
    (OUT / "result.json").write_text(json.dumps(result, indent=1))
    report(result)


def report(result: dict) -> None:
    def line(x):
        if not x.get("n"):
            return "n=0"
        return (
            f"n={x['n']:>5} assets={x['assets']:>3} median={x['median'] * 100:+6.1f}% "
            f"trimmed mean={x['trim_mean'] * 100:+6.1f}% p10={x['p10'] * 100:+5.0f}% "
            f"p90={x['p90'] * 100:+5.0f}% over hurdle={x['over_hurdle'] * 100:3.0f}%"
        )

    print(
        f"{result['decisions']} decisions, {result['pools']} pools, {result['no_base_price']} without a base"
        f" candle, snapshot/candle price ratio {result['snapshot_vs_candle_price_ratio_median']}"
    )
    print(
        "strict = a trade within 1 h before the target time; carry = the last known price carried forward"
    )
    for label, groups in [("ALL", result["all_decisions"]), *result["groups"].items()]:
        if label != "ALL" and groups.get("1h", {}).get("carry", {}).get("n", 0) < 60:
            continue
        print(label)
        for horizon in ("1h", "6h", "24h"):
            if horizon in groups:
                print(f"  {horizon:>4} strict {line(groups[horizon]['strict'])}")
                print(f"       carry  {line(groups[horizon]['carry'])}")


if __name__ == "__main__":
    sys.exit(main())
