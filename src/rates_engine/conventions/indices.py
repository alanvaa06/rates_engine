"""Rate indices as data, and the collateral rate each currency discounts at.

Until this module existed a second currency was a tag. ``Currency.MXN`` rode
on the curve and the cashflows, but every valuation still wrote
``"discounting": "collateral_rate_ois_sofr"`` into its evidence, because the
string was a literal in the pricer. A peso swap priced on a peso curve came
back saying it had been discounted on OIS-SOFR -- a false statement in the
one place this package promises is true.

The fix is to make the index a value. A :class:`RateIndex` carries what a
valuation needs to say about the rate behind it: the currency, the tenor,
the accrual basis, the calendar it fixes on, and which of its conventions
this build assumed rather than verified. :func:`collateral_index` maps a
currency to the overnight rate its collateral earns, which is the curve its
collateralised flows discount on (Fujii-Shimada-Takahashi; Piterbarg).

**Adding a currency is adding data.** A new :class:`~rates_engine.core.
money.Currency` member needs an entry in :data:`COLLATERAL_INDEX` and
nothing else in the pricers; ``tests/test_indices.py`` fails until it has
one, so a currency cannot exist without saying what it discounts at.

**What is unverified is data too.** :data:`UNRESOLVED_MXN` lists every peso
convention this build assumes. It lives here rather than beside the MXN
curve because the pricers, the curve and the CLI all read it, and the
lowest layer they share is this one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from rates_engine.conventions.calendar import BMV, SIFMA_US, HolidayCalendar
from rates_engine.conventions.daycount import DayCount
from rates_engine.core.errors import UnsupportedConventionError
from rates_engine.core.money import Currency

__all__ = [
    "IndexTenor",
    "RateIndex",
    "SOFR",
    "TIIE_FONDEO",
    "TIIE_28",
    "TERM_SOFR_1M",
    "TERM_SOFR_3M",
    "TERM_SOFR_6M",
    "TERM_SOFR_12M",
    "INDICES",
    "index_named",
    "term_sofr",
    "COLLATERAL_INDEX",
    "collateral_index",
    "TIIE_PERIOD_DAYS",
    "TIIE_DAY_COUNT",
    "UNRESOLVED_MXN",
]

TIIE_PERIOD_DAYS = 28
"""The assumed TIIE coupon period, in calendar days. Unverified — see
:data:`UNRESOLVED_MXN`."""

TIIE_DAY_COUNT = DayCount.ACT_360
"""The assumed TIIE accrual basis. Unverified — see :data:`UNRESOLVED_MXN`."""

UNRESOLVED_MXN: tuple[tuple[str, str], ...] = (
    (
        "tiie_day_count",
        f"ACT/360 assumed for TIIE accrual; not confirmed against Banxico. "
        f"Currently {TIIE_DAY_COUNT.value}.",
    ),
    (
        "tiie_period_days",
        f"A {TIIE_PERIOD_DAYS}-day coupon period assumed; not confirmed against "
        "Banxico.",
    ),
    (
        "tiie_fondeo_vs_28",
        "Which conventions attach to TIIE de Fondeo as against TIIE 28 is not "
        "established. This build applies the same ones to both, which is an "
        "assumption and possibly a wrong one.",
    ),
    (
        "banxico_series_ids",
        "The Banxico SIE series identifiers for either benchmark are not known "
        "here, so fixings must be supplied by the caller rather than fetched.",
    ),
    (
        "banxico_quotes_in_percent",
        "Series from Banxico's SIE are divided by 100 on the assumption that SIE "
        "quotes rates in percent, as FRED does. Never confirmed against the live "
        "endpoint, which this build cannot reach. A wrong guess is a hundredfold "
        "error in every fixing.",
    ),
    (
        "mxn_calendar_is_bmv_not_banxico",
        "Business days come from the BMV (stock exchange) calendar. Banxico's "
        "banking calendar is a different list and the two have not been diffed.",
    ),
    (
        "mxn_collateral_rate",
        "Peso flows are discounted on the assumption that MXN collateral is "
        "remunerated at TIIE de Fondeo, the overnight rate. The CSA convention for "
        "peso collateral has not been confirmed against a primary source.",
    ),
)
"""Every MXN convention this build assumes rather than knows.

A tuple of ``(name, why)``. Each one becomes a
:class:`~rates_engine.core.evidence.Degradation` on every MXN curve, and
``strict_conventions=True`` turns the set into a refusal. When Banxico
becomes reachable this tuple shrinks and nothing else changes.
"""


class IndexTenor(StrEnum):
    """How long one fixing of an index accrues.

    ``OVERNIGHT``
        One business day, compounded over a period: SOFR, TIIE de Fondeo.
    ``TERM_28D``
        A 28-day term rate known at the start of its period: TIIE 28.
    ``TERM_1M``, ``TERM_3M``, ``TERM_6M``, ``TERM_12M``
        A term rate for that many months, known at the start of its period:
        Term SOFR.
    """

    OVERNIGHT = "overnight"
    TERM_28D = "28d"
    TERM_1M = "1m"
    TERM_3M = "3m"
    TERM_6M = "6m"
    TERM_12M = "12m"

    @property
    def months(self) -> int | None:
        """The tenor in months, or ``None`` for overnight and day-count tenors."""
        return {"1m": 1, "3m": 3, "6m": 6, "12m": 12}.get(self.value)


@dataclass(frozen=True)
class RateIndex:
    """A published rate and the conventions a valuation on it rests on.

    Attributes:
        name: Stable identifier, e.g. ``"SOFR"``.
        label: How the evidence names it in prose, e.g. ``"TIIE de Fondeo"``.
        slug: How the evidence names it in a machine field, e.g.
            ``"tiie_fondeo"``; ``collateral_rate_ois_<slug>`` is the discounting
            tag of a curve on this index.
        currency: What it is a rate in.
        tenor: How long one fixing accrues.
        day_count: Accrual basis.
        calendar: The calendar it fixes and pays on.
        administrator: Who publishes it.
        unresolved: Names from :data:`UNRESOLVED_MXN` (or its successors) that
            any valuation on this index inherits as assumptions. Empty when
            every convention was verified against its administrator.
    """

    name: str
    label: str
    slug: str
    currency: Currency
    tenor: IndexTenor
    day_count: DayCount
    calendar: HolidayCalendar
    administrator: str
    unresolved: tuple[str, ...] = ()

    @property
    def is_verified(self) -> bool:
        """True when no convention of this index is an assumption."""
        return not self.unresolved


SOFR = RateIndex(
    name="SOFR",
    label="SOFR",
    slug="sofr",
    currency=Currency.USD,
    tenor=IndexTenor.OVERNIGHT,
    day_count=DayCount.ACT_360,
    calendar=SIFMA_US,
    administrator="Federal Reserve Bank of New York",
)
"""The Secured Overnight Financing Rate, compounded in arrears on ACT/360."""

TIIE_FONDEO = RateIndex(
    name="TIIE_FONDEO",
    label="TIIE de Fondeo",
    slug="tiie_fondeo",
    currency=Currency.MXN,
    tenor=IndexTenor.OVERNIGHT,
    day_count=TIIE_DAY_COUNT,
    calendar=BMV,
    administrator="Banco de Mexico",
    unresolved=(
        "tiie_day_count",
        "tiie_fondeo_vs_28",
        "mxn_calendar_is_bmv_not_banxico",
        "mxn_collateral_rate",
    ),
)
"""The peso overnight funding rate, the reference since TIIE 28's replacement."""

TIIE_28 = RateIndex(
    name="TIIE_28",
    label="TIIE 28",
    slug="tiie_28",
    currency=Currency.MXN,
    tenor=IndexTenor.TERM_28D,
    day_count=TIIE_DAY_COUNT,
    calendar=BMV,
    administrator="Banco de Mexico",
    unresolved=(
        "tiie_day_count",
        "tiie_period_days",
        "tiie_fondeo_vs_28",
        "mxn_calendar_is_bmv_not_banxico",
    ),
)
"""The historical 28-day interbank rate."""


def _term_sofr(tenor: IndexTenor) -> RateIndex:
    return RateIndex(
        name=f"TERM_SOFR_{tenor.value.upper()}",
        label=f"Term SOFR {tenor.value.upper()}",
        slug=f"term_sofr_{tenor.value}",
        currency=Currency.USD,
        tenor=tenor,
        day_count=DayCount.ACT_360,
        calendar=SIFMA_US,
        administrator="CME Group Benchmark Administration",
    )


TERM_SOFR_1M = _term_sofr(IndexTenor.TERM_1M)
"""CME Term SOFR, one month."""
TERM_SOFR_3M = _term_sofr(IndexTenor.TERM_3M)
"""CME Term SOFR, three months: the default floating index of an IRS here."""
TERM_SOFR_6M = _term_sofr(IndexTenor.TERM_6M)
"""CME Term SOFR, six months."""
TERM_SOFR_12M = _term_sofr(IndexTenor.TERM_12M)
"""CME Term SOFR, twelve months."""

INDICES: dict[str, RateIndex] = {
    index.name: index
    for index in (
        SOFR, TIIE_FONDEO, TIIE_28, TERM_SOFR_1M, TERM_SOFR_3M, TERM_SOFR_6M, TERM_SOFR_12M
    )
}
"""Every index this build defines, by :attr:`RateIndex.name`."""


def index_named(name: str) -> RateIndex:
    """The index registered under ``name``.

    Args:
        name: A key of :data:`INDICES`, e.g. ``"TIIE_FONDEO"``.

    Returns:
        The index.

    Raises:
        UnsupportedConventionError: No index has that name.
    """
    try:
        return INDICES[name]
    except KeyError:
        raise UnsupportedConventionError(
            f"no rate index named {name!r}; defined: {', '.join(sorted(INDICES))}"
        ) from None


def term_sofr(months: int) -> RateIndex:
    """The Term SOFR index of a tenor in months.

    Args:
        months: 1, 3, 6 or 12.

    Returns:
        The index.

    Raises:
        UnsupportedConventionError: CME publishes no Term SOFR of that tenor.
    """
    for index in (TERM_SOFR_1M, TERM_SOFR_3M, TERM_SOFR_6M, TERM_SOFR_12M):
        if index.tenor.months == months:
            return index
    raise UnsupportedConventionError(
        f"no Term SOFR is published for {months} months; CME publishes 1, 3, 6 and 12"
    )


COLLATERAL_INDEX: dict[Currency, RateIndex] = {
    Currency.USD: SOFR,
    Currency.MXN: TIIE_FONDEO,
}
"""The overnight rate each currency's collateral is remunerated at."""


def collateral_index(currency: Currency) -> RateIndex:
    """The overnight index a currency's collateralised flows discount at.

    Args:
        currency: The currency of the flows.

    Returns:
        The index from :data:`COLLATERAL_INDEX`.

    Raises:
        UnsupportedConventionError: The currency has no collateral index,
            which means nothing can say what its flows discount at. Never a
            fallback to SOFR: that fallback is the bug this module removed.
    """
    try:
        return COLLATERAL_INDEX[currency]
    except KeyError:
        raise UnsupportedConventionError(
            f"no collateral index is defined for {currency.value}; add one to "
            "rates_engine.conventions.indices.COLLATERAL_INDEX before discounting "
            "its flows"
        ) from None
