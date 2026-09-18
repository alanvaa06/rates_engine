# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

A change to a golden test's tolerance is a change to what this package
claims, so it belongs here too.

## [Unreleased]

## [0.3.0] - 2026-09-18

A second currency, FX forwards and options, and a hedge-structure
comparator that does not recommend. No v1 or v2 number moves.

### Added

- **`Currency`**, carried on `DiscountCurve`, on `Cashflow` and derived on
  `CurveSet`. Defaults to `USD`, so every v1 and v2 call means what it
  always meant. `CurrencyMismatchError` (exit code 2) refuses to discount a
  cashflow on a curve of another currency, or to build a `CurveSet` whose
  two curves disagree. There is no implicit conversion: that needs a rate,
  a date and a quoting convention, and those are decisions.
- **`BMVCalendar`** and the `BMV` instance: the Mexican stock exchange
  calendar, thirteen rules including the 2006 Monday-observance reform and
  the sexennial inauguration day. Named BMV rather than Banxico because it
  *is* the exchange calendar — Banxico's banking calendar is a different
  list, and the diff is `[manual-check]` and outstanding.
- **`HolidayCalendar`**, the shared base every calendar's rolling, counting
  and stepping now comes from. Extracted when the second calendar arrived
  rather than duplicated.
- **FX.** `CurrencyPair` carrying the pip its forward points are counted
  in; `garman_kohlhagen` with price, delta, gamma, vega, vanna, volga and
  theta, each checked against a bump; `forward_from_curves` and
  `implied_basis`, which keep the cross-currency basis separate from the
  covered-interest-parity forward rather than folding it in.
- **Four delta conventions, and no default.** `DeltaConvention` is spot or
  forward crossed with premium-adjusted or not. They give four *different*
  strikes for the same quoted delta — over 300 pips apart on USD/MXN at
  three months — and `DeltaConventionError` refuses rather than picking
  one, because the research gate could not establish which USD/MXN trades
  on. Premium-adjusted delta is not monotone in the strike, so the
  inversion locates the peak and returns the out-of-the-money branch, and
  refuses a delta above the peak instead of approximating it.
- **Vanna-volga.** `VannaVolgaSmile` builds a smile from ATM, risk reversal
  and butterfly and reproduces all three quoted pillars exactly. Past the
  outer pillars it holds the volatility flat and marks the reading
  unreliable rather than extrapolating a construction that can imply a
  negative density. `ATMConvention` is explicit for the same reason the
  delta convention is: "at the money" is three different strikes.
- **The MXN curve.** `bootstrap_mxn_curve` with `TIIEBenchmark`, and
  `compare_benchmarks` reporting the zero-rate gap between the two.
- **`UNRESOLVED_MXN` and `UnresolvedConventionError`.** Every MXN
  convention this build assumes rather than knows, carried as a
  `Degradation` on every peso result so `worst_quality` reaches `assumed`
  and anything priced on it inherits that. `strict_conventions=True` turns
  the marking into a refusal.
- **Seven hedge structures.** `compare_structures` returns a table —
  unhedged, forward at a ratio, protective option at and out of the money,
  collar, zero-cost collar, option spread, seagull — with cost, worst case,
  best case, upside participation and a payoff grid, plus the residual
  variance decomposition when a correlation is supplied. It does not rank
  them, and no name in the package contains `recommend`.
- **`HedgeProgram`.** The hedging policy as data, loaded strictly — an
  unknown key refuses and names itself — and `audit_hedge`, which reports
  departures with a severity and what would bring them back.
- **A Banxico SIE client.** Token from the caller or the environment, never
  the repository; parsing tested against recorded payload shapes because
  this build has never reached the live endpoint.
- **`rateng fx-forward` and `rateng hedge-structures`**, which the MCP
  server picks up as tools automatically because it takes its table from
  the CLI's.
- **`ImplausibleInputError`.** A well-formed number outside the band this
  build accepts — a cross-currency basis beyond ±500 bp. The band is a
  named constant a caller can widen deliberately, and its own message
  admits it is a plausibility check rather than a measurement.

### Changed

- `rateng describe` reports the currencies, the FX models, the four delta
  conventions and their absent defaults, the hedge structures, the
  unresolved MXN conventions, and `recommends: false`.
- `bootstrap_discount_curve` takes a `currency`, defaulting to USD.
- `DiscountCurve.shifted`, `.with_node` and the parametric samplers carry
  the currency through.

### Fixed

- **A holiday observed across a year boundary was reported as a business
  day.** When New Year's Day falls on a Saturday, SIFMA observes it on the
  preceding Friday — 31 December of the *previous* year, which correctly
  belongs to `holidays(next_year)`. `is_business_day` looked only in
  `holidays(day.year)` and so reported 31 December 2021 and 31 December
  2027 as business days. Anything that rolled or counted across those dates
  — a schedule, an accrual, a settlement date — was off by a day. Present
  in 0.1.0 and 0.2.0. Found while writing the second calendar's tests; no
  existing test caught it because every fixture in the suite starts in
  January.

## [0.2.0] - 2026-09-18

Rate options, a volatility cube, two parametric curves, a second
interpolation, and the same payloads over MCP. No v1 number moves.

### Added

- **Volatility with its units attached.** `Volatility(value, units)` and
  `VolUnits` (`normal_bp`, `normal_decimal`, `lognormal_percent`,
  `lognormal_decimal`). There is no conversion between normal and lognormal
  because there is none to have; `atm_equivalent_normal` and
  `atm_equivalent_lognormal` are named for the at-the-money approximation
  they are. Out-of-band magnitudes raise `VolUnitsError`, with
  `Volatility.unchecked()` as the deliberate way past it.
- **Bachelier and Black.** Price, vega, delta and implied volatility for
  each. Black refuses a non-positive forward with `ShiftRequiredError`
  rather than returning a number, and has the exact `K = 0` limit.
- **Swaptions and caps/floors.** `Swaption`, `Caplet` and `CapFloor` as
  contracts, priced through `optionpricing` on the annuity numeraire.
  `optionpricing.model_for` picks the model from the volatility's units.
  A cap period ending past the projection curve's last node raises
  `MissingForwardError`.
- **Option greeks.** `risk.option_greeks`: delta, gamma, vega and theta by
  bump and reprice, with the bump in the evidence. Vega is always on the
  normal basis, whichever model priced the option.
- **SABR.** Hagan et al. (2002) eq. (2.17a), (2.18) and (A.59a), with β
  fixed rather than fitted and the reason recorded. `calibrate`,
  `density_diagnostics` (Breeden-Litzenberger) and `expansion_is_valid`.
  Where the expansion goes negative it raises `ExpansionBreakdownError`
  instead of returning a volatility that prices nothing.
- **The volatility cube.** `VolCube` fills unquoted strikes inside a quoted
  smile with SABR and marks them synthetic; across expiries or tenors it
  raises `SliceNotQuotedError`. Units, strike convention, as-of date, β and
  shift all serialise with it.
- **Monotone convex interpolation.** Hagan and West (2006) as a second
  `DiscountCurve` interpolation, with the step-2 collar that carries
  positivity from the inputs to the interpolated forwards.
  `curves.compare_interpolations` bootstraps one instrument set both ways
  and reports the largest instantaneous-forward gap, which is the only thing
  the choice changes.
- **Parametric curves.** `fit_nelson_siegel` by concentrated least squares,
  deterministic where a four-way nonlinear search is not, and
  `fit_fomc_step_curve`, a piecewise-constant overnight path fitted to SR1
  and SR3 settlements. A term rate read off the latter carries
  `TERM_RATE_CAVEAT` and a `Degradation`: it is an expected average
  overnight rate, not a traded term rate.
- **`pricing.price_on_parametric`.** Prices one instrument on a fitted curve
  and on the curve it was fitted to, and reports the gap. The payload is
  marked `curve_kind="parametric"` and takes the model's name from the fit.
- **`rateng-mcp`.** The five CLI handlers over stdio, behind the `mcp`
  extra (`mcp>=2.0,<3`, whose server class is `MCPServer`). A tool's answer
  is byte-identical to the corresponding `--json` command because it is the
  same callable; the server writes no file, opens no socket and invokes no
  model. A refusal from the engine is re-raised as the SDK's own `ToolError`
  carrying the named exception and its message — anything else is flattened
  by the SDK into `Error executing tool <name>` with the message dropped.
  Without the SDK the module still imports and names the extra that installs
  it; with the wrong version of it, the message says so instead.
- **`rateng list-instruments`.** What this build prices and what each
  instrument needs.
- **Fixtures.** `swaption_vol_cube.csv`, `zero_curve.csv`,
  `fomc_futures_strip.csv` and `fomc_meetings.csv`, each constructed from a
  shape the model being fitted cannot reproduce, each with a provenance
  sibling saying so.

### Changed

- `rateng describe` reports the option models, volatility units, smile
  model and parametric curves alongside what it already reported.
- `docs/RESEARCH.md` gains the v2 formulas and what "not read" costs for
  Hagan and Hagan-West. `AGENTS.md` gains the volatility-units trap and the
  five refusals around it.
- Acceptance criteria are now referenced in tests as `PRD-001 AC-3.1`
  rather than `AC-3.1`, because the two PRDs have criteria with the same
  numbers. `scripts/audit_acceptance.py` audits every shipped PRD.

### Fixed

- `_z_over_x` in the SABR expansion used `1 + ρz/2` in its small-`z` series
  where the expansion gives `1 − ρz/2`. The error was below 1e-7 in implied
  volatility and only near the money, which is why it took a branch
  continuity test to find.
- The monotone convex region-four formulas divided by zero when either end
  of an interval sat exactly on its discrete forward. The limit there is
  `g == 0` across the interval, whose integral agrees with the plain
  quadratic's, so no node discount factor changes.

### Added (errors)

- `VolatilityError` and its five children: `VolUnitsError`,
  `ShiftRequiredError`, `MissingForwardError`, `ExpansionBreakdownError`,
  `SliceNotQuotedError`. Plus `CalibrationError`, `ConfigurationError` and
  `IncompatibleDependencyError`. Twenty-five classes plus the base;
  `docs/ERRORS.md` covers all of them.

## [0.1.0] - 2026-09-16

First release. Curves, futures convexity, linear pricing, risk and hedging,
each result carrying the evidence it rests on.

### Added

- **Conventions.** `DayCount` (ACT/360, ACT/365F, 30/360), the SIFMA US
  calendar with the twelve recommended holidays, three roll conventions, IMM
  dates and a single backward-generating `Schedule`.
- **Evidence.** `Evidence` composes rather than flattens: `sources` holds the
  evidence of the inputs, `qualities` is the recursive union, and
  `worst_quality` ranks `observed < synthetic < proxy < assumed`. A hedge
  built on a proxied curve reports the proxy without the hedging code knowing
  what a proxy is.
- **Market data.** `MarketSnapshot` with per-series provenance, SOFR
  compounding and averaging that report how many days reused a previous
  published rate, a `file` provider and a `fred` provider that imports
  `urllib` inside the call.
- **Instruments.** `OISSwap`, `IRSwap` (Term SOFR projection, OIS
  discounting), `FRA`, `SOFRFuture1M` and `SOFRFuture3M`. Contract DV01 is
  derived from notional and nominal tenor: USD 25.00 for SR3, USD 41.67 for
  SR1.
- **Curves.** Log-linear-in-discount-factor interpolation, a sequential
  bootstrap where every instrument exposes one node and one residual, the four
  views with their conventions attached, and a dual-curve solver in both
  sequential and simultaneous modes.
- **Convexity.** Ho-Lee and Hull-White adjustments, with Hull-White converging
  to Ho-Lee as mean reversion vanishes, and sigma either explicit or estimated
  from realised SOFR.
- **Pricing.** `pv`, `par_rate`, `annuity` and a central-difference parallel
  `dv01`.
- **Risk.** `key_rate_dv01` built from tent shocks that form a partition of
  unity, `key_rate_duration`, `pvbp`, `money_duration`, `money_convexity`,
  `effective_duration` and `effective_convexity`.
- **Hedging.** `strip_hedge` sizing contracts from bucketed deltas by
  instrument quote, and `shock_table` reporting the convexity the strip cannot
  remove in dollars and in basis points.
- **CLI.** `rateng bootstrap / price / hedge / describe`, all with `--json`.
- **Docs.** `AGENTS.md`, `llms.txt`, `docs/ERRORS.md`, `docs/RESEARCH.md`,
  `docs/RELEASING.md`.

### Refusals

Seventeen named exceptions, each with an exit code and an entry in
`docs/ERRORS.md`. A missing fixing is never interpolated, an unimplemented
convention never falls back to a default, a Treasury proxy never enters a
curve undeclared, a key rate is never extrapolated, and a duration is never
divided by a zero price.

### Known limits

- The CME whitepaper goldens (AC-6.6, AC-8.1, AC-8.2) and the CME settlement
  comparisons (AC-5.1, AC-5.2) are **skipped**: the build environment's egress
  policy blocked both CME and FRED, so the third-party fixtures could not be
  fetched. Tolerances are unchanged and the tests activate as soon as the
  files land in `tests/fixtures/cme_published/`. Nothing was substituted in
  their place.
- The SIFMA Saturday-observance rule is implemented but **not yet checked**
  against SIFMA's published calendar. The fixture's provenance records this.
- The dual-curve solver is validated against synthetic inputs only; there is
  no free source of Term SOFR par or basis quotes. Every such result declares
  `inputs_origin="synthetic"`.
- The SR1 contract notional of USD 5,000,000 is assumed rather than read from
  the contract spec.
- The Hull-White convexity formula is transcribed via Skov and Skovmand rather
  than read in Henrard 2018. The Ho-Lee limit test is what guards it.

[Unreleased]: https://github.com/alanvaa06/rates_engine/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/alanvaa06/rates_engine/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/alanvaa06/rates_engine/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/alanvaa06/rates_engine/releases/tag/v0.1.0
