"""PRD-001 AC-2.1: the FRED provider, exercised without touching the network.

The provider is the only code here that dials out, so the test stubs the one
call it makes. Doing it any other way would make the suite depend on a
third party being up, which is the thing the fixtures exist to avoid.
"""

from __future__ import annotations

import io
from datetime import date

import pytest

from rates_engine.core.errors import InsufficientDataError, UnsupportedConventionError
from rates_engine.core.evidence import DataQuality
from rates_engine.market.providers import fred
from rates_engine.market.providers.file import load_series_csv

#: FRED's download layout: ``observation_date`` then a column named after the
#: series. ``{series}`` is filled with the identifier the stub was asked for,
#: as FRED itself does.
CSV = "observation_date,{series}\n2026-01-13,4.31\n2026-01-14,4.32\n2026-01-15,.\n"


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
        return _Response(CSV.format(series=url.rsplit("id=", 1)[1]))

    monkeypatch.setattr("urllib.request.urlopen", fake)
    return calls


def _serve(monkeypatch, payload: str) -> None:
    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=None: _Response(payload))


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


def test_every_spelling_of_missing_is_skipped(monkeypatch):
    # The same markers as the file provider, so that a download and a file
    # cannot disagree about which days exist.
    _serve(
        monkeypatch,
        "observation_date,SOFR\n2026-01-12,4.31\n2026-01-13,nan\n2026-01-14,NaN\n"
        "2026-01-15,N/A\n2026-01-16,na\n2026-01-20,4.33\n",
    )
    series = fred.fetch_series("SOFR")
    assert series.dates == (date(2026, 1, 12), date(2026, 1, 20))


class TestUnits:
    """A download and the same bytes saved to disk are the same series."""

    def test_a_saved_download_reads_the_same_as_the_download(self, stub_urlopen, tmp_path):
        # The file provider's docstring promised this and did not keep it:
        # FRED's CSV read from disk gave 0 observations with the default
        # columns and values a hundred times too large, marked observed, with
        # the columns named.
        downloaded = fred.fetch_series("SOFR")
        path = tmp_path / "SOFR.csv"
        path.write_text(CSV.format(series="SOFR"), encoding="utf-8")
        for loaded in (
            load_series_csv(path, "SOFR"),
            load_series_csv(path, "SOFR", value_column="SOFR", date_column="observation_date"),
        ):
            assert loaded.dates == downloaded.dates
            assert loaded.values == downloaded.values
        assert downloaded.values == (pytest.approx(0.0431), pytest.approx(0.0432))

    def test_the_sofr_index_is_an_index_not_a_percentage(self, monkeypatch):
        # FRED publishes SOFRINDEX as an index level (2018-04-02 = 1), not in
        # percent; dividing it by 100 like the rates made it 1% of itself.
        _serve(monkeypatch, "observation_date,SOFRINDEX\n2026-01-13,1.23456789\n")
        assert fred.fetch_series("SOFRINDEX").values == (1.23456789,)

    def test_a_series_of_unknown_units_is_refused_until_they_are_stated(self, monkeypatch):
        # DEXMXUS is pesos per dollar. Dividing every FRED series by 100 read
        # 17.5 as 0.175, marked observed.
        _serve(monkeypatch, "observation_date,DEXMXUS\n2026-01-13,17.5\n")
        with pytest.raises(UnsupportedConventionError) as excinfo:
            fred.fetch_series("DEXMXUS")
        assert "percent=" in str(excinfo.value)
        assert fred.fetch_series("DEXMXUS", percent=False).values == (17.5,)
        assert fred.fetch_series("DEXMXUS", percent=True).values == (pytest.approx(0.175),)

    def test_an_empty_response_refuses_rather_than_returning_nothing(self, monkeypatch):
        _serve(monkeypatch, "observation_date,SOFR\n2026-01-13,.\n")
        with pytest.raises(InsufficientDataError, match="SOFR"):
            fred.fetch_series("SOFR")

    def test_a_response_for_another_series_refuses(self, monkeypatch):
        _serve(monkeypatch, "observation_date,DGS2\n2026-01-13,4.05\n")
        with pytest.raises(ValueError, match="DGS2"):
            fred.fetch_series("SOFR")


def test_it_calls_the_documented_endpoint(stub_urlopen):
    fred.fetch_series("DGS2")
    assert stub_urlopen == ["https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS2"]


def test_treasuries_arrive_classified_as_proxies(stub_urlopen):
    series = fred.fetch_series("DGS2")
    assert series.provenance.data_quality is DataQuality.PROXY
    assert series.provenance.instrument_kind == "treasury_par_yield"


#: The six series PRD-001 AC-2.1 names.
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
