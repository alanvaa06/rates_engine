# Errors, and what to do about them

The engine's whole argument is that it refuses rather than fills in. That only
helps if the refusal is legible, so this is the contract: every exception the
library raises on purpose, what causes it, whether it is recoverable, and what
to catch.

There are seventeen exception classes plus the base. You almost never want to
catch all of them, because they mean two different things — and the exit code
says which.

| It means | Recoverable | Do this | Exit code | Examples |
| --- | --- | --- | --- | --- |
| **Your inputs cannot support the calculation** | Yes, by changing the input | Supply the missing data, name a convention that exists, or declare the proxy you meant to use. Retrying unchanged is pointless. | `1` | `MissingFixingError`, `UnsupportedConventionError`, `InsufficientDataError`, `ProxySourceNotDeclaredError`, `MissingDependencyError` |
| **The calculation is impossible or undefined on inputs that are fine** | No | Ask a different question, or relax the thing the message names. | `2` | `CurveArbitrageError`, `BootstrapResidualError`, `UnderdeterminedCurveError`, `NoTenorQuoteSourceError`, `IncompleteStripError`, `UndefinedDurationError`, `KeyTenorOutOfRangeError` |

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

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | The command produced a result. |
| `1` | The inputs cannot support the calculation. |
| `2` | The calculation is impossible on inputs that are themselves fine. |

With `--json`, stdout carries one parseable document either way. A failure
before a result puts `{"error": {...}, "exit_code": n}` there, and the
traceback goes to stderr.
