"""PRD-003 AC-3.2: a smile from three quotes that reproduces all three.

The criterion asks for exact reproduction of the quoted points, which is
what settles open question 2 in favour of vanna-volga over reusing v2's
SABR: SABR *fits* three points, vanna-volga *reproduces* them, because at a
pillar the weight vector collapses onto that one instrument.

`volatility_at` short-circuits at a pillar, so testing it there would test
the short circuit. The exactness claim is tested on the price instead —
`smile.price(K)` against Garman-Kohlhagen at the quoted volatility — which
is the thing the construction actually has to get right.
"""

from __future__ import annotations

import json
import math

import pytest

from rates_engine.errors import CalibrationError
from rates_engine.evidence import DataQuality
from rates_engine.fx import garman_kohlhagen as gk
from rates_engine.fx.delta import DeltaBasis, DeltaConvention, PremiumAdjustment
from rates_engine.fx.vannavolga import (
    NOT_ARBITRAGE_FREE,
    ATMConvention,
    SmileQuotes,
    VannaVolgaSmile,
)
from rates_engine.volatility.kinds import OptionKind

SPOT = 18.50
EXPIRY = 0.25
R_MXN = 0.0950
R_USD = 0.0420

QUOTES = SmileQuotes(
    atm=0.1150,
    risk_reversal_25=0.0180,
    butterfly_25=0.0035,
    risk_reversal_10=0.0320,
    butterfly_10=0.0110,
)
SPOT_UNADJUSTED = DeltaConvention(DeltaBasis.SPOT, PremiumAdjustment.UNADJUSTED)


def _smile(**kw) -> VannaVolgaSmile:
    args = {
        "spot": SPOT,
        "expiry": EXPIRY,
        "r_domestic": R_MXN,
        "r_foreign": R_USD,
        "quotes": QUOTES,
        "delta_convention": SPOT_UNADJUSTED,
        "atm_convention": ATMConvention.DELTA_NEUTRAL_STRADDLE,
    }
    args.update(kw)
    return VannaVolgaSmile(**args)


@pytest.fixture
def smile() -> VannaVolgaSmile:
    return _smile()


class TestTheQuotesArithmetic:
    """Risk reversal and butterfly to wing volatilities."""

    def test_the_twenty_five_delta_wings(self):
        put_vol, call_vol = QUOTES.wings(0.25)
        assert call_vol - put_vol == pytest.approx(QUOTES.risk_reversal_25)
        assert 0.5 * (call_vol + put_vol) - QUOTES.atm == pytest.approx(QUOTES.butterfly_25)

    def test_the_ten_delta_wings(self):
        put_vol, call_vol = QUOTES.wings(0.10)
        assert call_vol - put_vol == pytest.approx(QUOTES.risk_reversal_10)

    def test_a_positive_risk_reversal_puts_the_call_wing_higher(self):
        put_vol, call_vol = QUOTES.wings(0.25)
        assert call_vol > put_vol

    def test_an_absent_ten_delta_pair_refuses_rather_than_extrapolating(self):
        thin = SmileQuotes(atm=0.115, risk_reversal_25=0.018, butterfly_25=0.0035)
        with pytest.raises(CalibrationError, match="ten-delta"):
            thin.wings(0.10)

    def test_a_delta_the_market_does_not_quote_refuses(self):
        with pytest.raises(ValueError, match="0.25 and 0.10"):
            QUOTES.wings(0.40)

    def test_absent_wings_serialise_as_null_not_as_a_missing_key(self):
        thin = SmileQuotes(atm=0.115, risk_reversal_25=0.018, butterfly_25=0.0035)
        stored = thin.to_dict()
        assert stored["risk_reversal_10"] is None
        assert "butterfly_10" in stored


class TestExactReproductionOfThePillars:
    """AC-3.2, tested on the price rather than on the lookup short circuit."""

    @pytest.mark.parametrize("kind", list(OptionKind))
    def test_the_price_at_each_pillar_is_the_market_price(self, smile, kind):
        for strike, vol in smile.pillars():
            market = gk.price(SPOT, strike, EXPIRY, R_MXN, R_USD, vol, kind)
            assert smile.price(strike, kind) == pytest.approx(market, abs=1e-12)

    def test_the_correction_is_not_trivially_zero(self, smile):
        """If the construction were doing nothing, the pillar prices would
        match the flat-volatility prices too. They must not."""
        for strike, vol in smile.pillars():
            if vol == QUOTES.atm:
                continue
            flat = gk.price(SPOT, strike, EXPIRY, R_MXN, R_USD, QUOTES.atm)
            assert abs(smile.price(strike) - flat) > 1e-6

    def test_the_volatility_read_at_a_pillar_is_the_quote(self, smile):
        for strike, vol in smile.pillars():
            reading = smile.volatility_at(strike)
            assert reading.volatility == pytest.approx(vol, abs=1e-12)
            assert reading.source == "pillar"

    def test_a_strike_a_hair_off_a_pillar_still_reads_back_to_it(self, smile):
        """The exactness is in the construction, not in the short circuit:
        nudging off the pillar takes the interpolated path and must still
        land essentially on the quoted volatility."""
        strike, vol = smile.pillars()[0]
        reading = smile.volatility_at(strike * (1.0 + 1e-9))
        assert reading.source == "vanna_volga"
        assert reading.volatility == pytest.approx(vol, abs=1e-6)

    def test_there_are_exactly_three_pillars_in_strike_order(self, smile):
        pillars = smile.pillars()
        assert len(pillars) == 3
        assert [k for k, _ in pillars] == sorted(k for k, _ in pillars)


class TestTheShapeBetweenThePillars:
    """Interpolation has to be a smile, not just a number."""

    def test_it_stays_between_the_neighbouring_quotes(self, smile):
        pillars = smile.pillars()
        for (left_k, left_v), (right_k, right_v) in zip(pillars, pillars[1:], strict=False):
            mid = smile.volatility_at(0.5 * (left_k + right_k)).volatility
            assert min(left_v, right_v) - 1e-6 <= mid <= max(left_v, right_v) + 1e-6

    def test_the_skew_has_the_sign_of_the_risk_reversal(self, smile):
        low, _, high = smile.pillars()
        assert smile.volatility_at(high[0] * 0.99).volatility > smile.volatility_at(
            low[0] * 1.01
        ).volatility

    def test_it_is_continuous_across_the_atm_strike(self, smile):
        """The reading switches from a put to a call at the at-the-money
        strike; the volatility must not jump there."""
        atm = smile.atm_strike
        below = smile.volatility_at(atm * (1 - 1e-7)).volatility
        above = smile.volatility_at(atm * (1 + 1e-7)).volatility
        assert below == pytest.approx(above, abs=1e-7)

    def test_reading_the_same_strike_twice_is_bit_exact(self, smile):
        strike = 18.9
        assert smile.volatility_at(strike).volatility == smile.volatility_at(strike).volatility


class TestTheWingsAreFlaggedNotExtrapolated:
    """Vanna-volga is a hedging argument, and it says so past its pillars."""

    def test_past_the_top_pillar_it_holds_flat(self, smile):
        top_strike, top_vol = smile.pillars()[-1]
        reading = smile.volatility_at(top_strike * 1.3)
        assert reading.volatility == pytest.approx(top_vol)
        assert reading.source == "flat_wing"

    def test_past_the_bottom_pillar_it_holds_flat(self, smile):
        bottom_strike, bottom_vol = smile.pillars()[0]
        reading = smile.volatility_at(bottom_strike * 0.7)
        assert reading.volatility == pytest.approx(bottom_vol)

    def test_a_wing_reading_is_marked_unreliable(self, smile):
        reading = smile.volatility_at(smile.pillars()[-1][0] * 1.3)
        assert reading.reliable is False
        assert reading.evidence.worst_quality is DataQuality.ASSUMED

    def test_the_warning_names_the_construction_not_just_the_strike(self, smile):
        reading = smile.volatility_at(smile.pillars()[-1][0] * 1.3)
        codes = {w.code for w in reading.evidence.warnings}
        assert "outside_quoted_smile" in codes
        assert "negative density" in NOT_ARBITRAGE_FREE

    def test_an_inside_reading_is_reliable(self, smile):
        assert smile.volatility_at(smile.atm_strike).reliable is True
        assert smile.wing_is_reliable(smile.atm_strike)
        assert not smile.wing_is_reliable(smile.pillars()[-1][0] * 2)

    def test_a_non_positive_strike_refuses(self, smile):
        with pytest.raises(ValueError, match="lognormal"):
            smile.volatility_at(0.0)


class TestTheAtmConventionIsThreeDifferentStrikes:
    """The second ambiguity, treated like the first."""

    def test_the_three_conventions_give_three_strikes(self):
        strikes = {
            convention: _smile(atm_convention=convention).atm_strike
            for convention in ATMConvention
        }
        assert len(set(round(k, 10) for k in strikes.values())) == 3

    def test_forward_is_the_outright(self, smile):
        assert _smile(atm_convention=ATMConvention.FORWARD).atm_strike == pytest.approx(
            smile.forward
        )

    def test_spot_is_the_spot(self):
        assert _smile(atm_convention=ATMConvention.SPOT).atm_strike == SPOT

    def test_the_straddle_strike_sits_above_the_forward_when_unadjusted(self, smile):
        drift = math.exp(0.5 * QUOTES.atm**2 * EXPIRY)
        assert smile.atm_strike == pytest.approx(smile.forward * drift)
        assert smile.atm_strike > smile.forward

    def test_the_straddle_strike_sits_below_the_forward_when_premium_adjusted(self):
        adjusted = _smile(
            delta_convention=DeltaConvention(
                DeltaBasis.SPOT, PremiumAdjustment.PREMIUM_ADJUSTED
            )
        )
        assert adjusted.atm_strike < adjusted.forward

    def test_the_convention_changes_the_whole_smile_not_just_one_point(self):
        straddle = _smile(atm_convention=ATMConvention.DELTA_NEUTRAL_STRADDLE)
        forward = _smile(atm_convention=ATMConvention.FORWARD)
        probe = 19.0
        assert straddle.volatility_at(probe).volatility != forward.volatility_at(
            probe
        ).volatility

    def test_there_is_no_default_convention(self):
        with pytest.raises(TypeError):
            VannaVolgaSmile(  # type: ignore[call-arg]
                spot=SPOT, expiry=EXPIRY, r_domestic=R_MXN, r_foreign=R_USD, quotes=QUOTES
            )


class TestParitySurvivesTheCorrection:
    """The correction is the same for a call and a put, so it must."""

    @pytest.mark.parametrize("strike", [18.2, 18.7778, 19.4])
    def test_put_call_parity_holds_on_the_smile(self, smile, strike):
        left = smile.price(strike, OptionKind.CALL) - smile.price(strike, OptionKind.PUT)
        right = SPOT * math.exp(-R_USD * EXPIRY) - strike * math.exp(-R_MXN * EXPIRY)
        assert left == pytest.approx(right, abs=1e-10)


class TestConstructionRefusals:
    """Inputs that are not a smile."""

    def test_a_non_positive_expiry_refuses(self):
        with pytest.raises(ValueError, match="positive expiry"):
            _smile(expiry=0.0)

    def test_a_non_positive_atm_refuses(self):
        with pytest.raises(ValueError, match="at-the-money volatility"):
            _smile(quotes=SmileQuotes(atm=0.0, risk_reversal_25=0.0, butterfly_25=0.0))

    def test_a_butterfly_that_sinks_a_wing_below_zero_refuses(self):
        broken = SmileQuotes(atm=0.10, risk_reversal_25=0.30, butterfly_25=-0.02)
        with pytest.raises(CalibrationError, match="non-positive wing"):
            _smile(quotes=broken)


class TestSerialisation:
    """Every convention that gives the smile meaning travels with it."""

    def test_the_smile_states_both_conventions(self, smile):
        stored = smile.to_dict()
        assert stored["delta_convention"] == "spot unadjusted"
        assert stored["atm_convention"] == "delta_neutral_straddle"

    def test_it_carries_the_caveat(self, smile):
        assert "not arbitrage" in smile.to_dict()["caveat"].lower() or "hedging argument" in (
            smile.to_dict()["caveat"]
        )

    def test_it_lists_the_pillars_it_was_built_from(self, smile):
        stored = smile.to_dict()
        assert len(stored["pillars"]) == 3
        assert all({"strike", "volatility"} == set(p) for p in stored["pillars"])

    def test_a_reading_is_json_serialisable(self, smile):
        json.dumps(smile.volatility_at(19.0).to_dict())

    def test_a_reading_names_its_source_and_reliability(self, smile):
        payload = smile.volatility_at(19.0).payload_fields()
        assert payload["source"] == "vanna_volga"
        assert payload["reliable"] is True
        assert payload["caveat"] == NOT_ARBITRAGE_FREE

    def test_the_evidence_records_the_pillars_used(self, smile):
        fields = smile.volatility_at(19.0).evidence.fields
        assert len(fields["pillars"]) == 3
        assert fields["quotes"]["atm"] == QUOTES.atm

    def test_the_smile_is_json_serialisable(self, smile):
        json.dumps(smile.to_dict())
