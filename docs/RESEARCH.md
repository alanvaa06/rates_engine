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

## Formulas added in v2

| What | Where it lives | Source | Read? |
| --- | --- | --- | --- |
| Bachelier (normal) option value, `A[(F-K)N(d) + σ√T n(d)]` | `volatility/bachelier.py` | Standard; the vault note *Volatility, Greeks and Option Strategy Practice (2026)* states it in this form | Secondary |
| Black (lognormal) option value on a forward | `volatility/black.py` | Black 1976, via *CFA L2 Derivatives — Pricing and Valuation* ·
*CFA Economics — Currency Exchange Rates* ·
*CFA L3 Derivatives — Forwards, Futures, and Options* ·
*CFA L3 Asset Allocation — Constraints, Currency, and Benchmarks* | Secondary |
| The annuity is the numeraire that makes the forward swap rate a martingale | `optionpricing.py` | *Forwards, Multi-Curve and Swaptions (Post-LIBOR)*; Mercurio | Secondary |
| SABR implied lognormal volatility, Hagan et al. (2002) eq. (2.17a) | `volatility/sabr.py` | Hagan, Kumar, Lesniewski and Woodward, via *Stochastic, Local and Rough Volatility Models* | **Not read in Hagan** |
| SABR at-the-money expansion, eq. (2.18) | `volatility/sabr.py` | Same | **Not read in Hagan** |
| SABR implied normal volatility, eq. (A.59a) | `volatility/sabr.py` | Same | **Not read in Hagan** |
| β fixed rather than calibrated, at a market convention | `volatility/sabr.py` | *Stochastic, Local and Rough Volatility Models*, on the β/ρ identification problem | Secondary |
| Breeden-Litzenberger: the second strike derivative of the call price is the density | `volatility/sabr.py` diagnostics | Breeden and Litzenberger 1978; standard | Secondary |
| Nelson-Siegel zero curve and its three loadings | `curves/parametric.py` | Nelson and Siegel 1987, via *Term Structure Models for Swaps and Swaptions* | Secondary |
| Concentrated least squares: betas linear given tau | `curves/parametric.py` | Standard result; the reason is argued in the module rather than cited | Derived here |
| Piecewise-constant policy path fitted to the futures strip | `curves/parametric.py` | Heitfield and Park, via *SOFR Futures — Pricing, Convexity and Hedging Swaps* | Secondary |
| Monotone convex interpolation, the four regions and the step-2 collar | `curves/interpolation.py` | Hagan and West 2006, *Interpolation Methods for Curve Construction* | **Not read in Hagan-West** |

## Formulas added in v3

| What | Where it lives | Source | Read? |
| --- | --- | --- | --- |
| Garman-Kohlhagen: Black-Scholes with the foreign rate as a yield | `fx/garman_kohlhagen.py` | Garman and Kohlhagen 1983; standard, and derivable from Black-Scholes in a line | Secondary |
| Covered interest parity, `F = S P_f / P_d` | `fx/forward.py` | *CFA Economics — Currency Exchange Rates*; an arbitrage relation rather than a model | Read |
| CIP has not held since 2007; the residual is the cross-currency basis | `fx/forward.py` | *Forwards, Multi-Curve and Swaptions (Post-LIBOR)* | Secondary |
| The four FX delta conventions and the premium adjustment | `fx/delta.py` | *Volatility, Greeks and Option Strategy Practice (2026)*, on the units-and-conventions traps | Secondary |
| Delta-neutral straddle strike, `F exp(±σ²T/2)` | `fx/vannavolga.py` | Standard; the sign follows from the premium adjustment and is derived in the module | Derived here |
| Vanna-volga: price at ATM plus the vega, vanna and volga the flat price fails to hedge | `fx/vannavolga.py` | Castagna and Mercurio, via *Forwards, Multi-Curve and Swaptions (Post-LIBOR)* | **Not read in Castagna-Mercurio** |
| Hedge structures and the cost-versus-protection trade-off | `hedging_structures.py` | CFA Level III Reading 19; *CFA L3 Derivatives — Forwards, Futures, and Options* | Read |
| `σ²(RDC) = σ²(RFC) + σ²(RFX) + 2ρσσ` | `hedging_structures.py` | *CFA L3 Asset Allocation — Constraints, Currency, and Benchmarks* | Read |
| The Mexican holiday calendar, thirteen rules | `conventions/calendar.py` | QuantLib `ql/time/calendars/mexico.cpp`, **read directly** | Read — but it is the **BMV** calendar, not Banxico's |
| TIIE day count, coupon period, benchmark distinction | `curves/mxn.py` | **Nothing reachable.** Recorded in `UNRESOLVED_MXN` | **Not established** |

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

**Hagan's SABR expansion.** Transcribed from a secondary source, so the
protection is the set of limits the formula must satisfy and a check against
the process it claims to approximate. With `nu = 0` and `beta = 1` it must
collapse to a constant lognormal volatility; with `beta = 0` the normal
expansion must collapse to `alpha`; the at-the-money form must agree with the
general form as the strike approaches the forward, from both sides. Above
those, `tests/test_sabr.py` runs a Monte Carlo of the SABR stochastic
differential equation and compares the simulated implied volatility with the
expansion, which is the only check here that would catch an error the limits
and the reference both share. Reading Hagan 2002 directly is what would close
the rest.

That comparison also bounds where the expansion stops being usable, which is
why `ExpansionBreakdownError` exists: the `O(nu² T)` correction can drive the
expansion negative at long expiry and high vol of vol, and a negative
volatility prices nothing.

**Hagan-West.** The four regions and the collar are transcribed. The
protection is the property the paper is about: positive inputs must give
positive interpolated forwards, and `tests/test_interpolation.py` asserts it
on six shapes including a sawtooth that goes negative with the collar
switched off. That the *uncollared* version fails is what shows the test has
something to catch. Reproducing every node's discount factor to 1e-13 across
all four regions bounds the transcription of the integrals.

**Vanna-volga.** Transcribed from a secondary source, and the protection is
the property the construction is defined by: it must reproduce the three
quoted pillars exactly. `tests/test_fx_smile.py` asserts that on the
*price* rather than on the volatility reader, because the reader
short-circuits at a pillar and testing it there would test the short
circuit. Put-call parity surviving the correction is the second check: the
correction is the same for a call and a put, so a formula error that
treated them differently would break parity.

**The Mexican calendar.** Read directly, which is the strongest row in this
table — and it still does not answer the question asked. QuantLib's
implementation is named `BmvImpl` and reports "Mexican stock exchange";
PRD-003 AC-1.2 asks for Banxico's banking calendar, which is a different
list. The calendar is therefore named `BMV`, the difference is one of the
entries in `UNRESOLVED_MXN`, and `tests/fixtures/bmv_holidays.csv` exists
to be diffed by a human. The two lines to confirm first: whether Banxico
observes Holy Thursday, and whether it applies any weekend-observance roll,
which this calendar does not.

**Every MXN convention.** Not "read in a secondary source" — *not
established at all*. banxico.org.mx, isda.org, cmegroup.com and bis.org all
return 403 at this environment's egress proxy, and a code search across
QuantLib for TIIE returns zero hits. The TIIE day count, the 28-day coupon
period, which conventions attach to TIIE 28 as against TIIE de Fondeo, and
the SIE series identifiers are all assumptions. They are stored as data —
`rates_engine.curves.mxn.UNRESOLVED_MXN` — so that every peso result names
them, carries a degradation that reaches `ASSUMED`, and can be made to
refuse outright. Resolving them shortens a tuple; no code changes. See
`docs/forge/research/003-mxn-conventions.md` for what was tried.

## What is validated against nothing external

The dual-curve solver. There is no free source of Term SOFR par rates or
OIS-versus-term basis spreads, so v1 proves the solver rather than a market
fit: the inputs are constructed with a known basis, and the tests assert
properties that hold for any basis plus an exact identity at zero basis. Every
such result declares `inputs_origin="synthetic"`, and asking for real quotes
raises rather than substituting something plausible.

The same is true of the volatility cube in v2. There is no free source of
swaption volatility quotes, so `tests/fixtures/swaption_vol_cube.csv` is
constructed, and its provenance says so. It is generated from a smile that is
quadratic in moneyness — deliberately *not* SABR — so the residual AC-3.2
bounds measures how well Hagan's expansion approximates a shape it did not
produce. A cube generated by SABR would have made that residual a statement
about the solver.

`tests/fixtures/zero_curve.csv` and `tests/fixtures/fomc_futures_strip.csv`
are built the same way: the first from a Svensson curve, which Nelson-Siegel
cannot reproduce because it has a second hump, and the second from a known
step path rounded to the exchange price tick, so that no step path reprices
the rounded strip exactly. In both cases the fit is measured against
something it cannot reach by construction.

## Assumptions carried in the code

| Assumption | Where | How it is marked |
| --- | --- | --- |
| SR1 contract notional is USD 5,000,000 | `instruments/futures.py` | `notional_source="assumed"`; the DV01 of 41.67 is derived from it, not quoted |
| SIFMA observes a Saturday holiday on the preceding Friday | `conventions/calendar.py` | The fixture's provenance records the manual check as **not done** |
| Treasury par yields are usable as a stand-in for OIS par | `curves/bootstrap.py` | Opt-in only; marks every node; a `Degradation` names the swap spread as an unquantified bias |
| SABR's backbone exponent β is 0.5 unless the caller says otherwise | `volatility/sabr.py` | Carried in every `SABRParameters` and every calibration payload; the module says why it is fixed rather than fitted |
| A term rate read off a futures-fitted curve carries no convexity adjustment | `curves/parametric.py` | `TERM_RATE_CAVEAT` plus a `Degradation` marking the result `assumed`; the evidence sets `convexity_adjustment_applied: false` |
| Every MXN convention: TIIE day count, coupon period, benchmark distinction, SIE identifiers, and that the calendar is the BMV's | `curves/mxn.py` | `UNRESOLVED_MXN`, one `Degradation` each on every peso result; `strict_conventions=True` refuses |
| The USD/MXN pip is 1e-4 | `fx/quote.py` | Declared on `CurrencyPair` with no default, and serialised into every payload that reports forward points |
| A cross-currency basis beyond ±500 bp is not a quote | `fx/forward.py` | `MAX_PLAUSIBLE_BASIS_BP`, a named constant; the refusal says the band is a plausibility check rather than a measurement |

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
*CFA L3 Derivatives — Swap Strategies* ·
*Stochastic, Local and Rough Volatility Models* ·
*Volatility, Greeks and Option Strategy Practice (2026)* ·
*CFA L2 Derivatives — Pricing and Valuation* ·
*CFA Economics — Currency Exchange Rates* ·
*CFA L3 Derivatives — Forwards, Futures, and Options* ·
*CFA L3 Asset Allocation — Constraints, Currency, and Benchmarks*
