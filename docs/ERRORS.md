# Errors, and what to do about them

The engine's whole argument is that it refuses rather than fills in. That only
helps if the refusal is legible, so this is the contract: every exception the
library raises on purpose, what causes it, whether it is recoverable, and what
to catch.

There are thirty-two exception classes plus the base. You almost never want to
catch all of them, because they mean two different things — and the exit code
says which.

| It means | Recoverable | Do this | Exit code | Examples |
| --- | --- | --- | --- | --- |
| **Your inputs cannot support the calculation** | Yes, by changing the input | Supply the missing data, name a convention that exists, or declare the proxy you meant to use. Retrying unchanged is pointless. | `1` | `MissingFixingError`, `UnsupportedConventionError`, `InsufficientDataError`, `ProxySourceNotDeclaredError`, `ImplausibleInputError`, `MissingDependencyError`, `IncompatibleDependencyError`, `ConfigurationError`, `DeltaConventionError`, `ConventionError` |
| **The calculation is impossible or undefined on inputs that are fine** | Sometimes: each heading below says, and the JSON field follows it | Ask a different question, or relax the thing the message names. | `2` | `CurveArbitrageError`, `CurveMismatchError`, `BootstrapResidualError`, `UnderdeterminedCurveError`, `NoTenorQuoteSourceError`, `IncompleteStripError`, `PolicyBreachError`, `UnresolvedConventionError`, `UndefinedDurationError`, `KeyTenorOutOfRangeError`, `CurrencyMismatchError`, `ShiftRequiredError`, `ExpansionBreakdownError`, `MissingForwardError`, `SliceNotQuotedError`, `CalibrationError` |

Everything derives from `RatesEngineError`, so one `except` catches the lot:

```python
from rates_engine import RatesEngineError

try:
    result = bootstrap_discount_curve(as_of, instruments)
except RatesEngineError as exc:
    print(type(exc).__name__, exc, exc.exit_code)
```

Catch the subclasses when you intend to respond differently. The CLI does,
which is why the taxonomy exists.

---

## Conventions

### `UnsupportedConventionError`

**Exit code 1. Recoverable: name a convention that exists.**

A day count, roll, compounding basis, interpolation or convexity model that
this package does not implement. Raised *instead of* falling back to a
default, because a day count chosen by accident is a wrong number that looks
right — a fixed leg on 30/360 read as ACT/360 is off by about 1.4%, which is
large enough to matter and small enough to pass a glance.

The message names what you asked for and lists what exists.

```
UnsupportedConventionError: day count 'ACT/ACT ISMA' is not implemented;
supported: 30/360, ACT/360, ACT/365F
```

Also raised by `hull_white` without a `kappa`: the model has two parameters
and there is no sensible default for the second.

Also raised by `fred.fetch_series`, and by `load_series_csv` on a file in
FRED's layout, when the series' units are not known to this package and
`percent=` was not given: dividing by 100 on a guess is a hundredfold error
that looks like data.

And for a swap on an overnight index not on ACT/360 once a period is in
progress: its realised part is compounded by `MarketSnapshot.compounded`,
which compounds on 360, so the index cannot be valued part-way through a
period.

### `ConventionError`

**Exit code 1. Recoverable: change the terms.**

The parent of `UnsupportedConventionError` and `UnresolvedConventionError`:
catch it to cover any convention problem.

It is also raised itself, when an instrument is built, if its roll
convention leaves the schedule no accrual period: an `OISSwap`, `IRSwap` or
`CapFloor` whose maturity adjusts onto or before its adjusted effective date.
Friday 2026-05-29 to Sunday 2026-05-31 under Modified Following is the case:
the Sunday rolls back to the Friday. Move the maturity, or roll with a
convention that keeps it after the start (Following gives 2026-05-29 to
2026-06-01).

```
ConventionError: maturity 2026-05-31 adjusts to 2026-05-29 under
modified_following, which is not after the adjusted effective date
2026-05-29, so the schedule has no accrual period. Move the maturity to a
later date, or roll with a convention that keeps it after the start.
```

---

## Market data

### `MissingFixingError`

**Exit code 1. Recoverable: supply the fixing.**

A business day inside a compounding or averaging period has no published
overnight rate, or a named series is absent from the snapshot. Never
interpolated: an invented overnight rate contaminates every compounded rate
that spans it, and the contamination is invisible afterwards.

The message names the date, or the series and what the snapshot does hold.

A *non-publication* day is not this error. Weekends and holidays take the
rate published for the business day before them and accrue at it as one
simple-interest period (a Friday's rate over the weekend is `1 + 3r/360`),
per the CME and New York Fed rule, and the count of reused days is reported
in the result rather than hidden.

Two more cases raise it. A period that opens on a non-publication day
carries in the fixing of the business day immediately before it and no
other; if that fixing is absent the message names it and the older fixing
it declined to carry. And a snapshot serves nothing dated after its
`as_of`: a fixing, or a business day inside a compounding or averaging
period, after the valuation date is refused, naming both dates, so that a
backtest cannot read the future. `realized_sofr_sigma` refuses an `as_of`
later than the snapshot's for the same reason. The fixing dated `as_of`
itself is served; for SOFR, published the next business day, that is a
one-day look-ahead, documented on `MarketSnapshot`.

A trade that has started raises it when a fixing it needs was not passed.
The rule is QuantLib's default: a fixing dated before the valuation date
is required, one dated on it is read if supplied and forecast off the curve
if not, and anything later is forecast. A forecast in place of a fixing
dated `as_of` is reported, as the degradation
`curve_forward_for_todays_fixing:<series>` (below), not raised.

- A swap or FRA needs the history of an overnight period once the period
  has begun, and the fixing of a term period once its fixing date is before
  `as_of` (Term SOFR fixes two SIFMA business days before its period; TIIE
  28 is read on its start). It raises this when `fixings=` is not passed,
  or lacks the index's series (named after the index: `SOFR`,
  `TIIE_FONDEO`, `TERM_SOFR_3M`, `TIIE_28`) or a date it needs. The message
  names the trade, the period, the fixing date or window, the series and
  the keyword.
- A cap or floor period whose rate fixed before the valuation date is
  valued on its published fixing, so `cap_floor_pv`, `caplet_pv` and
  `option_greeks` raise this when `fixings=` is absent or lacks that date.
  They also raise it when the snapshot has no series named after the
  index, even for a period fixing on the valuation date. The message names
  the series (`TERM_SOFR_3M`) and the date. A NaN fixing never gets that
  far: `Series` refuses it with `ValueError`.
- `caplet_forward_rate` raises it for a period that began before the
  projection curve's date: its rate is a fixing, not a forward.
- `fit_fomc_step_curve` raises it when a contract started before `as_of`
  and its `fixings` snapshot is absent or lacks the `"SOFR"` series or a
  business day of it.

Recoverable in every case by passing them: `fixings=MarketSnapshot(...)`,
with the series named after the index.

### `InsufficientDataError`

**Exit code 1. Recoverable: widen the window or supply more history.**

An estimator was given fewer observations than it needs to mean anything.
Realised volatility needs at least 60 daily changes; a volatility from thirty
observations is a number, not an estimate.

Also raised by the file and FRED providers when a CSV or a response yields
no observations at all — empty, header only, or every value marked missing
— and by the Banxico parser for a row it cannot read, including a value
whose comma could be a decimal comma.

### `ProxySourceNotDeclaredError`

**Exit code 1. Recoverable: declare the proxy, or stop using it.**

Instruments carrying `data_quality="proxy"` — Treasury par yields standing in
for OIS par — were passed to a bootstrap that was not told to expect them.
Using them is allowed; using them by accident is not, so the opt-in is
explicit and there is no default that admits one.

```python
bootstrap_discount_curve(as_of, instruments, long_end_source="treasury_proxy")
```

Once declared, the affected nodes are marked, a degradation is recorded
naming the swap spread as an unquantified bias, and both travel up the
evidence chain into whatever consumes the curve.

Also raised for an unrecognised `long_end_source`: the only accepted value is
`"treasury_proxy"`.

### `ImplausibleInputError`

**Exit code 1. Recoverable: check the input, or widen the band on purpose.**

A well-formed number so far from anything a market produces that using it
would be worse than refusing. The case it exists for is a cross-currency
basis beyond ±500 bp, which is a currency crisis rather than a quote — and
which, coming out of `implied_basis`, usually means a curve or the quoted
forward is wrong rather than that the market moved that far.

The bands are plausibility checks, not measurements: the data that would
calibrate them was not reachable. Each is therefore a named, documented
constant — `MAX_PLAUSIBLE_BASIS_BP`, `MAX_PLAUSIBLE_DECIMAL_RATE` and the
two below — that a caller can widen deliberately, rather than a magic
number inside a comparison.

Two more bands follow the same rule. `MAX_PLAUSIBLE_RATE` (+/-100% a year,
continuously compounded): `StructureQuote` and the CLI's `fx` block refuse
a rate written in percent, such as 9.5 for 9.5%. `MAX_PLAUSIBLE_SIGMA`
(0.10, 1,000 bp a year) guards the convexity sigma of `ho_lee` and
`hull_white`: a sigma given in percent or basis points lands outside it.

Also raised by the file and FRED providers when units were not stated and
a series known to be a rate reads above `MAX_PLAUSIBLE_DECIMAL_RATE` (30%)
as a decimal: the file is in percent. Pass `percent=True`, or
`percent=False` to keep the values.

The error is also raised for a NaN, an infinite, or a too-large-to-price
input (a finite quote whose residual overflows) before anything is solved:
by `bootstrap_discount_curve` (naming every instrument),
`solve_dual_curve`, `fit_fomc_step_curve` (prices and fixings) and
`fit_nelson_siegel` (times and rates).

One more condition is not a band at all. `solve_zero_cost_strike` raises
it when the protection asked for costs more than the entire opposite wing
is worth, so no strike funds it and no zero-cost collar exists at that
protective level. The refusal names the strike to widen. It is here rather
than under a solver error because nothing failed to converge: the bracket
is exhausted and the answer is that there isn't one.

### `MissingDependencyError`

**Exit code 1. Recoverable: install the extra the message names.**

An optional extra is needed for this path. Today that means YAML configs:
`pip install "finport-ratesengine[config]"`. JSON configs need no extra, which
is why they are the native format.

### `IncompatibleDependencyError`

**Exit code 1. Recoverable: install the version range the package pins.**

An optional extra *is* installed, at a version this code does not speak. It
subclasses `MissingDependencyError`, so one `except` still covers "the extra
is not usable", and it exists separately because the two have different fixes.
Telling someone to install what they already have sends them the wrong way —
which is exactly what happened before this class existed: `rateng-mcp` reported
"the MCP server needs the SDK" at an SDK that was installed, because mcp 2.x
renamed `FastMCP` to `MCPServer` and the import looked in the 1.x place.

### `ConfigurationError`

**Exit code 1. Recoverable: pass the config, or fix the key the message names.**

Raised when a command that reads a configuration has none; when the file
has a key, anywhere in it, that the schema `rateng describe --json`
publishes under `config` does not document (the message names the path
and the closest accepted key; keys starting with `_` are comments); or when
a block the command reads has a required key missing, or a `null` where the
key's default is a value (the message names the path). The whole file is
checked before anything is computed. Under `--json` the CLI also raises it
for a command line its
parser rejects, exit code 1; without `--json` argparse prints its usage and
exits 2. The MCP tools take the config as an optional argument, so a call
with none is this rather than a `KeyError`. `describe` and
`list-instruments` need no config and do not open one.

Also raised by the futures hedge, library and CLI alike, for inputs that
contradict each other or cannot be told apart, each fixed by editing the
input: a `FuturesNode.contract` (a CLI `curve.futures[n].contract`) that is
not `"SR1"` or `"SR3"`; a declared contract its period contradicts (an
SR1 on an IMM quarter, an SR3 on a calendar month); a `contract_dv01`
(`hedge.contract_dv01`) that contradicts a contract the node declares or
its period names -- 25.00 on a calendar month, which is an SR1 at 41.67;
calibration instruments whose keys still collide after the node date is
appended to repeated labels; a zero in `shock_table`'s `shocks_bp`
(`hedge.shocks_bp[i]`); and a `hedge.require_full_coverage` that is not a
JSON boolean.

Also raised by `load_program` for a policy file this engine cannot read: an
unknown key, a missing required one, a rebalance frequency that is not one
of the four, a value that will not coerce, or an `allowed_instruments` entry
naming a structure the engine does not build. Every one of those is the same
mistake — the file says something the engine cannot act on — and every one is
fixed by editing the file.

A proposal the programme *forbids* is not one of them; that is
`PolicyBreachError`, below.

### `UnresolvedConventionError`

**Exit code 2. Raised only when you ask for it.**

A convention this build *assumes* rather than *knows* was required to be
known. The default is the opposite: proceed, and mark. Every affected result
names the unverified conventions, carries a `Degradation` per one, and so
reaches `worst_quality == "assumed"` — which anything priced on it inherits,
because the evidence chain composes.

Set `strict_conventions=True` to turn the marking into a refusal. That is
for code that must not rest on an assumption, and it fires before any
calculation rather than after.

The case it exists for is MXN. PRD-003's research gate could not reach
Banxico, ISDA or CME from the build environment, so the TIIE day count, the
28-day coupon period, the distinction between TIIE 28 and TIIE de Fondeo,
and the difference between the BMV and Banxico calendars are all
assumptions. They are stored as a data gap rather than a code one:
`rates_engine.curves.mxn.UNRESOLVED_MXN` is a tuple, and resolving them
shortens it and changes nothing else.

### `MarketDataError`

The parent of `MissingFixingError`, `InsufficientDataError`,
`ProxySourceNotDeclaredError` and `ImplausibleInputError`. Catch it to
cover any data problem.

The rest of this section is filed here because that is where a caller
looks for them, not because they descend from it: the two dependency
errors and `ConfigurationError` come straight off `RatesEngineError`, and
`UnresolvedConventionError` is a `ConventionError`.

---

## Curves

### `CurveArbitrageError`

**Exit code 2. Not recoverable by retrying: the quotes disagree.**

Either a discount factor is non-positive, which is not a curve under any rate
environment, or a *bootstrapped* curve has a segment whose discount factor
rises. A rising segment means a negative zero rate over that stretch, which
the quotes do not support.

Two things this deliberately does **not** raise on:

- Constructing a `DiscountCurve` with a rising segment. Negative rates exist,
  and every DV01 in this package comes from a symmetric bump that can push a
  one basis point forward through zero. Use `curve.require_monotone()` when
  you mean to assert it, or read `curve.rising_segments`.
- A bumped curve. See above.

### `CurveMismatchError`

**Exit code 2. Not recoverable by changing one input.**

Two curves were combined that do not describe the same market state —
today, two different valuation dates. Each curve discounts from its own
`as_of`, so combining curves struck on different days gives a number that
is part forward and part stale. On USD/MXN, six months of drift is around
four thousand pips, and the evidence would report one year fraction for
both legs. Roll one curve to the other's date first.

Exit code 2 for the same reason as `CurrencyMismatchError`: each curve is
fine on its own, and it is the combination that has no meaning.

### `BootstrapResidualError`

**Exit code 2. Recoverable: loosen the tolerance, or fix the quote.**

Under `strict=True`, an input instrument does not reprice within
`tolerance_bp` after its node was solved. With `strict=False` the instrument
is recorded in `result.dropped_instruments` with the reason instead — never
simply discarded.

Also raised, under either strictness, when a monotone convex bootstrap has
not settled after `max_iterations` sweeps (raise it, or use
`log_linear_df`, which needs none), and by `solve_dual_curve` when an OIS
or dual instrument misses `tolerance_bp` or `least_squares` reports it did
not converge.

### `UnderdeterminedCurveError`

**Exit code 2. Recoverable: supply one instrument per node.**

No instruments at all, or two instruments pinning the same node, which leaves
some other node without a quote of its own. The message names the clashing
labels or the nodes.

`fit_fomc_step_curve` also raises it for a meeting on or before `as_of`, or
a segment no contract prices a day of.

### `NoTenorQuoteSourceError`

**Exit code 2. Not recoverable in v1.**

`solve_dual_curve` was asked for real Term SOFR par or basis swap quotes.
There is no free source of either, so v1 validates the solver against
synthetic inputs and says so: pass `inputs_origin="synthetic"`. A real
provider arrives in v1.1.

### `CurveError`

The parent of the five above.

---

## Currency and FX

### `CurrencyMismatchError`

**Exit code 2. Not recoverable by changing the input.**

Two currencies met where the operation needs one: discounting a peso
cashflow on a dollar curve, or summing present values in different
currencies. Exit code 2 rather than 1 because each input is fine on its own
— it is the combination that has no meaning.

Both halves of that are enforced. `pv` checks every flow against the
discount curve, and `PriceResult.__add__` checks the two units, which is
why adding present values is spelled `a + b` rather than
`a.value + b.value`: the obvious spelling is the one that refuses, and it
carries both evidence chains into the total instead of dropping them.

There is no implicit conversion and there will not be one. Converting needs
a spot rate, a date and a quoting convention, all of which are decisions;
`rates_engine.pricing.fx_forward` is where they are made explicitly. Until v3 the engine had
one currency and never said so, which is why `Currency` defaults to `USD`:
every v1 and v2 call means what it always meant.

### `DeltaConventionError`

**Exit code 1. Recoverable: state the convention.**

"25 delta" does not name a strike. FX has four conventions in common use —
spot or forward, premium-adjusted or not — and they give four *different*
strikes for the same quoted number, hundreds of pips apart at ordinary
volatilities. There is no default, because PRD-003's research gate could
not establish which one USD/MXN trades on and a default would let an
unverified convention set every strike in the smile.

Also raised when the delta is not attainable under the convention given,
which is a real condition rather than a guard: a spot delta cannot exceed
`e^{-r_f T}`, and premium-adjusted delta is not monotone in the strike, so
a delta above its peak names no strike at all.

---

## Hedging

### `IncompleteStripError`

**Exit code 2. Recoverable: supply the missing contract, or waive the check.**

The futures strip has no contract covering part of the swap's span. The
message names the gap. A missing contract is never extrapolated from its
neighbours, because the hedge would then report as complete when it is not.

Pass `require_full_coverage=False` when a partial hedge is what you want.
From a CLI or MCP config, set `"require_full_coverage": false` in the
`hedge` block.

### `PolicyBreachError`

**Exit code 2. Not recoverable by changing the input: neither input is wrong.**

`audit_hedge(..., strict=True)` and the proposal departs from the programme —
outside the discretion band, or an instrument the policy does not permit. The
message carries every non-informational finding.

It is deliberately not a `ConfigurationError`, which it used to be. The two
need opposite responses: a `ConfigurationError` from `load_program` means the
policy file is unreadable and you fix the file, while this means the file was
read and understood, the hedge is a perfectly good hedge, and the two
disagree — you change the trade, or you get the mandate changed. Catching one
must not catch the other.

The default is to report rather than refuse. Whether a breach stops a trade
is a treasury decision and not this package's to make; `strict=True` is how a
caller says it has already made that decision.

### `HedgeError`

The parent of both of the above.

The futures hedge's refusals of contradictory inputs are
`ConfigurationError`, above. `strip_hedge` raises a plain `ValueError` only
when `instruments` holds no `FuturesNode` at all.

---

## Risk

### `UndefinedDurationError`

**Exit code 2. Not recoverable: ask for the monetary measure instead.**

A price-normalised risk measure was asked for on an instrument that prices at
zero. A par swap is the ordinary case: it is worth nothing, so modified,
effective and key-rate *duration* all divide by zero.

The measures that are defined there, and what the message points you at:

| Undefined at zero price | Defined at zero price |
| --- | --- |
| `effective_duration` | `dv01`, `pvbp`, `money_duration` |
| `effective_convexity` | `money_convexity` |
| `key_rate_duration` | `key_rate_dv01` |

The threshold is relative to notional (`1e-8`), so it means the same thing on
a one million and a one billion trade.

### `KeyTenorOutOfRangeError`

**Exit code 2. Recoverable: ask for a tenor the curve covers.**

A key tenor falls outside the curve's span. Key rates are not extrapolated:
the answer would be a property of the extrapolation rule rather than of the
instrument. The message names the tenor and the span.

### `RiskError`

The parent of both.

---

## Not an exception: `NotImplementedError`

`macaulay_duration` and `modified_duration` exist as names and raise
`NotImplementedError` with the reason. They are defined from a single yield to
maturity, which needs an instrument priced away from zero — a fixed rate bond,
arriving in v1.1. `effective_duration` is a curve-based measure and is a
different quantity, so it is never served under these names.

They exist rather than being absent so that reaching for them returns the
reason instead of an `AttributeError`.

---

## Not an exception: degradations

Some failures are *reported* rather than raised, because the number is still
computable and worth less than it looks. These arrive as
`Degradation` entries in `result.evidence.warnings`, and they propagate up the
evidence chain:

| Code | Means |
| --- | --- |
| `treasury_par_proxy` | The long end rests on Treasury par yields standing in for OIS par. The swap spread they carry is negative and variable, and the bias is known to exist and is not quantified. |
| `outside_quoted_strikes` | A cube volatility at a strike outside the quoted strikes of its smile: SABR extrapolated beyond its data. The reading has `reliable=False` and is `assumed`. |
| `outside_quoted_smile` | An FX smile volatility at a strike beyond the outermost quoted pillars, held flat at the nearest one; vanna-volga is not arbitrage free there. `assumed`. |
| `curve_forward_for_todays_fixing:<series>` | A fixing dated the valuation date was not among the fixings passed, so the curve's forward stands in for it, as QuantLib does by default. Raised by the linear pricers, the cap and floor pricers and `fit_fomc_step_curve`, one per series. `proxy`. Pass the fixing to price on the print. |
| `sr1_notional_assumed` | The hedge holds SR1 contracts, sized at 41.67 USD per basis point from a 5,000,000 USD notional that has not been read from the CME contract spec. `assumed`. |
| `futures_contract_assumed` | A futures period is neither a calendar month (SR1) nor one IMM date to the next (SR3), so the contract is sized as SR3. Declare `FuturesNode.contract`, or pass `contract_dv01`. `assumed`. |
| `smile_forward_unverified` | The smile given to a hedge structure does not report the forward it was built on, so it could not be checked against the forward the legs are struck on, basis included. `assumed`. |
| `nelson_siegel_tau_at_bound` | The fitted decay `tau` sits on a bound of `tau_bounds`: the range set it, not the data. Widen the bounds if the range was not a deliberate constraint. `assumed`. |
| `term_rate_without_convexity` | A term rate read off a futures-fitted curve is the expected average overnight rate, with no convexity adjustment (`TERM_RATE_CAVEAT`). `assumed`. |
| `unresolved_convention:<name>` | A convention this build assumes rather than knows: one per entry of `UNRESOLVED_MXN` on a peso curve, and one per unresolved convention of the instrument's index and of the collateral rate on a price (`mxn_collateral_rate` on any peso one). `assumed`; `bootstrap_mxn_curve(strict_conventions=True)` refuses with `UnresolvedConventionError` instead. |

Read them with `rates_engine.core.diagnostics.all_warnings(result.evidence)`, or
check `result.evidence.worst_quality`.

---

## Volatility and options

### `VolUnitsError`

**Exit code 1. Recoverable: quote the volatility in the units the call wants.**

Two failures wearing one name, both worth orders of magnitude.

A *relative* volatility was handed to something expecting an *absolute* one,
or the reverse. There is no conversion between them — only the at-the-money
equivalence `sigma_normal = sigma_lognormal * F`, which is the leading term of
an expansion and is wrong away from the forward.
`Volatility.atm_equivalent_normal` computes it and says so; nothing converts
silently.

Or the magnitude is implausible for the units declared: a normal volatility
outside 0.1 to 1,000 basis points, or a lognormal one outside 5% to 500%. The
lognormal floor is 5% rather than the 0.5% PRD-002 first proposed, because
0.5% does not catch the mistake: swaption normal vols are 60 to 150 bp, which
as decimals are 0.006 to 0.015, all of it above 0.005. A floor of 5% sits in
the gap between the two populations. FX has its own band: `StructureQuote`
holds a float FX volatility to 0.2%-500% (`MIN_PLAUSIBLE_FX_VOLATILITY`),
so 11.5 for 11.5% raises it; pass
`Volatility.unchecked(value, VolUnits.LOGNORMAL_DECIMAL)` for a market
outside it.

`Volatility.unchecked` skips the magnitude band — and only that band — for a
genuinely extreme market or for testing the limits.

A third case: `sabr.lognormal_vol` on a smile with a non-zero shift returns
a shifted-Black volatility, which `Volatility`, `black.price` and
`swaption_pv` cannot carry; it raises this unless `shifted_black=True`. Use
`normal_vol`, which a shift does not change.

### `ShiftRequiredError`

**Exit code 2. Recoverable: shift the model, or quote normal volatility.**

A lognormal model was asked for at a forward or strike at or below zero. Black
and unshifted SABR take logarithms of both. This is not a hard case for the
model, it is the wrong model; the market's answer is Bachelier, and SABR's is
a shift.

A strike of exactly zero under Black is *not* this error: the limit exists and
is finite, so the call is worth the forward and the put is worth nothing.

### `ExpansionBreakdownError`

**Exit code 2. Not recoverable at these parameters.**

Hagan's SABR implied volatility is an asymptotic expansion with error of order
`nu^2 T`, and its `O(T)` correction is additive. At long expiries, high
vol-of-vol and far strikes that correction can exceed the leading term and
drive the result to or below zero. A negative volatility prices nothing, so
the expansion is refused rather than passed on, and the message names the
strike and expiry where the boundary was crossed.

Shorten the expiry, lower `nu`, narrow the strikes, or use a model that solves
SABR rather than expanding it. `density_diagnostics` maps the region, and
`expansion_is_valid` tests one point.

### `MissingForwardError`

**Exit code 2. Recoverable: extend the projection curve, or shorten the cap.**

A period of a cap or floor ends past the projection curve's last node. The
curve will extrapolate a flat forward there, which is a defensible convention
for a discount factor and not for an option: the caplet would be priced off a
rate the market never quoted, inside a total that reports as complete.

### `SliceNotQuotedError`

**Exit code 2. Recoverable: quote the slice.**

The volatility cube has no quotes at that expiry and tenor. Filling a strike
inside a quoted smile is a model fitted to data; filling a whole missing slice
would be a model fitted to a *different* slice, which is a larger claim than
v2 makes. There is no interpolation across expiry or tenor.

### `CalibrationError`

**Exit code 2. Recoverable: supply more quotes, or loosen the tolerance.**

A model could not be fitted: fewer quotes than parameters, inputs that do not
align, or a fit outside the tolerance the caller set. The message carries the
error achieved and the parameters reached, because a calibration that quietly
returns its starting point is worse than one that refuses.

The FX smile raises it for half a ten-delta pair, a ten-delta strike inside
the 25-delta one, a broker butterfly no smile fly reprices, and a premium
outside the implied-volatility bracket.

### `VolatilityError`

The parent of the volatility family.

---

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | The command produced a result. |
| `1` | The inputs cannot support the calculation. |
| `2` | The calculation is impossible on inputs that are themselves fine. |

With `--json`, stdout carries one parseable document either way. A failure
before a result puts `{"error": {...}, "exit_code": n}` there, and the
traceback goes to stderr. That includes a command line the parser rejects
(`ConfigurationError`, exit code 1, usage on stderr). The MCP tools fail
with the same document after the SDK's `Error executing tool <name>: `.
`error.recoverable` repeats the heading of the exception's section.
`--help` with `--json` puts a `Usage` document on stdout, exit code 0.
