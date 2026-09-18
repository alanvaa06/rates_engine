"""Every exception this package raises on purpose, and the exit code it carries.

The engine's argument is that it refuses rather than fills in. That only helps
if the refusal is legible, so the taxonomy here is shallow and the names say
what went wrong rather than where. Three families, matching the three things a
caller can do about a failure:

``MarketDataError``
    The inputs cannot support the calculation. Fix the data; retrying is
    pointless. Exit code 1.
``ConventionError``
    A convention, tenor or instrument was named that this package does not
    implement. Fix the request. Exit code 1.
``CurveError`` / ``HedgeError`` / ``RiskError``
    The calculation itself is impossible or undefined on these inputs — an
    arbitrageable curve, a strip with a hole in it, a duration normalised by a
    zero price. Relax something or ask a different question. Exit code 2.

``docs/ERRORS.md`` is the prose version of this module and is checked against
it by ``tests/test_errors_contract.py``: an exception defined here and missing
there fails the suite.
"""

from __future__ import annotations

__all__ = [
    "RatesEngineError",
    "ConventionError",
    "UnsupportedConventionError",
    "MarketDataError",
    "MissingFixingError",
    "InsufficientDataError",
    "ProxySourceNotDeclaredError",
    "MissingDependencyError",
    "IncompatibleDependencyError",
    "ConfigurationError",
    "CurrencyMismatchError",
    "DeltaConventionError",
    "ImplausibleInputError",
    "CurveError",
    "CurveArbitrageError",
    "BootstrapResidualError",
    "UnderdeterminedCurveError",
    "NoTenorQuoteSourceError",
    "HedgeError",
    "IncompleteStripError",
    "RiskError",
    "UndefinedDurationError",
    "KeyTenorOutOfRangeError",
    "VolatilityError",
    "VolUnitsError",
    "ShiftRequiredError",
    "ExpansionBreakdownError",
    "MissingForwardError",
    "CalibrationError",
    "SliceNotQuotedError",
]


class RatesEngineError(Exception):
    """Base class for every deliberate refusal in this package.

    Attributes:
        exit_code: What the CLI returns when this propagates. ``1`` means the
            inputs are unusable, ``2`` means the calculation is impossible on
            inputs that are themselves fine.
    """

    exit_code: int = 1


class ConventionError(RatesEngineError):
    """A day count, calendar, roll or tenor this package does not implement."""

    exit_code = 1


class UnsupportedConventionError(ConventionError):
    """Named convention is not implemented, and no default was substituted.

    Raised instead of silently falling back, because a day count chosen by
    accident is a wrong number that looks right.
    """


class MarketDataError(RatesEngineError):
    """The market data cannot support the calculation being asked for."""

    exit_code = 1


class MissingFixingError(MarketDataError):
    """A business-day fixing the calculation needs is absent from the snapshot.

    Never interpolated. A published overnight rate is an observation, and an
    invented one contaminates every compounded rate that spans it.
    """


class InsufficientDataError(MarketDataError):
    """An estimator was given fewer observations than it needs to be meaningful."""


class ProxySourceNotDeclaredError(MarketDataError):
    """Proxy instruments were supplied without ``long_end_source`` declaring them.

    Treasury par yields standing in for OIS par carry a swap spread. Using them
    is allowed; using them by accident is not, so the opt-in is explicit and
    there is no default.
    """


class MissingDependencyError(RatesEngineError):
    """An optional extra is needed for this path and is not installed.

    The message names the install command that fixes it.
    """

    exit_code = 1


class IncompatibleDependencyError(MissingDependencyError):
    """An optional extra is installed, but not at a version this code speaks.

    Subclasses :class:`MissingDependencyError` so that one ``except`` still
    covers "the extra is not usable", and exists separately because the two
    have different fixes and telling a caller to install something they have
    already installed wastes their afternoon. The message names the version
    range and what moved.
    """

    exit_code = 1


class ConfigurationError(RatesEngineError):
    """The command was asked to run without the configuration it needs.

    ``describe`` and ``list-instruments`` answer from the build itself; every
    other command reads a config. Over the CLI argparse enforces that, but the
    MCP server's tools take the config as an optional argument, so the refusal
    has to be a named one rather than whatever the first missing key happens
    to raise.
    """

    exit_code = 1


class CurrencyMismatchError(RatesEngineError):
    """Two currencies met where the operation needs one.

    Discounting a peso cashflow on a dollar curve, or summing present values
    in different currencies. Exit code 2 rather than 1: the inputs are each
    fine, it is the combination that has no meaning. Converting between them
    needs a rate, a date and a quoting convention, which is
    :mod:`rates_engine.fx`'s job and never an implicit one.
    """

    exit_code = 2


class DeltaConventionError(RatesEngineError):
    """A delta was used without saying which of the four conventions it is.

    "25 delta" names a strike only once spot-versus-forward and
    premium-adjusted-versus-not are both stated; the four combinations give
    four different strikes for the same quoted number. Also raised when a
    delta is not attainable under the convention given, which happens for
    real: premium-adjusted delta is not monotone in the strike, so a delta
    above its peak names no strike at all.
    """

    exit_code = 1


class ImplausibleInputError(MarketDataError):
    """A number arrived that is outside the band this build will accept.

    Not a type error and not an arbitrage: a well-formed quantity so far
    from anything a market produces that using it would be worse than
    refusing. A five-hundred-basis-point cross-currency basis is the case
    this exists for.

    The bands are plausibility checks, not measurements — the data that
    would calibrate them was not reachable — so each one is a named,
    documented constant that a caller can widen deliberately rather than a
    magic number inside a comparison.
    """

    exit_code = 1


class CurveError(RatesEngineError):
    """The curve implied by these instruments cannot be built honestly."""

    exit_code = 2


class CurveArbitrageError(CurveError):
    """Discount factors would be non-positive or increasing in time.

    Names the offending segment. An increasing discount factor is a negative
    zero-coupon yield over that segment in a currency that does not have one,
    which means the inputs disagree rather than that the curve is interesting.
    """


class BootstrapResidualError(CurveError):
    """An input instrument does not reprice within tolerance under ``strict=True``."""


class UnderdeterminedCurveError(CurveError):
    """More curve nodes than instruments: the system does not pin the curve down."""


class NoTenorQuoteSourceError(CurveError):
    """A real tenor-curve quote source was requested and v1 has none.

    The dual-curve solver is validated against synthetic inputs in v1 by
    decision; there is no free source of Term SOFR par or basis swap quotes.
    """


class HedgeError(RatesEngineError):
    """The hedge cannot be constructed from the instruments supplied."""

    exit_code = 2


class IncompleteStripError(HedgeError):
    """The futures strip has no contract covering a period the swap spans.

    Names the period. A missing contract is never extrapolated from its
    neighbours: the hedge would be reported as complete when it is not.
    """


class RiskError(RatesEngineError):
    """The risk measure is undefined or out of range on these inputs."""

    exit_code = 2


class UndefinedDurationError(RiskError):
    """A price-normalised risk measure was asked for on an instrument priced at zero.

    A par swap has ``PV = 0``, so modified, effective and key-rate *duration*
    divide by zero. The defined measures there are ``dv01``, ``key_rate_dv01``
    and ``money_convexity``.
    """


class KeyTenorOutOfRangeError(RiskError):
    """A key tenor falls outside the span of the curve, and is not extrapolated."""


class VolatilityError(RatesEngineError):
    """The volatility input or surface cannot support the calculation."""

    exit_code = 1


class VolUnitsError(VolatilityError):
    """A volatility was quoted in units that do not match what was asked for.

    Either a relative volatility was handed to something expecting an absolute
    one, which is not a scaling away, or the magnitude is implausible for the
    units declared. Both are the same mistake wearing different clothes, and
    both are wrong by orders of magnitude rather than by a little.
    """


class ShiftRequiredError(VolatilityError):
    """A lognormal model was asked for at a forward at or below zero.

    Black and unshifted SABR take the logarithm of the forward. Use a shifted
    model, or quote normal volatility, which is what the market does.
    """

    exit_code = 2


class MissingForwardError(VolatilityError):
    """A period of a cap or floor has no forward on the projection curve.

    Never extrapolated: the missing caplet would be priced off a rate the
    curve does not imply, and the cap would report as complete.
    """

    exit_code = 2


class CalibrationError(RatesEngineError):
    """A model could not be fitted to the quotes supplied.

    The message says which quotes and what the fit achieved, because a
    calibration that silently returns its starting point is worse than one
    that refuses.
    """

    exit_code = 2


class ExpansionBreakdownError(VolatilityError):
    """An asymptotic expansion returned a volatility at or below zero.

    Not a coding error and not a bad input: it is the expansion reporting
    that it has left the region where it approximates anything. Hagan's SABR
    implied volatility carries an ``O(nu^2 T)`` correction that can drive the
    result negative at long expiries, high vol-of-vol and strikes far into
    the wing. The honest response is to refuse, because a negative volatility
    prices nothing, and to say where the boundary was crossed.
    """

    exit_code = 2


class SliceNotQuotedError(VolatilityError):
    """The volatility cube holds no quotes at that expiry and tenor.

    Filling an unquoted strike inside a quoted smile is a model fitted to
    data. Filling a whole missing slice would be a model fitted to a
    *different* slice, which is a larger claim than v2 makes: there is no
    interpolation across expiry or tenor.
    """

    exit_code = 2
