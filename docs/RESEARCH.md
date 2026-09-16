# Where each formula came from, and whether it was read

Every number this package produces rests on a formula, and every formula came
from somewhere. This table says where, and — the column that matters — whether
the source was read directly or taken from something that cited it. A formula
transcribed from a secondary source is not wrong, but it is a different kind
of claim, and a reader deserves to know which one they are getting.

## Formulas in v1

| What | Where it lives | Source | Read? |
| --- | --- | --- | --- |
| SR1 settlement: arithmetic average of daily SOFR, ACT/360 | `instruments/futures.py` | CME contract definition, via the vault note *SOFR Futures — Pricing, Convexity and Hedging Swaps* | Secondary |
| SR3 settlement: daily compounded SOFR between IMM dates, ACT/360 | `instruments/futures.py` | Same | Secondary |
| Non-publication days repeat the last published rate | `market/snapshot.py` | CME and New York Fed rule, via the same note | Secondary |
| Ho-Lee convexity adjustment, `0.5 σ² T₁ T₂` | `convexity.py` | Hull, via *Forward vs Futures and the Eurodollar Convexity Bias* | Secondary |
| Hull-White convexity adjustment | `convexity.py` | Henrard, transcribed by Skov and Skovmand (eq. 74-75) | **Not read in Henrard** |
| Convexity material only past two years | `convexity.py`, tested qualitatively | Skov and Skovmand | Secondary |
| At-the-money calibration overstates the adjustment by 10-25% | Recorded in the evidence, applies from v2 | Romero, Bermúdez and Turfus | Secondary |
| Collateralised flows discount at the collateral rate | `pricing.py`, `curves/discount.py` | Fujii, Shimada and Takahashi; Piterbarg, via *Multi-Curve Framework and Collateral Discounting* | Secondary |
| Projection on the tenor curve, discounting on OIS | `instruments/swaps.py` | Mercurio; Bianchetti, via the same note | Secondary |
| The FRA rate is not the discount curve's forward under a basis | `instruments/fra.py` | Bianchetti | Secondary |
| Forward basis between tenor and OIS curves (eq. 20) | `curves/dual.py` | Bianchetti | Secondary |
| Sequential versus simultaneous dual-curve calibration | `curves/dual.py` | Ametrano and Bianchetti | Secondary |
| Four curve views and the no-arbitrage relations between them | `curves/views.py` | CFA Level I *Fixed Income Valuation*; CFA Level II *Term Structure and Arbitrage-Free Valuation* | Read |
| Hedging a swap with a futures strip; the convexity residual | `hedging.py` | CFA Level III *Derivatives — Swap Strategies*; CME/Rogerson 2025 whitepaper | Whitepaper **not read** |
| Key-rate shocks as a partition of unity | `risk.py` | Standard construction; the property is asserted directly rather than cited | Derived here |
| Duration conventions: money duration, PVBP, effective duration and convexity | `risk.py` | CFA Level I *Fixed Income* | Read |

## What "not read" costs, concretely

**The Hull-White adjustment.** Transcribed rather than derived, so the
protection against a transcription error is the limit test: as `kappa`
approaches zero the expression must converge to Ho-Lee, and
`tests/test_convexity.py` holds the two within 0.01 bp at `kappa = 1e-6`. A
sign error or a misplaced factor of two fails that test. A subtler error — a
wrong power of `kappa` that happens to vanish in the same limit — would not,
and reading Henrard 2018 is what would close it.

**The CME whitepaper.** Its published outputs are the only third-party numbers
this package is measured against: 779 contracts, a 3.3304% IMM par coupon,
+22,292 USD at minus a hundred basis points, and a DV01 moving from 19,480 to
19,921. The input curve is reconstructed from the SR3 prices the paper lists
rather than copied, which is why the tolerance is ±3% rather than exact.

Those tests are currently **skipped**. The session that wrote this package had
egress limited to package registries and GitHub, so the whitepaper could not
be fetched and neither could CME's published settlements. See
`tests/fixtures/cme_published/README.md`. Nothing was faked in their place: a
generated fixture would turn a test that proves something into a test that
proves the generator agrees with itself.

## What is validated against nothing external

The dual-curve solver. There is no free source of Term SOFR par rates or
OIS-versus-term basis spreads, so v1 proves the solver rather than a market
fit: the inputs are constructed with a known basis, and the tests assert
properties that hold for any basis plus an exact identity at zero basis. Every
such result declares `inputs_origin="synthetic"`, and asking for real quotes
raises rather than substituting something plausible.

## Assumptions carried in the code

| Assumption | Where | How it is marked |
| --- | --- | --- |
| SR1 contract notional is USD 5,000,000 | `instruments/futures.py` | `notional_source="assumed"`; the DV01 of 41.67 is derived from it, not quoted |
| SIFMA observes a Saturday holiday on the preceding Friday | `conventions/calendar.py` | The fixture's provenance records the manual check as **not done** |
| Treasury par yields are usable as a stand-in for OIS par | `curves/bootstrap.py` | Opt-in only; marks every node; a `Degradation` names the swap spread as an unquantified bias |

## Vault notes behind this package

*SOFR Futures — Pricing, Convexity and Hedging Swaps* ·
*Multi-Curve Framework and Collateral Discounting* ·
*Forward vs Futures and the Eurodollar Convexity Bias* ·
*LIBOR Transition, SOFR and Fallbacks* ·
*Term Structure Models for Swaps and Swaptions* ·
*Swap Spreads — Credit, Duration Demand and Limits to Arbitrage* ·
*Swap Hedging, Central Clearing and XVA* ·
*Forwards, Multi-Curve and Swaptions (Post-LIBOR)* ·
*CFA L2 Fixed Income — Term Structure and Arbitrage-Free Valuation* ·
*CFA L3 Derivatives — Swap Strategies*
