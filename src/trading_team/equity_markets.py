"""Fixed market profiles for the equity backtests: which symbols, which benchmark, which costs.

A profile is written down BEFORE its candidates are run (docs/equity-backtest.md for the US,
docs/equity-backtest-kr.md for Korea). The symbol lists are chosen by asset class and by whether
the provider has usable history, never by how a symbol performed. Research only: nothing here
places an order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from trading_team.equity_backtest import (
    DEFAULT_UNIVERSE,
    DEFENSIVE_ASSET,
    DEV_END,
    HOLDOUT_START,
)


@dataclass(frozen=True, slots=True)
class Profile:
    key: str
    label: str
    reference: str  # the benchmark: bought and held, and the calendar of the whole market
    universe: tuple[str, ...]  # ranked by the momentum rules; excludes the defensive asset
    defensive: str  # holds the unallocated share (what an investor holds instead of idle cash)
    cost_bps: float  # one-way cost used for the criterion
    robust_cost_bps: float  # the return condition must also hold at this higher cost
    dev_end: date
    holdout_start: date
    history_start: date | None = None  # bars before this date are unusable and left out
    cache_dir: str = ".state/equity-cache"

    @property
    def symbols(self) -> tuple[str, ...]:
        """Everything to download: the universe, the defensive asset and the reference."""
        extra = tuple(s for s in (self.defensive, self.reference) if s not in self.universe)
        return (*self.universe, *dict.fromkeys(extra))


US = Profile(
    key="us",
    label="US ETFs",
    reference="SPY",
    universe=DEFAULT_UNIVERSE,
    defensive=DEFENSIVE_ASSET,
    cost_bps=5.0,
    robust_cost_bps=10.0,
    dev_end=DEV_END,
    holdout_start=HOLDOUT_START,
)

# Korean-listed ETFs on Yahoo (".KS"), one flagship per asset class or sector that was listed
# before 2018. KODEX 200 is the benchmark; TIGER 200 (a duplicate of it) and the KRW cash ETF
# (an alternative defensive asset) were surveyed and left out. Korean domestic ETFs are exempt
# from the securities transaction tax, so the cost is a commission plus a half spread; 10 bps
# one way is a deliberately rough, conservative figure for the thin sector ETFs.
KR_UNIVERSE = (
    "069500.KS",  # KODEX 200 (benchmark)
    "229200.KS",  # KODEX KOSDAQ 150
    "091160.KS",  # semiconductors
    "091170.KS",  # banks
    "091180.KS",  # autos
    "117460.KS",  # energy & chemicals
    "117680.KS",  # steel
    "117700.KS",  # construction
    "140700.KS",  # insurance
    "140710.KS",  # transportation
    "266420.KS",  # healthcare
    "266360.KS",  # IT software
    "266370.KS",  # IT hardware
    "133690.KS",  # TIGER US Nasdaq 100
    "143850.KS",  # TIGER US S&P 500 futures (H)
    "195930.KS",  # TIGER Euro Stoxx 50 (H)
    "192090.KS",  # TIGER China A300
    "238720.KS",  # KINDEX Nikkei 225 (H)
    "148070.KS",  # KOSEF 10-year KTB
    "132030.KS",  # KODEX gold futures
)
KR = Profile(
    key="kr",
    label="Korean ETFs",
    reference="069500.KS",
    universe=KR_UNIVERSE,
    defensive="114100.KS",  # KODEX 3-year KTB: the analogue of SHY
    cost_bps=10.0,
    robust_cost_bps=20.0,
    dev_end=DEV_END,
    holdout_start=HOLDOUT_START,
    # Yahoo returns empty placeholder rows for the Korean ETFs from 2007-02 to 2009-04-16; the
    # first real bar after them is 2009-04-17. Earlier bars would sit across a two-year gap.
    history_start=date(2009, 4, 17),
    cache_dir=".state/kr-cache",
)

PROFILES = {profile.key: profile for profile in (US, KR)}
