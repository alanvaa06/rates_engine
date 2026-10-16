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

from rates_engine.conventions.option_kind import OptionKind
from rates_engine.core.errors import CalibrationError
from rates_engine.core.evidence import DataQuality
from rates_engine.models import garman_kohlhagen as gk
from rates_engine.models.fx_delta import DeltaBasis, DeltaConvention, PremiumAdjustment
from rates_engine.volatility.fx_smile import (
    NOT_ARBITRAGE_FREE,
    ATMConvention,
    SmileQuotes,
    VannaVolgaSmile,
)

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
    """PRD-003 AC-3.2, tested on the price rather than on the lookup short circuit."""

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


def _reference_volatility(strike: float, kind: OptionKind) -> float:
    """Vanna-volga computed here, from the definition, calling no package code.

    The package's smile is checked against this. Written out rather than
    imported because a check that calls the same helpers as the thing it
    checks is a check that they agree with themselves — the same reason v1
    reimplements SR1 and SR3 settlement from the contract definition rather
    than reusing the engine's own accrual.
    """
    cdf = lambda x: 0.5 * math.erfc(-x / math.sqrt(2.0))  # noqa: E731
    pdf = lambda x: math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)  # noqa: E731

    def black(k: float, vol: float) -> float:
        d1 = (math.log(SPOT / k) + (R_MXN - R_USD + 0.5 * vol * vol) * EXPIRY) / (
            vol * math.sqrt(EXPIRY)
        )
        d2 = d1 - vol * math.sqrt(EXPIRY)
        if kind is OptionKind.CALL:
            return SPOT * math.exp(-R_USD * EXPIRY) * cdf(d1) - k * math.exp(
                -R_MXN * EXPIRY
            ) * cdf(d2)
        return k * math.exp(-R_MXN * EXPIRY) * cdf(-d2) - SPOT * math.exp(
            -R_USD * EXPIRY
        ) * cdf(-d1)

    def greeks(k: float) -> tuple[float, float, float]:
        vol = QUOTES.atm
        d1 = (math.log(SPOT / k) + (R_MXN - R_USD + 0.5 * vol * vol) * EXPIRY) / (
            vol * math.sqrt(EXPIRY)
        )
        d2 = d1 - vol * math.sqrt(EXPIRY)
        vega = SPOT * math.exp(-R_USD * EXPIRY) * pdf(d1) * math.sqrt(EXPIRY)
        return vega, -math.exp(-R_USD * EXPIRY) * pdf(d1) * d2 / vol, vega * d1 * d2 / vol

    pillars = list(_smile().pillars())
    columns = [greeks(k) for k, _ in pillars]
    rows = [[columns[j][i] for j in range(3)] for i in range(3)]
    target = list(greeks(strike))
    augmented = [row[:] + [target[i]] for i, row in enumerate(rows)]
    for i in range(3):
        pivot = max(range(i, 3), key=lambda r: abs(augmented[r][i]))
        augmented[i], augmented[pivot] = augmented[pivot], augmented[i]
        for r in range(3):
            if r == i:
                continue
            factor = augmented[r][i] / augmented[i][i]
            for c in range(i, 4):
                augmented[r][c] -= factor * augmented[i][c]
    weights = [augmented[i][3] / augmented[i][i] for i in range(3)]

    premium = black(strike, QUOTES.atm) + sum(
        weights[i] * (black(k, v) - black(k, QUOTES.atm))
        for i, (k, v) in enumerate(pillars)
    )
    low, high = 1e-6, 5.0
    for _ in range(200):
        mid = 0.5 * (low + high)
        if black(strike, mid) < premium:
            low = mid
        else:
            high = mid
    return 0.5 * (low + high)


class TestAgainstAnIndependentImplementation:
    """The test the pillar check is not.

    Reproducing the three pillars exactly is arithmetically forced: at a
    pillar the target's greek row *is* that pillar's row, so the solve
    returns the indicator vector whatever the rows contain. Replacing the
    vega/vanna/volga basis with `[K, K², K³]` leaves every other test in
    this file passing while moving the interpolated volatility by four to
    eleven basis points. These are the tests that catch that.
    """

    @pytest.mark.parametrize("strike", [18.2, 18.4, 18.6, 18.8, 19.0, 19.2, 19.4])
    def test_the_interpolated_volatility_matches_the_reference(self, smile, strike):
        kind = OptionKind.CALL if strike >= smile.atm_strike else OptionKind.PUT
        assert smile.volatility_at(strike).volatility == pytest.approx(
            _reference_volatility(strike, kind), abs=1e-9
        )

    def test_the_weights_hedge_vega_vanna_and_volga(self, smile):
        """The defining equation. This is what makes the construction
        vanna-volga rather than any interpolation through three points."""
        strike = 19.0
        weights = smile._weights(strike)
        base = QUOTES.atm
        args = (EXPIRY, R_MXN, R_USD, base)
        for greek in (gk.vega, gk.vanna, gk.volga):
            hedged = sum(
                w * greek(SPOT, k, *args)
                for w, (k, _) in zip(weights, smile.pillars(), strict=True)
            )
            assert hedged == pytest.approx(greek(SPOT, strike, *args), rel=1e-10)

    def test_a_polynomial_basis_would_be_caught(self, smile):
        """Proof the tests above have teeth: the same construction on a
        `[K, K², K³]` basis reproduces the pillars just as exactly and gives
        a visibly different smile between them."""
        pillars = list(smile.pillars())
        strike = 19.0
        rows = [[k**power for k, _ in pillars] for power in (1, 2, 3)]
        target = [strike**power for power in (1, 2, 3)]
        augmented = [row[:] + [target[i]] for i, row in enumerate(rows)]
        for i in range(3):
            pivot = max(range(i, 3), key=lambda r: abs(augmented[r][i]))
            augmented[i], augmented[pivot] = augmented[pivot], augmented[i]
            for r in range(3):
                if r == i:
                    continue
                factor = augmented[r][i] / augmented[i][i]
                for c in range(i, 4):
                    augmented[r][c] -= factor * augmented[i][c]
        bogus = [augmented[i][3] / augmented[i][i] for i in range(3)]

        real = smile._weights(strike)
        gap = max(abs(a - b) for a, b in zip(real, bogus, strict=True))
        assert gap > 1e-3, "the two bases agree, so this file cannot tell them apart"


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
    """Vanna-volga is a hedging argument, and it says so past its pillars.

    "Its pillars" are the outermost quoted ones: the ten-delta pair when the
    quotes have it, as `QUOTES` does. These two tests used the 25-delta
    pillars, and passed only because the ten-delta quotes were ignored.
    """

    def test_past_the_top_pillar_it_holds_flat(self, smile):
        top_strike, top_vol = smile.all_pillars()[-1]
        assert (top_strike, top_vol) == smile.wing_pillars()[-1]
        reading = smile.volatility_at(top_strike * 1.3)
        assert reading.volatility == pytest.approx(top_vol)
        assert reading.source == "flat_wing"

    def test_past_the_bottom_pillar_it_holds_flat(self, smile):
        bottom_strike, bottom_vol = smile.all_pillars()[0]
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


class TestTheInversionDoesNotClampInSilence:
    """`_implied` bisected inside `[1e-6, 5.0]` and returned whichever end it
    reached, so a premium worth more than a 500% volatility read back as
    500% (`REVISION-rates_engine-2026-10-15-repro/options/09_vanna_volga.py`
    gave 4.99999999... for a premium priced at 700%). Outside the bracket
    no volatility in it reproduces the premium, and that is an error."""

    def test_a_premium_above_the_top_of_the_bracket_refuses(self, smile):
        premium = gk.price(SPOT, 19.0, EXPIRY, R_MXN, R_USD, 7.0, OptionKind.CALL)
        with pytest.raises(CalibrationError, match="500%"):
            smile._implied(19.0, premium, OptionKind.CALL)

    def test_a_premium_below_the_bottom_of_the_bracket_refuses(self, smile):
        intrinsic = gk.price(SPOT, 18.0, EXPIRY, R_MXN, R_USD, 0.0, OptionKind.CALL)
        with pytest.raises(CalibrationError, match="premium"):
            smile._implied(18.0, 0.5 * intrinsic, OptionKind.CALL)

    def test_a_premium_inside_it_inverts_exactly(self, smile):
        premium = gk.price(SPOT, 19.0, EXPIRY, R_MXN, R_USD, 0.137, OptionKind.CALL)
        assert smile._implied(19.0, premium, OptionKind.CALL) == pytest.approx(0.137, abs=1e-12)


class TestTheTenDeltaQuotesAreUsed:
    """The review's finding 3.11: `SmileQuotes` took a ten-delta risk reversal
    and butterfly, and the smile ignored them.

    PRD-003 AC-3.2 asks for the volatility at ten delta. Built only on the
    25-delta pillars, the smile read 15.10% at the ten-delta call strike of
    a quote set whose ten-delta call is 18.00%, and was identical with and
    without the ten-delta numbers (`REVISION-rates_engine-2026-10-15-repro/
    options/09_vanna_volga.py`). It now builds vanna-volga piecewise: the
    25-delta triple between the 25-delta strikes, exactly as before, and
    `(10P, 25P, ATM)` and `(ATM, 25C, 10C)` in the two wings out to the
    ten-delta strikes, flat and unreliable past them.

    The pillar strikes below are QuantLib 1.44's,
    `ql.BlackDeltaCalculator(kind, ql.DeltaVolQuote.Spot, S, e^{-r_d T},
    e^{-r_f T}, vol sqrt(T)).strikeFromDelta(delta)` at each wing's own
    volatility, not this package's.
    """

    QUANTLIB = {  # (strike, quoted volatility), spot unadjusted delta, this file's market
        "10P": (17.50308981619162, 0.1100),
        "25P": (18.102401018159345, 0.1095),
        "25C": (19.59988253905741, 0.1275),
        "10C": (20.575601889840065, 0.1420),
    }

    def test_the_wing_pillars_are_quantlibs_strikes_at_the_quoted_vols(self, smile):
        # 1e-9 rather than tighter: QuantLib inverts the normal with Acklam's
        # approximation, unrefined, which is good to about 1e-9; the package
        # uses scipy's ndtri. The two strikes differ by 8e-11 relative.
        (k10p, v10p), (k10c, v10c) = smile.wing_pillars()
        assert k10p == pytest.approx(self.QUANTLIB["10P"][0], rel=1e-9)
        assert k10c == pytest.approx(self.QUANTLIB["10C"][0], rel=1e-9)
        assert v10p == pytest.approx(self.QUANTLIB["10P"][1], abs=1e-15)
        assert v10c == pytest.approx(self.QUANTLIB["10C"][1], abs=1e-15)

    @pytest.mark.parametrize("pillar", ["10P", "25P", "25C", "10C"])
    def test_the_smile_reads_the_quote_at_each_quantlib_strike(self, smile, pillar):
        strike, quoted = self.QUANTLIB[pillar]
        assert smile.volatility_at(strike).volatility == pytest.approx(quoted, abs=1e-9)

    @pytest.mark.parametrize("kind", list(OptionKind))
    def test_all_five_pillars_reprice_exactly(self, smile, kind):
        """On the price, not the lookup short circuit, as for the 25-delta three."""
        assert len(smile.all_pillars()) == 5
        for strike, vol in smile.all_pillars():
            market = gk.price(SPOT, strike, EXPIRY, R_MXN, R_USD, vol, kind)
            assert smile.price(strike, kind) == pytest.approx(market, abs=1e-12)

    def test_the_reviews_case_reads_eighteen_percent_not_fifteen(self):
        """S 18.50, T 1, r 9.5% and 4.2%, spot premium-adjusted delta, ATM
        13%, RR25 3%, BF25 0.6%, RR10 6%, BF10 2%: the ten-delta call is
        quoted at 18.00% and QuantLib puts its strike at 24.641237726504414."""
        smile = VannaVolgaSmile(
            18.50, 1.0, 0.095, 0.042,
            SmileQuotes(0.13, 0.030, 0.006, 0.060, 0.020),
            DeltaConvention(DeltaBasis.SPOT, PremiumAdjustment.PREMIUM_ADJUSTED),
            ATMConvention.DELTA_NEUTRAL_STRADDLE,
        )
        reading = smile.volatility_at(24.641237726504414)
        assert reading.volatility == pytest.approx(0.18, abs=1e-9)
        assert reading.reliable is True
        put = smile.volatility_at(16.828969828751596)
        assert put.volatility == pytest.approx(0.12, abs=1e-9)

    def test_between_the_two_deltas_it_is_interpolated_and_reliable(self, smile):
        for (left_k, left_v), (right_k, right_v) in (
            (smile.wing_pillars()[0], smile.pillars()[0]),
            (smile.pillars()[-1], smile.wing_pillars()[-1]),
        ):
            reading = smile.volatility_at(0.5 * (left_k + right_k))
            assert reading.source == "vanna_volga"
            assert reading.reliable is True
            assert reading.evidence.warnings == ()
            assert min(left_v, right_v) - 2e-3 <= reading.volatility <= max(left_v, right_v) + 2e-3

    def test_it_is_continuous_where_the_wings_meet_the_core(self, smile):
        for strike, vol in (smile.pillars()[0], smile.pillars()[-1]):
            below = smile.volatility_at(strike * (1 - 1e-7)).volatility
            above = smile.volatility_at(strike * (1 + 1e-7)).volatility
            assert below == pytest.approx(vol, abs=1e-6)
            assert above == pytest.approx(vol, abs=1e-6)

    def test_inside_the_25_delta_strikes_nothing_changed(self, smile):
        thin = _smile(quotes=SmileQuotes(QUOTES.atm, QUOTES.risk_reversal_25, QUOTES.butterfly_25))
        for strike in (18.2, 18.5, 18.8, 19.1, 19.4):
            assert smile.volatility_at(strike).volatility == thin.volatility_at(strike).volatility

    def test_past_the_ten_delta_strikes_it_is_flat_and_flagged(self, smile):
        for strike, vol in (smile.all_pillars()[0], smile.all_pillars()[-1]):
            factor = 0.9 if strike < SPOT else 1.1
            reading = smile.volatility_at(strike * factor)
            assert reading.volatility == vol
            assert reading.source == "flat_wing"
            assert reading.reliable is False
            assert reading.evidence.worst_quality is DataQuality.ASSUMED

    def test_without_ten_delta_quotes_the_outer_pillars_are_the_25_delta(self):
        thin = _smile(quotes=SmileQuotes(QUOTES.atm, QUOTES.risk_reversal_25, QUOTES.butterfly_25))
        assert thin.wing_pillars() == ()
        assert thin.all_pillars() == thin.pillars()
        top_strike, top_vol = thin.pillars()[-1]
        reading = thin.volatility_at(top_strike * 1.01)
        assert (reading.volatility, reading.source, reading.reliable) == (top_vol, "flat_wing", False)

    @pytest.mark.parametrize(
        "quotes",
        [
            SmileQuotes(0.115, 0.018, 0.0035, risk_reversal_10=0.032),
            SmileQuotes(0.115, 0.018, 0.0035, butterfly_10=0.011),
        ],
    )
    def test_half_a_ten_delta_pair_refuses(self, quotes):
        with pytest.raises(CalibrationError, match="ten-delta"):
            _smile(quotes=quotes)

    def test_a_ten_delta_wing_inside_the_25_delta_one_refuses(self):
        """A ten-delta volatility so far below the 25-delta one that its
        strike lands inside the 25-delta strike is not a smile."""
        broken = SmileQuotes(0.115, 0.018, 0.0035, risk_reversal_10=0.0, butterfly_10=-0.09)
        with pytest.raises(CalibrationError, match="ten-delta"):
            _smile(quotes=broken).wing_pillars()

    def test_the_payload_lists_the_wing_pillars(self, smile):
        stored = smile.to_dict()
        assert len(stored["wing_pillars"]) == 2
        fields = smile.volatility_at(19.0).evidence.fields
        assert len(fields["wing_pillars"]) == 2
