"""A magnitude stated in a docstring is a claim, so it is checked like one.

v0.3's ``instruments/futures.py`` said SR3's compounded rate exceeds SR1's
average by "roughly a tenth of a basis point" over three months at 5%. The
package itself computes about three basis points, thirty times that. Prose
that quotes a number is read by people sizing tolerances, so the number is
read back here from the docstring and compared with the code and with a
closed form.
"""

from __future__ import annotations

import re
from datetime import date

import pytest

import rates_engine.instruments.futures as futures_module
from rates_engine.conventions.calendar import SIFMA_US
from rates_engine.core.evidence import DataQuality, Provenance
from rates_engine.instruments.futures import SOFRFuture3M
from rates_engine.market.snapshot import MarketSnapshot, Series

FLAT_RATE = 0.05


def _stated_gap_bp() -> float:
    prose = " ".join((futures_module.__doc__ or "").split())
    match = re.search(r"([0-9]+\.[0-9]+) bp across the IMM quarter", prose)
    assert match, "instruments/futures.py no longer states the compounded-average gap"
    return float(match.group(1))


def _flat_snapshot() -> MarketSnapshot:
    provenance = Provenance(
        source="synthetic",
        series_id="SOFR",
        instrument_kind="overnight_fixing",
        data_quality=DataQuality.SYNTHETIC,
    )
    days = SIFMA_US.business_days(date(2026, 1, 2), date(2026, 12, 31))
    series = Series("SOFR", tuple(days), tuple(FLAT_RATE for _ in days), provenance)
    return MarketSnapshot(date(2026, 12, 31), {"SOFR": series})


def test_the_stated_gap_is_the_computed_one():
    contract = SOFRFuture3M.from_contract_month(2026, 3)
    snapshot = _flat_snapshot()
    compounded = snapshot.compounded("SOFR", contract.imm_start, contract.imm_end).rate
    averaged = snapshot.averaged("SOFR", contract.imm_start, contract.imm_end).rate
    # 0.05 bp absorbs the difference between compounding a weekend as one
    # three-day accrual and as three one-day ones, which is 0.04 bp here.
    assert (compounded - averaged) * 1e4 == pytest.approx(_stated_gap_bp(), abs=0.05)


def test_the_stated_gap_is_close_to_the_closed_form():
    # Continuous compounding over T years exceeds the average by r**2 * T / 2
    # to first order: 3.16 bp at 5% over the 91-day quarter.
    year_fraction = (date(2026, 6, 17) - date(2026, 3, 18)).days / 360.0
    closed_form_bp = FLAT_RATE**2 * year_fraction / 2.0 * 1e4
    assert _stated_gap_bp() == pytest.approx(closed_form_bp, abs=0.1)


def test_the_gap_is_far_outside_the_settlement_tolerance():
    # The reason the docstring gives for the number: using one formula for
    # both contracts must fail the 0.1 bp settlement tolerance loudly.
    assert _stated_gap_bp() > 10 * 0.1
