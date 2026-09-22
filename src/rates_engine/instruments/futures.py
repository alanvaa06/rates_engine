"""SOFR futures: one-month average and three-month compounded.

The two contracts settle on different functions of the same fixings, and the
difference is not cosmetic. SR1 averages the daily rates arithmetically over a
calendar month; SR3 compounds them between IMM dates. At 5% overnight the
compounded rate exceeds the average by roughly a tenth of a basis point over
three months — larger than the 0.1 bp tolerance the settlement tests hold, so
using one formula for both contracts fails loudly rather than quietly.

**Contract size and DV01.** Both are derived from notional and tenor rather
than quoted from a table, so the arithmetic is visible:

* SR3 — 1,000,000 x 0.25 x 0.0001 = USD 25.00 per basis point.
* SR1 — 5,000,000 x 30/360 x 0.0001 = USD 41.67 per basis point.

Both use the contract's *nominal* tenor, not the realised accrual of the
period: an IMM quarter is 91 or 98 days and a calendar month is 28 to 31,
but the tick value does not move with them.

The SR1 notional of USD 5,000,000 is assumed rather than read from the CME
contract spec, and :attr:`SOFRFuture1M.notional_source` says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from rates_engine.conventions.schedule import imm_date, next_imm_on_or_after
from rates_engine.market.snapshot import CompoundedRate, MarketSnapshot

__all__ = [
    "SOFRFuture1M",
    "SOFRFuture3M",
    "SR3_NOTIONAL",
    "SR1_NOTIONAL",
    "SR3_CONTRACT_TENOR",
    "SR1_CONTRACT_TENOR",
    "BASIS_POINT",
]

BASIS_POINT = 1e-4
"""One basis point as a decimal rate."""

SR3_NOTIONAL = 1_000_000.0
"""SR3 contract notional in USD."""

SR1_NOTIONAL = 5_000_000.0
"""SR1 contract notional in USD. Assumed; see the module docstring."""

SR3_CONTRACT_TENOR = 0.25
"""SR3 contract tenor for DV01, in years. Nominal, not the realised accrual."""

SR1_CONTRACT_TENOR = 30.0 / 360.0
"""SR1 contract tenor for DV01, in years. Nominal 30/360, not the realised accrual."""


def _month_bounds(contract_month: date) -> tuple[date, date]:
    """First day of the contract month and first day of the next, as a half-open range."""
    start = date(contract_month.year, contract_month.month, 1)
    end = date(start.year + start.month // 12, start.month % 12 + 1, 1)
    return start, end


@dataclass(frozen=True)
class SOFRFuture1M:
    """A one-month SOFR future, settling on the arithmetic average of the month.

    Attributes:
        contract_month: Any date inside the contract month; only year and month
            are used.
    """

    contract_month: date

    notional: float = SR1_NOTIONAL
    notional_source: str = "assumed"
    """``"assumed"``: the USD 5,000,000 notional has not been read from the CME spec."""

    @property
    def accrual_start(self) -> date:
        """First calendar day of the contract month."""
        return _month_bounds(self.contract_month)[0]

    @property
    def accrual_end(self) -> date:
        """First calendar day of the following month, exclusive."""
        return _month_bounds(self.contract_month)[1]

    @property
    def year_fraction(self) -> float:
        """Contract period in years, ACT/360."""
        return (self.accrual_end - self.accrual_start).days / 360.0

    @property
    def dv01(self) -> float:
        """Value of one basis point in USD: notional x nominal 30/360 tenor x 1bp.

        The *nominal* tenor, not :attr:`year_fraction`. The tick value is
        defined on 30/360 whatever the calendar month contains, so a March
        contract and a February one are worth the same per basis point even
        though they accrue over 31 and 28 days.
        """
        return self.notional * SR1_CONTRACT_TENOR * BASIS_POINT

    def settlement_rate(self, snapshot: MarketSnapshot, *, series_id: str = "SOFR") -> CompoundedRate:
        """The arithmetic average rate the contract settles on.

        Args:
            snapshot: Snapshot holding the overnight fixings.
            series_id: Overnight series to average.

        Returns:
            The :class:`~rates_engine.market.snapshot.CompoundedRate`, whose
            ``rate`` is the settlement rate as a decimal.

        Raises:
            MissingFixingError: A business day in the month has no fixing.
        """
        return snapshot.averaged(series_id, self.accrual_start, self.accrual_end)

    def settlement_price(self, snapshot: MarketSnapshot, *, series_id: str = "SOFR") -> float:
        """Final settlement price, ``100 - average rate in percent``.

        Args:
            snapshot: Snapshot holding the overnight fixings.
            series_id: Overnight series to average.

        Returns:
            The price in the contract's own quotation, e.g. ``94.69``.

        Raises:
            MissingFixingError: A business day in the month has no fixing.
        """
        return 100.0 - 100.0 * self.settlement_rate(snapshot, series_id=series_id).rate


@dataclass(frozen=True)
class SOFRFuture3M:
    """A three-month SOFR future, settling on the rate compounded between IMM dates.

    Attributes:
        imm_start: Third Wednesday that starts the reference period.
        imm_end: Third Wednesday that ends it, exclusive.

    The constructor takes dates rather than a contract month on purpose: a
    contract month tells you which IMM quarter, not which two Wednesdays, and
    the settlement is a function of the Wednesdays.
    """

    imm_start: date
    imm_end: date

    notional: float = SR3_NOTIONAL
    notional_source: str = "derived"
    """``"derived"``: the USD 1,000,000 notional follows from the published tick value."""

    @classmethod
    def from_contract_month(cls, year: int, month: int) -> SOFRFuture3M:
        """Build the contract whose reference period starts in a given IMM month.

        Args:
            year: Contract year.
            month: IMM month — 3, 6, 9 or 12.

        Returns:
            The contract spanning that IMM date to the next one.

        Raises:
            ValueError: ``month`` is not an IMM month.
        """
        start = imm_date(year, month)
        return cls(start, next_imm_on_or_after(start + timedelta(days=1)))

    @property
    def year_fraction(self) -> float:
        """Reference period in years, ACT/360."""
        return (self.imm_end - self.imm_start).days / 360.0

    @property
    def dv01(self) -> float:
        """Value of one basis point in USD: notional x nominal 0.25 tenor x 1bp.

        The *nominal* quarter, not :attr:`year_fraction`. An IMM quarter runs
        91 or 98 calendar days; the contract is worth USD 25.00 per basis
        point either way, and using the realised accrual here would put a one
        percent error into every hedge ratio.
        """
        return self.notional * SR3_CONTRACT_TENOR * BASIS_POINT

    def settlement_rate(self, snapshot: MarketSnapshot, *, series_id: str = "SOFR") -> CompoundedRate:
        """The compounded rate the contract settles on.

        Args:
            snapshot: Snapshot holding the overnight fixings.
            series_id: Overnight series to compound.

        Returns:
            The :class:`~rates_engine.market.snapshot.CompoundedRate`.

        Raises:
            MissingFixingError: A business day in the period has no fixing.
        """
        return snapshot.compounded(series_id, self.imm_start, self.imm_end)

    def settlement_price(self, snapshot: MarketSnapshot, *, series_id: str = "SOFR") -> float:
        """Final settlement price, ``100 - compounded rate in percent``.

        Args:
            snapshot: Snapshot holding the overnight fixings.
            series_id: Overnight series to compound.

        Returns:
            The price in the contract's own quotation.

        Raises:
            MissingFixingError: A business day in the period has no fixing.
        """
        return 100.0 - 100.0 * self.settlement_rate(snapshot, series_id=series_id).rate

    def implied_forward_rate(self, price: float, convexity_adjustment: float = 0.0) -> float:
        """Forward rate implied by a price, net of the convexity adjustment.

        Args:
            price: Quoted futures price.
            convexity_adjustment: Futures rate minus forward rate, as a decimal.
                Subtracted, because the futures rate sits above the forward.

        Returns:
            The forward rate as a decimal, on the contract's ACT/360 period.
        """
        return (100.0 - price) / 100.0 - convexity_adjustment
