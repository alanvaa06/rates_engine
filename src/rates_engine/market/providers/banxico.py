"""Fetch series from Banxico's SIE, when a token and a route to it exist.

Same discipline as the FRED provider: the network call and the standard
library import that performs it are both inside the function, so importing
``rates_engine`` opens no socket.

**Two things this provider cannot supply, and says so.**

The *token*. Banxico's SIE requires a free API token per caller. It is
never read from the repository and never defaulted — it is passed in or
read from ``BANXICO_TOKEN`` in the environment, and its absence is a named
refusal rather than a 401 from a server.

The *series identifiers*. PRD-003's research gate could not reach
banxico.org.mx (403 at the egress proxy), so the SIE identifiers for TIIE
28 and TIIE de Fondeo are not known here. There is no mapping in this
module from a benchmark to a series ID, because writing one would mean
inventing it. The caller passes the identifier they looked up, and
``rates_engine.curves.mxn.UNRESOLVED_MXN`` records that the mapping is
missing.

So this is a working client for an endpoint this build has never reached.
That is stated rather than hidden: :func:`fetch_series` has never been run
against the live service from here, and the tests exercise the parsing
against a recorded payload shape rather than pretending otherwise.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime
from typing import Any

from rates_engine.errors import ConfigurationError, InsufficientDataError
from rates_engine.evidence import DataQuality, Provenance
from rates_engine.market.snapshot import MarketSnapshot, Series

__all__ = [
    "BANXICO_SIE_URL",
    "TOKEN_ENV_VAR",
    "parse_sie_payload",
    "fetch_series",
    "fetch_snapshot",
]

BANXICO_SIE_URL = (
    "https://www.banxico.org.mx/SieAPIRest/service/v1/series/{series_id}/datos"
)
"""The SIE series endpoint. One or more series per request, token in a header."""

TOKEN_ENV_VAR = "BANXICO_TOKEN"
"""Environment variable the token is read from when it is not passed in.

Never a file in the repository, and never a default. A token committed once
is a token to rotate.
"""


def _token(explicit: str | None) -> str:
    token = explicit if explicit is not None else os.environ.get(TOKEN_ENV_VAR)
    if not token:
        raise ConfigurationError(
            "Banxico's SIE needs a free API token per caller. Pass it as `token=`, or "
            f"put it in the {TOKEN_ENV_VAR} environment variable. It is never read "
            "from this repository and never defaulted: a token committed once is a "
            "token to rotate. Register at banxico.org.mx/SieAPIRest."
        )
    return token


def parse_sie_payload(payload: str, series_id: str, *, percent: bool = True) -> Series:
    """Turn a SIE JSON response into a :class:`Series`.

    Separated from the fetch so the parsing is testable without the network
    — which matters here more than usual, because this build has never
    reached the live endpoint.

    Args:
        payload: The response body.
        series_id: The identifier requested, used when the response carries
            several and to label the provenance.
        percent: Divide by 100. **Assumed, not verified**: this build has
            never reached SIE, so that it quotes rates in percent as FRED
            does is an inference from convention. A wrong guess is a
            hundredfold error in every fixing, so when it is applied the
            series comes back marked ``ASSUMED`` rather than ``OBSERVED``.

    Returns:
        The series. Marked ``OBSERVED`` when the caller states the units,
        and ``ASSUMED`` when the percent convention was applied on this
        module's unverified say-so.

    Raises:
        InsufficientDataError: The payload has no observations for the
            series, or is not the shape SIE documents.
    """
    try:
        document: dict[str, Any] = json.loads(payload)
        blocks = document["bmx"]["series"]
    except (ValueError, KeyError, TypeError) as exc:
        raise InsufficientDataError(
            f"the response is not a SIE series document: {payload[:200]!r}"
        ) from exc

    chosen = next((b for b in blocks if str(b.get("idSerie", "")) == series_id), None)
    if chosen is None:
        # No falling back to the first block. That fallback returned a
        # different series under the requested identifier, marked OBSERVED,
        # with nothing recorded — an FX rate labelled as the TIIE series
        # someone asked for. Since this module deliberately does not know
        # which identifier is which benchmark, a caller passing a guessed
        # one is the expected case, which is exactly when a silent
        # substitution does the most damage.
        raise InsufficientDataError(
            f"SIE returned no block for {series_id}; it returned "
            f"{[str(b.get('idSerie', '?')) for b in blocks]}. Returning one of those "
            "under the identifier you asked for would be a different series wearing "
            "your label."
        )
    if not chosen.get("datos"):
        raise InsufficientDataError(
            f"SIE returned no observations for {series_id}. A series that exists but "
            "has no data in the window is not an empty series to interpolate over."
        )

    rows: list[tuple[date, float]] = []
    for point in chosen["datos"]:
        raw = str(point.get("dato", "")).strip().replace(",", "")
        if raw in {"", "N/E", "N/A"}:
            continue
        day, month, year = (int(part) for part in str(point["fecha"]).split("/"))
        rows.append((date(year, month, day), float(raw) / (100.0 if percent else 1.0)))
    if not rows:
        raise InsufficientDataError(
            f"every observation SIE returned for {series_id} was marked unavailable"
        )
    rows.sort(key=lambda item: item[0])

    return Series(
        series_id=series_id,
        dates=tuple(d for d, _ in rows),
        values=tuple(v for _, v in rows),
        provenance=Provenance(
            source="banxico",
            series_id=series_id,
            retrieved_at=datetime.now(UTC),
            instrument_kind="mxn_rate",
            data_quality=DataQuality.ASSUMED if percent else DataQuality.OBSERVED,
            notes=(
                "Banxico SIE. Which benchmark this identifier corresponds to is the "
                "caller's knowledge, not this package's: the SIE identifiers for TIIE "
                "28 and TIIE de Fondeo are recorded as unresolved in "
                "rates_engine.curves.mxn.UNRESOLVED_MXN."
                + (
                    " Values were divided by 100 on the assumption that SIE quotes in "
                    "percent, which this build has never confirmed against the live "
                    "endpoint; that is why this is ASSUMED rather than OBSERVED."
                    if percent
                    else ""
                )
            ),
        ),
    )


def fetch_series(
    series_id: str, *, token: str | None = None, timeout: float = 30.0, percent: bool = True
) -> Series:
    """Download one SIE series.

    Args:
        series_id: The SIE identifier. This package does not map benchmarks
            to identifiers, because the research gate could not establish
            them; pass the one you looked up.
        token: The API token, or ``None`` to read :data:`TOKEN_ENV_VAR`.
        timeout: Socket timeout in seconds.
        percent: Whether SIE quotes this series in percent.

    Returns:
        The :class:`Series`.

    Raises:
        ConfigurationError: No token was available.
        InsufficientDataError: The response held no usable observations.
        OSError: The request failed. Left unwrapped, as in the FRED
            provider: a network failure is the environment's, and the
            original error says more than a rewrapped one would. From this
            build environment it will be a 403 at the egress proxy.
    """
    from urllib.request import Request, urlopen

    request = Request(  # noqa: S310 - fixed https host
        BANXICO_SIE_URL.format(series_id=series_id),
        headers={"Bmx-Token": _token(token), "Accept": "application/json"},
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310
        body = response.read().decode("utf-8")
    return parse_sie_payload(body, series_id, percent=percent)


def fetch_snapshot(
    as_of: date,
    series_ids: tuple[str, ...],
    *,
    token: str | None = None,
    timeout: float = 30.0,
) -> MarketSnapshot:
    """Download several SIE series into one snapshot.

    Args:
        as_of: Valuation date the snapshot describes.
        series_ids: SIE identifiers.
        token: The API token, or ``None`` to read the environment.
        timeout: Per-request socket timeout in seconds.

    Returns:
        The snapshot.

    Raises:
        ConfigurationError: No token was available.
        OSError: Any request failed.
    """
    resolved = _token(token)
    return MarketSnapshot(
        as_of=as_of,
        series={
            sid: fetch_series(sid, token=resolved, timeout=timeout) for sid in series_ids
        },
    )
