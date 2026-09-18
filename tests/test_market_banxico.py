"""The Banxico SIE provider: parsing tested, the endpoint never reached.

This build has never spoken to banxico.org.mx. The egress proxy returns 403
for it, which is the same organisation policy that left v1's CME goldens
skipped and the MXN conventions unresolved. So the provider is written and
its parsing is tested against recorded payload shapes, and the network path
is exercised only for the two things that do not need the network: that the
token is required, and that importing the module opens nothing.

The alternative — skipping the whole file until Banxico is reachable —
would leave the parsing untested, and the parsing is where the bugs are.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from rates_engine.errors import ConfigurationError, InsufficientDataError
from rates_engine.evidence import DataQuality
from rates_engine.market.providers import banxico


def _payload(rows, series_id="SF43783"):
    return json.dumps(
        {
            "bmx": {
                "series": [
                    {"idSerie": series_id, "titulo": "TIIE de Fondeo", "datos": rows}
                ]
            }
        }
    )


ROWS = [
    {"fecha": "15/09/2026", "dato": "9.5000"},
    {"fecha": "16/09/2026", "dato": "9.4800"},
    {"fecha": "17/09/2026", "dato": "9.4900"},
]


class TestParsing:
    """The part that has bugs, tested without the part that has a firewall."""

    def test_it_reads_dates_and_values(self):
        series = banxico.parse_sie_payload(_payload(ROWS), "SF43783")
        assert series.dates == (date(2026, 9, 15), date(2026, 9, 16), date(2026, 9, 17))
        assert series.values[0] == pytest.approx(0.095)

    def test_dates_are_day_first_as_sie_writes_them(self):
        """`03/09/2026` is the third of September, not the ninth of March.
        Reading it the American way would shift every fixing by months and
        still parse."""
        series = banxico.parse_sie_payload(
            _payload([{"fecha": "03/09/2026", "dato": "9.50"}]), "SF43783"
        )
        assert series.dates == (date(2026, 9, 3),)

    def test_percent_is_divided_into_a_decimal(self):
        series = banxico.parse_sie_payload(_payload(ROWS), "SF43783")
        assert all(0.05 < v < 0.15 for v in series.values)

    def test_a_series_already_in_decimals_can_say_so(self):
        series = banxico.parse_sie_payload(
            _payload([{"fecha": "15/09/2026", "dato": "0.095"}]), "SF43783", percent=False
        )
        assert series.values == (pytest.approx(0.095),)

    def test_thousands_separators_survive(self):
        series = banxico.parse_sie_payload(
            _payload([{"fecha": "15/09/2026", "dato": "1,850.50"}]), "SF43783", percent=False
        )
        assert series.values == (pytest.approx(1850.50),)

    def test_unavailable_observations_are_dropped_not_interpolated(self):
        rows = [*ROWS, {"fecha": "18/09/2026", "dato": "N/E"}]
        series = banxico.parse_sie_payload(_payload(rows), "SF43783")
        assert len(series.dates) == 3

    def test_results_come_back_in_date_order(self):
        shuffled = [ROWS[2], ROWS[0], ROWS[1]]
        series = banxico.parse_sie_payload(_payload(shuffled), "SF43783")
        assert list(series.dates) == sorted(series.dates)

    def test_a_response_without_the_requested_series_refuses(self):
        """No falling back to the first block. That returned a different
        series under the requested identifier, marked OBSERVED, with nothing
        recorded — and since this module deliberately does not know which
        identifier is which benchmark, a guessed ID is the expected case."""
        other = json.dumps(
            {"bmx": {"series": [{"idSerie": "SF331451", "datos": ROWS}]}}
        )
        with pytest.raises(InsufficientDataError) as excinfo:
            banxico.parse_sie_payload(other, "SF43783")
        message = str(excinfo.value)
        assert "SF331451" in message
        assert "wearing your label" in message

    def test_the_right_series_is_picked_from_a_multi_series_response(self):
        document = json.dumps(
            {
                "bmx": {
                    "series": [
                        {"idSerie": "SF00001", "datos": [{"fecha": "15/09/2026", "dato": "1.0"}]},
                        {"idSerie": "SF43783", "datos": ROWS},
                    ]
                }
            }
        )
        series = banxico.parse_sie_payload(document, "SF43783")
        assert len(series.dates) == 3


class TestRefusals:
    """What it will not do quietly."""

    def test_a_response_of_the_wrong_shape_refuses(self):
        with pytest.raises(InsufficientDataError, match="not a SIE series document"):
            banxico.parse_sie_payload('{"error": "nope"}', "SF43783")

    def test_html_refuses_rather_than_crashing_obscurely(self):
        """What a proxy returns on a 403, which is what this environment
        gets. The message shows the start of the body so the reader can see
        it is a block page and not data."""
        with pytest.raises(InsufficientDataError) as excinfo:
            banxico.parse_sie_payload("<html><body>403 Forbidden</body></html>", "SF43783")
        assert "403 Forbidden" in str(excinfo.value)

    def test_an_empty_series_refuses_rather_than_returning_nothing(self):
        with pytest.raises(InsufficientDataError, match="no observations"):
            banxico.parse_sie_payload(_payload([]), "SF43783")

    def test_a_series_of_only_unavailable_values_refuses(self):
        rows = [{"fecha": "15/09/2026", "dato": "N/E"}, {"fecha": "16/09/2026", "dato": "N/A"}]
        with pytest.raises(InsufficientDataError, match="marked unavailable"):
            banxico.parse_sie_payload(_payload(rows), "SF43783")

    def test_it_says_an_empty_series_is_not_one_to_interpolate_over(self):
        with pytest.raises(InsufficientDataError) as excinfo:
            banxico.parse_sie_payload(_payload([]), "SF43783")
        assert "not an empty series to interpolate over" in str(excinfo.value)


class TestTheToken:
    """Never in the repository, never defaulted, never a 401 from a server."""

    def test_a_missing_token_refuses_before_any_request(self, monkeypatch):
        monkeypatch.delenv(banxico.TOKEN_ENV_VAR, raising=False)
        with pytest.raises(ConfigurationError) as excinfo:
            banxico.fetch_series("SF43783")
        assert banxico.TOKEN_ENV_VAR in str(excinfo.value)

    def test_the_refusal_says_where_to_get_one(self, monkeypatch):
        monkeypatch.delenv(banxico.TOKEN_ENV_VAR, raising=False)
        with pytest.raises(ConfigurationError) as excinfo:
            banxico.fetch_snapshot(date(2026, 9, 16), ("SF43783",))
        message = str(excinfo.value)
        assert "SieAPIRest" in message
        assert "never read from this repository" in message

    def test_an_empty_token_is_not_a_token(self, monkeypatch):
        monkeypatch.setenv(banxico.TOKEN_ENV_VAR, "")
        with pytest.raises(ConfigurationError):
            banxico.fetch_series("SF43783")

    def test_the_token_comes_only_from_the_caller_or_the_environment(self):
        """The way a secret gets committed is a fallback nobody removed, so
        the check is on the code path rather than on a string search: the
        module reads the environment variable and the argument, and has no
        other source."""
        from pathlib import Path

        source = Path(banxico.__file__).read_text(encoding="utf-8")
        assert source.count("os.environ") == 1
        assert "os.environ.get(TOKEN_ENV_VAR)" in source
        for smell in ("Path.home()", " open(", "read_text", ".netrc", "config.json"):
            assert smell not in source, smell

    def test_the_token_never_reaches_the_url(self):
        """A token in a query string ends up in server logs and in browser
        history. SIE takes it in a header, and this uses the header."""
        assert "token" not in banxico.BANXICO_SIE_URL.lower()


class TestProvenance:
    """Where the number came from, and what this package does not know."""

    def test_it_is_sourced_to_banxico(self):
        series = banxico.parse_sie_payload(_payload(ROWS), "SF43783")
        assert series.provenance.source == "banxico"

    def test_applying_the_percent_convention_marks_the_series_assumed(self):
        """That SIE quotes in percent is an inference from convention — this
        build has never reached the endpoint. A wrong guess is a
        hundredfold error in every fixing, so the division is not free."""
        series = banxico.parse_sie_payload(_payload(ROWS), "SF43783")
        assert series.provenance.data_quality is DataQuality.ASSUMED
        assert "never confirmed" in (series.provenance.notes or "")

    def test_stating_the_units_yourself_is_observed(self):
        series = banxico.parse_sie_payload(_payload(ROWS), "SF43783", percent=False)
        assert series.provenance.data_quality is DataQuality.OBSERVED

    def test_the_percent_assumption_is_recorded_with_the_others(self):
        from rates_engine.curves.mxn import UNRESOLVED_MXN

        assert "banxico_quotes_in_percent" in {name for name, _ in UNRESOLVED_MXN}

    def test_it_records_that_the_benchmark_mapping_is_not_known_here(self):
        """The SIE identifier for each benchmark is exactly what the
        research gate could not establish, so the provider declines to map
        one to the other and says where that gap is recorded."""
        series = banxico.parse_sie_payload(_payload(ROWS), "SF43783")
        assert "UNRESOLVED_MXN" in (series.provenance.notes or "")

    def test_the_module_maps_no_benchmark_to_any_identifier(self):
        """Writing such a mapping would mean inventing it."""
        from pathlib import Path

        source = Path(banxico.__file__).read_text(encoding="utf-8")
        assert "TIIEBenchmark" not in source
        assert "SF43783" not in source


class TestNoImportTimeNetwork:
    """Same rule as the FRED provider."""

    def test_urllib_is_imported_inside_the_call(self):
        from pathlib import Path

        source = Path(banxico.__file__).read_text(encoding="utf-8")
        for line in source.splitlines():
            if line.startswith(("import urllib", "from urllib")):
                pytest.fail(f"module-level urllib import: {line!r}")

    def test_importing_it_opens_no_socket(self):
        import subprocess
        import sys

        done = subprocess.run(
            [
                sys.executable,
                "-c",
                "import socket\n"
                "def boom(*a, **k):\n"
                "    raise AssertionError('socket opened at import')\n"
                "socket.socket = boom\n"
                "import rates_engine.market.providers.banxico\n"
                "print('clean')",
            ],
            capture_output=True,
            text=True,
        )
        assert done.returncode == 0, done.stderr
        assert "clean" in done.stdout
