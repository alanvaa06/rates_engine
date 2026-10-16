# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

A change to a golden test's tolerance is a change to what this package
claims, so it belongs here too.

## [Unreleased]

A reorganisation into layers, then two decisions the layering exposed:
instruments carry their rate index, and curves calibrate to real swaps.
Then the corrections from the review of 2026-10-15, and those do move
numbers. The layering itself moves one -- the adjusted accrual ends, which
is all that entry means by "the one change in this release" -- and each
correction below gives its own before and after: a calendar checked against
the SOFR publication record, SR3 compounded as CME settles it, swaps, caps
and swaptions that have started valued on what has happened, an FX forward
run from its spot date, futures sized at their own tick, and a refusal
wherever a wrong number used to come back with exit code 0.

### Changed (breaking)

- **The CLI's `par_swaps` entries are swaps.** Each entry gives
  `effective`, `maturity` and `rate` (optional `frequency_months`, default
  12, and `payment_lag_days`, default 2) and is calibrated as a
  `SwapQuoteNode`, so the curve reprices the quoted SOFR OIS with the pricer
  itself, payment lag included. The v0.3 shape -- `start`, `payment_dates`,
  `year_fractions` -- built a `ParSwapNode`, which assumes each period is
  paid the day it ends and so misprices a SOFR OIS by several basis points
  at the front (5.5 bp at one year on the 2026-01-15 strip). It is refused
  with `ConfigurationError` (exit code 1) and a message that spells out the
  new keys.
- **Accrual period ends are adjusted, and dollar numbers move.**
  `Schedule.generate` now rolls each period end by the same business-day
  convention as the payment (Modified Following by default) and pays
  `payment_lag_days` business days after the adjusted end. That is the USD
  SOFR OIS convention (CME's Eris SOFR contract terms: periods end on the
  roll date "subject to adjustment in accordance with the Modified Following
  Business Day Convention", payment two business days later) and the 2006
  ISDA Definitions default, under which period end dates adjust with the
  payment dates. v0.3 left period ends unadjusted while payments rolled, so
  whenever a roll date fell on a weekend or holiday the period accrued to a
  day that was not paid, and the floating leg stopped telescoping. This is
  the one change in this release that moves a dollar number, and only where
  a roll date is not a business day. On the README quickstart (a two-year
  IMM-dated OIS) the par rate moves from 4.0539% to 4.0537% and DV01 from
  -21,870.88 to -21,870.07 USD/bp. IMM dates are Wednesdays and rarely roll,
  so futures-strip hedges barely move. The CME golden tests are skipped
  without the published fixture, as before, so they give no signal here.
  `adjust_accrual_ends=False` reproduces the v0.3 schedule. It also closes
  a 1.7 bp gap on rolled dates between a curve fitted to `SwapQuoteNode`s
  and one fitted to `ParSwapNode`s; what remains between them is the payment
  lag, under Added.
- **Instruments carry their rate index, and it fixes their currency.**
  `OISSwap`, `IRSwap`, `FRA`, `CapFloor` and `Caplet` take an `index`
  (`SOFR`, and Term SOFR of the frequency, by default). v0.3 took an
  instrument's currency from the curve it was priced on, so a SOFR swap
  priced on a peso curve came back as a peso swap rolling on SIFMA holidays;
  it now raises `CurrencyMismatchError`. `calendar` defaults to `None`,
  meaning the index's calendar, so a TIIE de Fondeo swap rolls on BMV
  without being told. `IRSwap.float_frequency_months` and
  `CapFloor.frequency_months` default to `None` (taken from the index) and
  are refused if they contradict it. Both are resolved into the fields, so
  `IRSwap()`, `IRSwap(float_frequency_months=3)` and
  `IRSwap(index=TERM_SOFR_3M)` are equal. A frequency with no published
  Term SOFR (two months, say) is now refused: v0.3 accepted it and labelled
  the leg `term_sofr_2m`, an index that does not exist. A `FRA` takes an
  `index` (Term SOFR 3M by default, whatever its period; an overnight index
  is refused) and its evidence gains an `index` key.
- **Instruments no longer project their own cashflows.** Removed:
  `OISSwap/IRSwap.cashflows`, `float_cashflows`, `FRA.fair_rate`,
  `FRA.cashflows`, `Caplet.forward_rate`, `Caplet.numeraire`. Use
  `pricing.projection.project(instrument, curve_set)`, `float_leg`,
  `fra_fair_rate`, and `pricing.options.caplet_forward_rate` /
  `caplet_numeraire`. `fixed_cashflows()` takes no curve. `Priceable` and
  `Swappable` moved to `pricing.projection` and require `currency` instead
  of `cashflows`. A third-party instrument prices with `pv`, and through it every risk measure
  and hedge, once registered with `project.register`; a swap-like one also
  needs `float_leg.register` for `par_rate`, `annuity` and swaptions.
- **`OISSwap`'s `float_index` is `compounded_<index>`**: `compounded_sofr`
  for a dollar swap, which is again byte-identical to v0.3.0, and
  `compounded_tiie_fondeo` for a peso one.
- **`CURVE_TIME_BASIS`** moved to `conventions.daycount`, so an instrument can
  measure option time without importing the curve layer.
- **`SIFMA_US` no longer closes the Friday before a Saturday New Year's Day
  or Veterans Day.** SIFMA moves a Saturday holiday to the Friday only for
  Independence Day, Juneteenth and Christmas, and the New York Fed published
  SOFR on 31 December 2021 and 10 November 2023. `is_business_day` on those
  dates (and on 2027-12-31, 2028-11-10, ...) is now `True`, and
  `holidays(2022)` holds 2022-01-01 instead of 2021-12-31. The 0.3.0 entry
  that called the old rule SIFMA's was wrong.
- **A schedule's effective date is rolled by its convention**, as
  QuantLib's `Schedule` does. Numbers move only when the effective date is
  not a business day: a one-year 4% OIS on USD 100m from the June 2030 IMM
  date (Juneteenth) accrues from 2030-06-20, and its PV goes from 20,591.21
  to 20,351.93 USD. `adjust_accrual_ends=False` still reproduces the old
  schedule.
- **A `MarketSnapshot` serves nothing dated after its `as_of`.** `fixing`,
  `compounded` and `averaged` raise `MissingFixingError` for a business day
  after the valuation date, and `require` returns the series cut there;
  `series` keeps what was loaded. `realized_sofr_sigma` refuses an `as_of`
  later than the snapshot's. A snapshot as of 2026-01-15 used to serve 10
  February's fixing and compound through 30 January: a look-ahead in any
  backtest.
- **FRED units are explicit.** `load_series_csv`, `load_snapshot_csv` and
  `fred.fetch_series` take `percent=`. A CSV in FRED's download layout
  (`observation_date,<SERIES>`) is read in FRED's units, exactly as
  `fetch_series` reads the same bytes; the review's FRED file used to load 0
  observations with the default columns, and 3.64 instead of 0.0364 with the
  columns named. `SOFRINDEX`, an index level, is no longer divided by 100,
  and a FRED series whose units this package does not know raises
  `UnsupportedConventionError` until `percent=` is given, instead of being
  divided by 100.
- **Reading nothing is an error.** A CSV or FRED response with no
  observations raises `InsufficientDataError`; a column missing from the
  header raises `ValueError` naming the header; a FRED file whose value
  column names another series is refused.
- **A swap or FRA whose floating coupon is already decided needs its
  fixings.** `pv`, `discounted_value`, `par_rate`, `par_rate_value`,
  `price_on_parametric`, `project`, `float_leg` and `fra_fair_rate` take a
  keyword-only `fixings`, a `MarketSnapshot` read by the series named after
  the index (`SOFR`, `TIIE_FONDEO`, `TERM_SOFR_3M`, `TIIE_28`). An
  overnight period is decided once it has begun. A term period is decided
  once its fixing date is before `as_of`; a fixing dated `as_of` itself is
  read if supplied and forecast off the curve if not, as QuantLib does by
  default, so a swap struck at spot today still prices without fixings.
  Term SOFR fixes two SIFMA business days before its period (the ARRC
  convention, as for caplets below); TIIE 28 is read on its period start, a
  lag still to be verified. Without the fixings it needs, a trade raises
  `MissingFixingError` (exit code 1) naming the period, the fixing date or
  window, the series and how to pass it. It used to return a number with
  the decided part priced off the curve, or at zero once begun. Risk
  measures, hedges and `rateng price` call these functions and refuse such
  a trade until they pass fixings on.
- **`key_rate_duration` is in years.** It is `KR DV01 / (PV x 1bp)`, and its
  `total` equals `effective_duration`; it was reported per basis point,
  1e4 smaller.
- **`fit_fomc_step_curve` needs the fixings of a contract already running.**
  A contract whose period started before `as_of` raises `MissingFixingError`
  unless `fixings` are given.
- **A caplet expires at its fixing.** Term SOFR fixes two US Government
  Securities business days before its period (ARRC), so `cap_floor_pv` and
  `caplet_pv` measure expiry to `Caplet.fixing_date`, not to the accrual
  start. A five-year cap is about 0.24% cheaper (216,763.11 to 216,249.07)
  and now matches QuantLib with `fixingDays = 2` to 2.4e-15.
  `fixing_lag_days=0` restores the old numbers.
- **A cap, floor or swaption that has started is valued on what has
  happened**, by QuantLib's rule (`enforcesTodaysHistoricFixings = False`).
  A period paid on or before the valuation date is dropped. One fixed before
  it is its intrinsic value on the published fixing, passed as `fixings=` (a
  `MarketSnapshot`, series named after the index); without it,
  `MissingFixingError`. One fixing on the valuation date uses that day's
  fixing if supplied, and otherwise the curve's forward with no time left
  (status `fixes_today`; the evidence's `fixing_source` says which). A
  swaption expiring on the valuation date is its intrinsic value on the
  forward swap rate; one that expired before it raises `ValueError`. A
  seasoned 3% floor went from 314,181.72 to QuantLib's 11,200.15.
- **`sabr.lognormal_vol` refuses a shifted smile** with `VolUnitsError`
  unless `shifted_black=True`: with a shift it is a shifted-Black
  volatility, which no pricer here carries.
- **An FX forward delivering on or before its spot date raises
  `ValueError`.** The forward now runs from the spot date (under Fixed).
- **`worst_case_rate` and `best_case_rate` are worked out from the legs,
  and are `None` on an open side.** They were the edge of the payoff grid,
  so they moved with `grid_width`.
- **FX inputs no market produces are refused.** `StructureQuote` refuses a
  volatility outside the lognormal band (`VolUnitsError`), a negative or
  non-finite one and a non-positive expiry (`ValueError`), and rates beyond
  ±100% (`ImplausibleInputError`); `compare_structures` refuses a
  correlation outside `[-1, 1]`; Garman-Kohlhagen raises on a negative or
  non-finite expiry or volatility;
  `SmileQuotes.wings()` refuses a broker butterfly; half a ten-delta pair is
  a `CalibrationError`.
- **A futures strip is sized at each contract's own tick.** An SR1 is worth
  USD 41.67 a basis point and an SR3 USD 25.00; `strip_hedge` used to divide
  every contract by one `contract_dv01` (25.00 by default, and the CLI always
  passed 25.00), so SR1 months were bought 1.67 times over -- 28.7 contracts
  where 17.2 offset the February SR1 of a mixed SR1/SR3 strip.
  `FuturesNode` takes an optional `contract` (`"SR1"` or `"SR3"`); left out,
  a calendar-month period is SR1, an IMM quarter SR3, and any other period
  is sized as SR3 as before, recorded as `assumed`.
  `strip_hedge(contract_dv01=None)` is the new default; a number still
  applies to every contract and is refused if it contradicts a declared one.
  `HedgeResult.contract_dv01` is `None` for a strip that mixes the two,
  `HedgeResult.contract_dv01_by_label` is new, and the evidence field
  `contract_dv01_derivation` is now a mapping per contract. A hedge on the
  SR1 tick carries an `sr1_notional_assumed` degradation. The CLI reads
  `curve.futures[].contract`. An SR3-only strip is bit-identical.
- **A config key a command does not read is refused.** Every command that
  takes a config raises `ConfigurationError` (exit code 1) for a key, in a
  block it uses, that it neither reads nor finds in the documented schema,
  naming the path and the closest key: `swap.notionl (did you mean
  notional?)`. A misspelt key used to fall back to its default with exit 0:
  `notionl` priced a 1,000,000 notional (PV 921.99 instead of 92,199.33),
  `convexty` dropped the convexity adjustment (PV 114,866.89), `key_tenor`
  left `key_rate_dv01` null, `shock_bps` used the default grid. Keys another
  command reads are accepted, so one file still serves `bootstrap`, `price`
  and `hedge`; keys starting with `_` are comments.
- **A missing required config key is `ConfigurationError`, not
  `KeyError`**, naming the path and what it holds. Exit code unchanged (1).
- **With `--json`, a command line the parser rejects is one error document,
  exit code 1.** It was exit 2 with an empty stdout. Without `--json`,
  unchanged.
- **MCP tools fail with the CLI's error document.** Every exception reaches
  the agent as `Error executing tool <name>: ` followed by what
  `rateng <name> --json` prints. Anything but a `RatesEngineError` used to
  arrive as `Error executing tool <name>` with no type or message.
- **`error.recoverable` follows `docs/ERRORS.md`:** `true` for every exit
  code 1 and for the eight exit-code-2 refusals headed "Recoverable"
  (`IncompleteStripError`, `KeyTenorOutOfRangeError`,
  `BootstrapResidualError`, `UnderdeterminedCurveError`,
  `ShiftRequiredError`, `MissingForwardError`, `SliceNotQuotedError`,
  `CalibrationError`). It was `exit_code == 1`.

### Changed

- **Module paths.** The package is now ten layers of subpackages instead of
  fourteen loose modules beside seven packages
  (`docs/architecture/ARCHITECTURE.md`). Import from `rates_engine` or from
  the module that defines a name; the old deep paths are gone:
  `errors`, `evidence`, `results`, `money` and `diagnostics` moved to
  `core`; `pricing` became `pricing.linear`, `optionpricing` became
  `pricing.options`, `fx.forward` became `pricing.fx_forward`; `risk`
  became `risk.sensitivities`; `hedging`, `hedging_structures` and
  `hedge_program` became `hedging.futures_strip`, `hedging.fx_structures`
  and `hedging.program`. The closed forms moved to `models` (`black`,
  `bachelier`, `sabr`, `garman_kohlhagen`, `fx_delta`, `convexity`,
  `gaussian`). `fx.vannavolga` became `volatility.fx_smile`. `Side`,
  `OptionKind` and `CurrencyPair` moved to `conventions`. The `fx` package
  no longer exists.
- **`realized_sofr_sigma`** and `SigmaEstimate` moved from `convexity` to
  `market.estimators`: measuring sigma from history is market data, and the
  convexity formula no longer imports a snapshot.
- **Subpackage `__init__` files re-export nothing.** `from
  rates_engine.curves import DiscountCurve` now fails; the package root and
  the defining module are the two homes of a name.
- **An application layer.** Config loading, the config-to-domain builders
  and the seven use cases moved out of `cli` into `app.config`,
  `app.builders` and `app.commands`; `app.commands.COMMANDS` is the table
  both interfaces dispatch through. `cli` and `mcp_server` are sibling
  adapters and no longer import each other (`mcp_server` used to import the
  CLI's private `_COMMANDS`). `rates_engine.cli.load_config` is now
  `rates_engine.app.config.load_config`. No payload changed in this step;
  the text changes listed below are the only differences from v0.3.0.
- **Text that named a module now names where it lives.** The hedge's
  bucketed-delta note points at `rates_engine.key_rate_dv01` (the stable root
  name) instead of `rates_engine.risk.key_rate_dv01`; the currency-mismatch
  refusal points at `rates_engine.pricing.fx_forward` instead of
  `rates_engine.fx`.
- **Discounting evidence is derived, not written.** Every pricer used to
  record `"discounting": "collateral_rate_ois_sofr"` whatever the curve, so a
  peso swap on a peso curve claimed an OIS-SOFR discount. The fields now come
  from `conventions.indices.collateral_index(curve.currency)`: dollar
  payloads are byte-identical, peso ones say `collateral_rate_ois_tiie_fondeo`
  and carry an `unresolved_convention:mxn_collateral_rate` degradation, which
  makes their `worst_quality` `assumed`. No number moves.
- **`UNRESOLVED_MXN`, `TIIE_PERIOD_DAYS` and `TIIE_DAY_COUNT`** moved from
  `curves.mxn` to `conventions.indices`, and `UNRESOLVED_MXN` gained
  `mxn_collateral_rate`. `describe` lists it; an MXN curve carries one more
  `assumed` degradation.
- **Calendars are typed as `HolidayCalendar`**, not `SIFMAUSCalendar`, on
  instruments, snapshots, curve views and the FOMC step curve, so a `BMV`
  calendar is a valid argument rather than a type error. Defaults unchanged.
- **One bump primitive.** `risk.bumps` (`shift_from_bp`, `tent_shift`,
  `repriced`, `BUMP_BP`, `tent_weights`) is the only code that moves a curve
  and reprices; every sensitivity is written on it instead of spelling out
  `pv(instrument, curve_set.shifted(shift)).value` and discarding the evidence
  it built. `dv01` moved from `pricing` to `risk.sensitivities` (its payload,
  `produced_by: "pricing.dv01"` included, is unchanged: `produced_by` is an
  identifier, not a module path). `option_greeks` and `GreeksResult` moved to
  `risk.greeks`, typed `Swaption | CapFloor` instead of `Any`. The option
  greeks and the shock table take their shifted curves from
  `risk.bumps.shifted`, and `tests/test_layering.py` fails on any other
  `.shifted(` call outside `curves`. Bit-identical.
- **`tests/test_layering.py`** checks layers rather than a total order of
  modules, and nothing imports sideways or upward: the last exception,
  `instruments -> curves`, went with the projection change above.
- **Faster curves and schedules, same bits.** `DiscountCurve` caches its
  node times, log discount factors and monotone-convex interpolant instead
  of recomputing them on every `df()`, finds the interval by bisection, and
  hands those arrays to the curves it derives (`with_node`, `shifted`).
  `MonotoneConvex` keeps running integrals over whole intervals, summed in
  the order the per-call loop used. Instruments compute their schedule once.
  Every output of a 80 kB battery (discount factors, forwards, zeros, PV,
  par, annuity, DV01, key rates, strip hedge, shock table, both
  interpolations) is bit-identical to v0.3.0. Against v0.3.0 on
  `benchmarks/bench_core.py`, with the bump primitive below: repricing a
  ten-year OIS x10 (x22 on monotone convex), pricing a newly built one x5,
  DV01 x9, key rate DV01 x5.6, bootstrap x3, shock table x3.3, strip hedge
  x1.8.
- **`fx_forward` says which basis it takes.** `basis_bp` is a flat
  zero-coupon spread on the quote-currency leg at one delivery, which is
  not the par spread a cross-currency swap quotes; the two agree at one
  coupon period and drift apart after it. The module docstring,
  `AGENTS.md` and `docs/RESEARCH.md` now say so, and say that no
  cross-currency swap instrument, implied discount curve or calibration
  node exists here to convert between them. No number changes.
- `StructureQuote.expiry` is the span from the spot date to settlement;
  pair payloads gain `spot_lag_days` and `settlement_calendars`; the CLI's
  `fx` rates are documented, and recorded in the evidence, as continuously
  compounded ACT/365F decimals.
- The licence is declared as the SPDX expression `MIT` with `license-files`
  (PEP 639); building needs setuptools>=77.
- Dependency floors raised to `pandas>=2.2.2` and `scipy>=1.13`: the
  previous floors could not be installed together with `numpy>=2.0`.

### Fixed

- **Risk numbers inherit their curve's degradations.** In v0.3.0 `pv` and
  `dv01` carried a curve's provenance, but `key_rate_dv01`,
  `key_rate_duration`, `money_convexity`, `effective_duration`, the swaption
  and cap/floor pricers and `option_greeks` did not: on a Treasury-proxied or
  peso curve they reported `worst_quality: observed`. Every one of them now
  carries `pricing.collateral.curve_warnings(curve_set)`. Dollar payloads on
  an unproxied curve are unchanged.
- **A curve built from lists froze them.** `DiscountCurve` and
  `MonotoneConvex` convert their inputs to tuples, so the cached arrays
  cannot outlive a mutation of the caller's list.
- **`tests/test_layering.py` saw neither relative imports nor a bare
  `import rates_engine`**, so an upward edge written either way passed. Both
  are resolved now, relative imports are banned, and the detector has its
  own test.
- Documentation counted thirty-one exceptions; there are thirty-two plus the
  base.
- **Memorial Day was a week early in ten years of every twenty-three**
  (2021-23, 2027-28, 2032-34, 2038-39): the last Monday of May was searched
  from the 28th and never stepped forward. FRED has SOFR on 2022-05-23 and
  none on 2022-05-30. Next affected date: 2027-05-31.
- **2018-12-05**, the national day of mourning for President George H. W.
  Bush, is a `SIFMA_US` closure.
- **A first period of zero days is dropped** when the roll convention pulls
  the first roll date onto the effective date; an `IRSwap` from 2026-05-29
  with annual rolls on the 31st now prices instead of raising.
- Docstrings: Juneteenth has been a federal holiday since 2021; SR3
  compounding exceeds the SR1 average by about 3.1 bp over a quarter at 5%,
  not a tenth of a basis point.
- **SR3 settles on CME's formula.** `MarketSnapshot.compounded` compounds
  `prod(1 + r_i d_i / 360)` over publication days, so a weekend is
  `1 + 3r/360`, not `(1 + r/360)^3`. The old rule put SR3 0.025-0.045 bp
  high on flat SOFR of 4.30-5.33% (USD 0.6-1.1 a contract). Matches
  QuantLib's `OvernightIndexedCoupon` to 1e-13.
- **No stale fixing is carried into a period.** A period opening on a
  non-publication day takes the previous business day's fixing or raises
  `MissingFixingError`; an August SR1 with July missing carried 30 June's
  rate and moved 1.46 bp silently.
- **NaN fixings are refused.** `Series` rejects NaN and infinity, naming the
  date; the file and FRED providers skip "", ".", "na", "n/a" and "nan" in
  any case (lower-case "nan" used to load as a NaN fixing).
- **Banxico decimal commas are refused, not stripped.** "11,2850" would
  have read as 112850; only `1,850.50`-style thousands separators are
  accepted.
- **Flows paid on or before the valuation date are worth nothing.** They
  were discounted at a factor of one: on a 4% payer over USD 100m from
  2024-07-15 to 2027-07-15 valued 2026-01-15, the coupon paid 2025-07-17
  entered at -4,055,556 USD. A flow paid on `as_of` counts as paid.
- **A swap that has started is valued on what is left of it.** The
  overnight period in progress compounds its published fixings to `as_of`
  and the curve after it; a term period pays its fixing; the fixed coupon in
  progress counts in full, so `par_rate` is QuantLib's `fairRate`. A 4%
  payer on USD 100m from 2025-07-15 to 2027-07-15, valued 2026-01-15: PV
  -2,238,694.78 -> -17,348.13 and par 2.8519% -> 3.9911%, QuantLib's figures
  to 5e-8 USD with the SR3 compounding above. Term SOFR IRSs, seasoned or
  starting at spot, match QuantLib (`fixingDays = 2`) to 1e-7 USD. Trades
  with no decided coupon are bit-identical.
- `bootstrap_discount_curve(interpolation="monotone_convex")` now reprices
  instruments that read discount factors inside an interval (an OIS paid
  two business days after each period): the curve is swept to a fixed
  point, as QuantLib's `IterativeBootstrap` does. A 12-swap SOFR strip went
  from residuals of up to 0.1145 bp (and a `BootstrapResidualError` under
  `strict`) to 6e-12 bp; `compare_interpolations` now runs on swap strips.
  Log-linear curves are bit-for-bit unchanged. The 0.2.0 entry's "the only
  thing the choice changes" holds on a futures strip; on swaps paid with a
  lag the two interpolations also put the nodes apart, by up to 0.58 bp of
  zero rate on that strip.
- The monotone convex positivity collar no longer bends curves with
  negative forwards (a flat -50 bp curve read -88 to -26 bp); it applies
  only against non-negative intervals.
- `instantaneous_forward` on a monotone convex curve before `as_of` returns
  the forward at `as_of`, not the long-end forward.
- `fit_nelson_siegel` scans a 100-point tau grid before refining (a
  Svensson curve went from 7.16 to 1.60 bp RMSE) and flags a tau on its
  bound with a `nelson_siegel_tau_at_bound` degradation; NaN or infinite
  inputs raise `ImplausibleInputError`, a non-finite RMSE `CalibrationError`.
- `FOMCStepCurve` compounds by the CME/New York Fed rule,
  `prod(1 + r_i d_i/360)` over business days (a weekend was three daily
  periods: +0.029 bp a quarter); `fit_fomc_step_curve` prices realised days
  of an in-progress SR1/SR3 from `fixings`, and refuses unknown symbols and
  NaN prices.
- `solve_dual_curve` enforces `tolerance_bp` and refuses a non-converged
  solve (`BootstrapResidualError`); its docstring no longer claims the two
  modes can differ.
- `bootstrap_discount_curve` and `solve_dual_curve` refuse NaN or infinite
  quotes with `ImplausibleInputError` (exit code 1), naming each instrument.
- `convexity_adjustment` is 0 for a reference period already started (it
  was negative) and refuses a sigma above `MAX_PLAUSIBLE_SIGMA` (0.10, 1,000
  bp a year) or non-finite inputs.
- The par view declares its `start` and `payment_lag_days`.
- NaN volatilities, SABR parameters and implied-vol targets are refused
  instead of priced into NaN or the top of the bracket.
- Vega at sigma = 0 is the limit (zero off the money, the closed form at
  it).
- SABR's `x(z)` no longer cancels or raises `math domain error` at large
  `|z|`.
- The vol cube matches quoted strikes within 1e-10 and marks extrapolation
  (`sabr_extrapolated`, `reliable=False`, `outside_quoted_strikes`).
- An option payload's `unit` is its currency (MXN for TIIE).
- Vega of a lognormal quote converts exactly through the price, caplet by
  caplet: a 10y10y at 30% was 0.893 of its normal-quoted vega.
- `caplet_forward_rate` refuses a period that began before the curve.
- Theta is documented as volatility decay only.
- **The USD/MXN forward runs from the T+2 spot date** (joint SIFMA_US + BMV
  calendar), not from the valuation date: it was 76-78 pips high at every
  tenor, and 81.7 pips at 3M across 16 September; `implied_basis` no longer
  reads that carry as basis (-125 bp at 1W on a zero-basis forward).
- An FX forward (and `hedge-structures`) on a peso curve carries the curve's
  degradations and reads `assumed`.
- `hedge-structures` applies the fx block's `basis_bp` and prices on the
  same spot-date forward as `fx-forward` (they were 1,941 pips apart at 1Y
  on -100 bp).
- Residual variance under a partial hedge is
  `s_a^2 + (1-h)^2 s_x^2 + 2(1-h) rho s_a s_x` (h = 0.8: 0.011265 ->
  0.009149).
- `upside_participation` is the share of a favourable move kept (ATM call
  0.533 -> 1.0, seagull 0.433 -> 0.119, collar 0.279 -> 0.339).
- `gk.delta_spot` at zero volatility compares the forward with the strike,
  magnitude `e^{-r_f T}`.
- The vanna-volga smile uses the ten-delta quotes and reprices all five
  pillars (a 10-delta call read 15.10% for an 18.00% quote).
- The smile's implied-volatility inversion refuses a premium outside its
  `[1e-6, 5.0]` bracket instead of returning the bound.
- **Futures that share a label are sized one by one.** The hedge keyed its
  dicts by label, whose default is `"future"`, so the README strip built
  without labels was sized as one contract: 101.6 contracts instead of 878,
  a hedge ratio of -0.116, a net of +193,078 USD at +10 bp instead of -310,
  no error. Repeated labels are now keyed `label@period-end`, as the
  bootstrap's residuals already were. Labelled strips are unchanged.
- **Overlapping futures no longer open a false gap.** The coverage check
  compared each contract only with the next, so SR1 months under an SR3
  quarter were refused with a hole the quarter covers.
- **A zero shock is refused by name.** `shock_table` raised a bare
  `ZeroDivisionError` on `shocks_bp` containing 0; it now raises a
  `ValueError` that says to drop it.
- **The bucketed-delta notes say what they measure.** They claimed the
  bucketed deltas and the key rates sum to the same parallel DV01; each sums
  to its own (-21,949.91 against -21,870.07 USD/bp on the README strip), and
  the factor between them is about (365/360)/(1 + F tau), not 365/360.
- `describe` and `list-instruments` no longer open `--config`;
  `rateng describe --config nope.json --json` was exit 1 with
  `FileNotFoundError`.
- `scripts/audit_acceptance.py` exits 1 when the suite fails, and its
  summary no longer counts failed tests as passed.
- Two tests that located `AGENTS.md` and `pyproject.toml` through the
  installed package now pass against a wheel install.
- `AGENTS.md` said only the FRED provider used the network; the Banxico
  provider does too.

### Added

- **`pricing.calibration.SwapQuoteNode`**: a calibration node wrapping a real
  swap, whose residual is `pricing.linear.par_rate_value` itself. The curve
  layer receives it through the `CalibrationInstrument` protocol and still
  imports no product. A curve fitted to it reprices its swaps to 1e-12.
  `ParSwapNode` prices the floating leg as `P(start) - P(last payment)`,
  exact only when each period is paid the day it ends: fitted to
  `ParSwapNode`s built from SOFR OIS payment dates (a two-business-day lag),
  the 2026-01-15 strip misprices its one-year swap by 5.5 bp. With no lag
  the two nodes agree exactly. `ParSwapNode` stays for quotes that arrive as
  dates.
  `SwapQuoteNode` takes an OIS only: a term-rate quote calibrates a
  projection curve, which is the dual-curve solver's job.
- **Term SOFR indices** (`TERM_SOFR_1M/3M/6M/12M`), `INDICES`,
  `index_named` and `term_sofr`. The CLI's `swap` block takes an `index`
  (`price` refuses a non-dollar one, because the curve block quotes SOFR
  instruments; peso swaps are priced through the library), and `describe` lists
  `rate_indices`.
- **Every result carries its instrument's index assumptions.** A TIIE de
  Fondeo swap's price, risk and greeks carry the TIIE day count, calendar
  and collateral conventions as `assumed`, whatever curve they ran on.
- `pricing.linear.annuity_value` and `par_rate_value`, the bare floats.
- `benchmarks/bench_core.py`: hot-path timings through the public API, run
  before and after a structural change.
- **`conventions.indices`**: `RateIndex` (currency, tenor, day count,
  calendar, administrator, unresolved conventions), the `SOFR`,
  `TIIE_FONDEO` and `TIIE_28` definitions, `COLLATERAL_INDEX` and
  `collateral_index`. A currency with no entry is refused with
  `UnsupportedConventionError` rather than defaulted to SOFR.
- **`pricing.collateral`**: the one home of the discounting evidence.
- **`pricing.linear.discounted_value`**: present value as a bare float, the
  same number `pv` returns without the cashflow list and evidence record.
  **`pricing.linear.valuation_evidence`**, formerly private, so a DV01 in
  `risk` is described exactly as a price is.
- `tests/test_curve_cache.py`: a derived curve's handed-down arrays equal a
  fresh computation exactly, and the cache never enters equality or hashing.
- `SIFMAUSCalendar.SPECIAL_CLOSURES`, `VERIFIED_FROM`, `VERIFIED_THROUGH`,
  and `tests/test_sifma_publication_record.py`, which checks every weekday
  from 2018-04-03 to 2026-10-14 against FRED's SOFR record.
- `market.providers.file`: `parse_series_csv`, `is_missing_value`,
  `MISSING_VALUE_MARKERS`, `FRED_DATE_COLUMNS`. `market.classify`:
  `quoted_in_percent`, `resolve_percent`.
- `RealisedFixing`, `Cashflow.fixing` and `Cashflow.projected_fixing_date`:
  what a floating amount rests on. Prices report the fixings read as
  `realised_fixings` in the evidence, with the series' provenance among the
  inputs, so their data quality reaches `worst_quality`; a term period fixed
  on the valuation date adds a `fixings_dated_as_of` entry saying whether
  its fixing was read (`"fixings"`) or forecast (`"curve"`).
- `bootstrap_discount_curve(max_iterations=, iteration_tolerance=)`,
  `DEFAULT_MAX_ITERATIONS`, `DEFAULT_ITERATION_TOLERANCE`; evidence
  `iterations`, `max_iterations`, `iteration_tolerance`.
  `DiscountCurve.with_factor`.
- `fit_fomc_step_curve(fixings=, calendar=)`; `calendar=` on
  `FOMCStepCurve.compounded`, `.averaged` and `.discount_curve`.
- `all_views(par_start=, par_payment_lag_days=)`; `CurveView.start`,
  `CurveView.payment_lag_days`. `models.convexity.MAX_PLAUSIBLE_SIGMA`.
- `pricing.options.option_value`, `PeriodStatus` (`paid`, `fixed`,
  `fixes_today`, `live`), `CapletValuation`, `cap_floor_periods`;
  `Caplet.fixing_date` and `Caplet.time_to_expiry`; `fixing_lag_days` on
  `CapFloor` and `Caplet`; `TERM_RATE_FIXING_LAG_DAYS`;
  `fixings=` on `cap_floor_pv`, `caplet_pv` and `option_greeks`;
  `OptionPriceResult.currency`; `CubeQuote.reliable`;
  `volatility.cube.STRIKE_TOLERANCE`.
- `CurrencyPair.spot_lag_days`, `.calendars`, `.spot_date()`,
  `.is_settlement_day()`; `SETTLEMENT_CALENDARS`.
- `StructureQuote.basis_bp`, `.r_domestic_effective`, `.carried()`.
- `pricing.fx_forward.MAX_PLAUSIBLE_RATE`, `require_plausible_rate`,
  `require_plausible_basis`; `forward_from_curves(inputs=...)`;
  `FXForwardResult.spot_date` and `.year_fraction`.
- `ButterflyConvention` (`SMILE`, the default, and `BROKER`),
  `VannaVolgaSmile.smile_quotes()`, `.wing_pillars()`, `.all_pillars()`.
- `hedge.require_full_coverage` in the CLI's `hedge` block (default `true`,
  a JSON boolean), and `SR1_DV01`.
- `rateng describe --json` publishes every config key under `config`, with
  units, defaults and the commands that read it, the `fx` block included.
- The sdist now carries `tests/conftest.py`, `tests/fixtures/`, `scripts/`,
  `AGENTS.md`, `CHANGELOG.md`, `llms.txt` and `docs/**/*.md` (new
  `MANIFEST.in`), so the suite runs from an unpacked sdist.
- CI: Python 3.14 in the test matrix; a `package` job that builds the sdist
  and wheel, runs `twine check --strict`, installs the wheel in a clean venv
  and runs the suite from the sdist; a `minimum` job that installs every
  core dependency at its floor (numpy 2.0.0, pandas 2.2.2, scipy 1.13.0) on
  Python 3.11.
- The release workflow refuses a tag that does not match `pyproject.toml`
  and `rates_engine.__version__`, and runs the suite against the built
  wheel before uploading.
- `Documentation` project URL (the API reference); classifiers for Python
  3.11-3.14, `Development Status :: 4 - Beta`, `Intended Audience ::
  Financial and Insurance Industry` and `Typing :: Typed`.
- `docs/RELEASING.md`: one-time setup of the GitHub environments and the
  PyPI/TestPyPI pending publishers.

### Removed

- The `data` extra (pyarrow): no code reads or writes Parquet.

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
- **Eight hedge structures.** `compare_structures` returns a table —
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
  admits it is a plausibility check rather than a measurement. Also raised
  by `solve_zero_cost_strike` when the protection asked for is worth more
  than the entire opposite wing, so no strike funds it.
- **`PriceResult.__add__`.** Two present values add, and two in different
  currencies refuse. The sum carries both evidence chains, so an `assumed`
  leg added to an `observed` one degrades the total instead of laundering
  the mark. A measure mismatch — a par rate plus an annuity — is a
  `TypeError`, because that is a mistake in the caller and not a problem
  with the data.
- **`PolicyBreachError`** (exit code 2), raised by
  `audit_hedge(..., strict=True)` where a `ConfigurationError` was raised
  before. A policy file that will not load and a proposal the policy
  forbids need opposite responses — edit the file, or change the trade —
  so catching one no longer catches the other.

### Changed

- `rateng describe` reports the currencies, the FX models, the four delta
  conventions and their absent defaults, the hedge structures, the
  unresolved MXN conventions, and `recommends: false`.
- `bootstrap_discount_curve` takes a `currency`, defaulting to USD.
- `DiscountCurve.shifted`, `.with_node` and the parametric samplers carry
  the currency through, as do `solve_dual_curve` and
  `compare_interpolations`, which both take a `currency` and give it to
  every curve they build.
- `CurveView` carries and serialises the currency. A zero rate is
  dimensionless and a discount factor more so, so the export was the one
  place a currency could be lost on the way out.
- `shock_table` states the currency of its money columns, and refuses a
  curve set that is not USD: the strip's P&L comes from `SR3_DV01`, a
  dollar constant, so netting it against a peso swap P&L would report two
  currencies as one number.
- `audit_hedge` reports `hedge_ratio` as `None` on an option row rather
  than `0.0`, which read as a ratio the structure had rather than one it
  does not define.
- The docstring contract in `test_docstrings.py` now covers each module's
  own `__all__` — 229 names — where it covered the 157 the top-level
  package re-exports.

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
