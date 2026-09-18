"""AC-2.1: the FRED provider, exercised without touching the network.

The provider is the only code here that dials out, so the test stubs the one
call it makes. Doing it any other way would make the suite depend on a
third party being up, which is the thing the fixtures exist to avoid.
"""

from __future__ import annotations

import io
from datetime import date

import pytest

from rates_engine.evidence import DataQuality
from rates_engine.market.providers import fred

CSV = "observation_date,SOFR\n2026-01-13,4.31\n2026-01-14,4.32\n2026-01-15,.\n"


class _Response(io.StringIO):
    def read(self, *args):  # type: ignore[override]
        return super().read(*args).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def stub_urlopen(monkeypatch):
    calls: list[str] = []

    def fake(url, timeout=None):
        calls.append(url)
        return _Response(CSV)

    monkeypatch.setattr("urllib.request.urlopen", fake)
    return calls


def test_it_fetches_and_records_provenance(stub_urlopen):
    series = fred.fetch_series("SOFR")
    assert len(series) == 2
    assert series.provenance.source == "fred"
    assert series.provenance.series_id == "SOFR"
    assert series.provenance.retrieved_at is not None
    assert series.provenance.instrument_kind == "overnight_fixing"


def test_percent_quotes_become_decimals(stub_urlopen):
    series = fred.fetch_series("SOFR")
    assert series.get(date(2026, 1, 13)) == pytest.approx(0.0431)
    assert series.get(date(2026, 1, 14)) == pytest.approx(0.0432)


def test_a_non_publication_marker_is_skipped_not_read_as_zero(stub_urlopen):
    series = fred.fetch_series("SOFR")
    assert series.get(date(2026, 1, 15)) is None


def test_it_calls_the_documented_endpoint(stub_urlopen):
    fred.fetch_series("DGS2")
    assert stub_urlopen == ["https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS2"]


def test_treasuries_arrive_classified_as_proxies(stub_urlopen):
    series = fred.fetch_series("DGS2")
    assert series.provenance.data_quality is DataQuality.PROXY
    assert series.provenance.instrument_kind == "treasury_par_yield"


#: The six series AC-2.1 names.
AC_2_1_SERIES = ("SOFR", "SOFR30DAYAVG", "SOFR90DAYAVG", "SOFRINDEX", "EFFR", "DGS2")


def test_a_snapshot_fetches_every_series_ac_2_1_names(stub_urlopen):
    snapshot = fred.fetch_snapshot(date(2026, 1, 15), AC_2_1_SERIES)
    assert set(snapshot.series) == set(AC_2_1_SERIES)
    assert len(stub_urlopen) == len(AC_2_1_SERIES)
    assert snapshot.worst_quality() is DataQuality.PROXY


def test_every_one_of_the_six_carries_the_three_provenance_fields(stub_urlopen):
    snapshot = fred.fetch_snapshot(date(2026, 1, 15), AC_2_1_SERIES)
    for name, provenance in snapshot.provenance.items():
        assert provenance.source == "fred", name
        assert provenance.series_id == name
        assert provenance.retrieved_at is not None, name


def test_the_series_ac_2_1_names_are_all_classifiable():
    from rates_engine.market.classify import classify_series

    for name in ("SOFR", "SOFR30DAYAVG", "SOFR90DAYAVG", "SOFRINDEX", "EFFR", "DGS2"):
        kind, _, _ = classify_series(name)
        assert kind != "unclassified", name
