# Errors, and what to do about them

The engine's whole argument is that it refuses rather than fills in. That only
helps if the refusal is legible, so this is the contract: every exception the
library raises on purpose, what causes it, whether it is recoverable, and what
to catch.

There are twenty-seven exception classes plus the base. You almost never want to
catch all of them, because they mean two different things — and the exit code
says which.

| It means | Recoverable | Do this | Exit code | Examples |
| --- | --- | --- | --- | --- |
| **Your inputs cannot support the calculation** | Yes, by changing the input | Supply the missing data, name a convention that exists, or declare the proxy you meant to use. Retrying unchanged is pointless. | `1` | `MissingFixingError`, `UnsupportedConventionError`, `InsufficientDataError`, `ProxySourceNotDeclaredError`, `MissingDependencyError`, `IncompatibleDependencyError`, `ConfigurationError`, `DeltaConventionError` |
| **The calculation is impossible or undefined on inputs that are fine** | No | Ask a different question, or relax the thing the message names. | `2` | `CurveArbitrageError`, `BootstrapResidualError`, `UnderdeterminedCurveError`, `NoTenorQuoteSourceError`, `IncompleteStripError`, `UndefinedDurationError`, `KeyTenorOutOfRangeError`, `CurrencyMismatchError`, `ShiftRequiredError`, `ExpansionBreakdownError`, `MissingForwardError`, `SliceNotQuotedError`, `CalibrationError` |

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

### `ConventionError`

The parent. Catch it to cover any convention problem.

---

## Market data

### `MissingFixingError`

**Exit code 1. Recoverable: supply the fixing.**

A business day inside a compounding or averaging period has no published
overnight rate, or a named series is absent from the snapshot. Never
interpolated: an invented overnight rate contaminates every compounded rate
that spans it, and the contamination is invisible afterwards.

The message names the date, or the series and what the snapshot does hold.

A *non-publication* day is not this error. Weekends and holidays reuse the
previous published rate, per the CME and New York Fed rule, and the count of
reused days is reported in the result rather than hidden.

### `InsufficientDataError`

**Exit code 1. Recoverable: widen the window or supply more history.**

An estimator was given fewer observations than it needs to mean anything.
Realised volatility needs at least 60 daily changes; a volatility from thirty
observations is a number, not an estimate.

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

**Exit code 1. Recoverable: pass the config.**

A command that reads a configuration was called without one. Over the `rateng`
CLI argparse makes this unreachable, because `--config` is required for every
command that needs it. It exists for the MCP server, whose tools take the
config as an optional argument: without this, calling `bootstrap` with no
config would surface as whichever `KeyError` the handler hit first. `describe`
and `list-instruments` are the two tools that answer without a config.

### `MarketDataError`

The parent of the first three. Catch it to cover any data problem.

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

### `BootstrapResidualError`

**Exit code 2. Recoverable: loosen the tolerance, or fix the quote.**

Under `strict=True`, an input instrument does not reprice within
`tolerance_bp` after its node was solved. With `strict=False` the instrument
is recorded in `result.dropped_instruments` with the reason instead — never
simply discarded.

### `UnderdeterminedCurveError`

**Exit code 2. Recoverable: supply one instrument per node.**

No instruments at all, or two instruments pinning the same node, which leaves
some other node without a quote of its own. The message names the clashing
labels or the nodes.

### `NoTenorQuoteSourceError`

**Exit code 2. Not recoverable in v1.**

`solve_dual_curve` was asked for real Term SOFR par or basis swap quotes.
There is no free source of either, so v1 validates the solver against
synthetic inputs and says so: pass `inputs_origin="synthetic"`. A real
provider arrives in v1.1.

### `CurrencyMismatchError`

**Exit code 2. Not recoverable by changing the input.**

Two currencies met where the operation needs one: discounting a peso
cashflow on a dollar curve, or summing present values in different
currencies. Exit code 2 rather than 1 because each input is fine on its own
— it is the combination that has no meaning.

There is no implicit conversion and there will not be one. Converting needs
a spot rate, a date and a quoting convention, all of which are decisions;
`rates_engine.fx` is where they are made explicitly. Until v3 the engine had
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

### `CurveError`

The parent of the four above.

---

## Hedging

### `IncompleteStripError`

**Exit code 2. Recoverable: supply the missing contract, or waive the check.**

The futures strip has no contract covering part of the swap's span. The
message names the gap. A missing contract is never extrapolated from its
neighbours, because the hedge would then report as complete when it is not.

Pass `require_full_coverage=False` when a partial hedge is what you want.

### `HedgeError`

The parent.

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

Read them with `rates_engine.diagnostics.all_warnings(result.evidence)`, or
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
the gap between the two populations.

`Volatility.unchecked` skips the magnitude band — and only that band — for a
genuinely extreme market or for testing the limits.

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
traceback goes to stderr.
