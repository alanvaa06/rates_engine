"""Every key a command reaches is one it reads, one documented, or a refusal by name.

The review of 2026-10-15 ran four misspelt keys through ``rateng`` and got
four exit codes of zero:

- ``swap.notionl``: the swap fell back to the 1,000,000 default and the PV
  read 921.99 instead of 92,199.33, a hundredth of the trade asked for;
- ``curve.convexty``: the convexity adjustment silently switched off and the
  PV moved from 92,199.33 to 114,866.89;
- ``risk.key_tenor``: ``key_rate_dv01`` came back ``null``;
- ``hedge.shock_bps``: the default eight-point grid replaced the two shocks
  asked for.

Nothing in any of those documents said a key had been ignored, and an agent
calling the MCP tool has no other way to find out. The fix is generic rather
than a list per command, because the command bodies are where new keys
appear: the dispatcher wraps the config in a mapping that records what was
read, and anything the command reached but neither read nor recognises as
the documented vocabulary is a :class:`ConfigurationError` naming its path.

The expected values here are refusals and paths, not numbers, so the
reference is the key the test misspelt rather than anything the engine
computes.
"""

from __future__ import annotations

import copy
import inspect
from datetime import timedelta

import pytest

from rates_engine.app import commands
from rates_engine.app.config import CONFIG_SCHEMA, track_reads
from rates_engine.conventions.schedule import imm_date, next_imm_on_or_after
from rates_engine.core.errors import ConfigurationError

CONFIG_COMMANDS = ("bootstrap", "price", "hedge", "fx-forward", "hedge-structures")


def _full_config(as_of) -> dict:
    """One document carrying every key the schema documents, for every command.

    A treasurer keeps one file and runs several commands on it; this is that
    file with nothing left out, so a key one command does not read but
    another does is exercised in every combination.
    """
    start = imm_date(2026, 3)
    futures = []
    period = start
    for index in range(9):
        following = next_imm_on_or_after(period + timedelta(days=1))
        futures.append(
            {
                "label": f"SR3-{index + 1}",
                "start": period.isoformat(),
                "end": following.isoformat(),
                "price": 96.0,
            }
        )
        period = following
    return {
        "as_of": as_of.isoformat(),
        "curve": {
            "stub": {
                "end": start.isoformat(),
                "accrual_factor": 1.0 + 0.04 * ((start - as_of).days / 360.0),
            },
            "futures": futures,
            "par_swaps": [
                {
                    "label": "OIS3Y",
                    "effective": as_of.isoformat(),
                    "maturity": "2029-01-15",
                    "rate": 0.041,
                    "frequency_months": 12,
                    "payment_lag_days": 2,
                    "source": "file",
                    "instrument_kind": "ois_par",
                    "data_quality": "observed",
                }
            ],
            "convexity": {"model": "ho_lee", "sigma": 0.01, "kappa": None},
            "long_end_source": None,
            "strict": True,
        },
        "swap": {
            "effective": start.isoformat(),
            "maturity": period.isoformat(),
            "fixed_rate": 0.04,
            "notional": 100_000_000.0,
            "side": "payer",
            "frequency_months": 12,
            "payment_lag_days": 2,
            "index": "SOFR",
        },
        "risk": {"key_tenors": [0.5, 1.0, 2.0]},
        "hedge": {"contract_dv01": 25.0, "shocks_bp": [-100, -10, 10, 100]},
        "fx": {
            "spot": 18.50,
            "delivery": "2026-12-15",
            "r_domestic": 0.0950,
            "r_foreign": 0.0420,
            "basis_bp": -25.0,
            "volatility": 0.115,
        },
        "exposure": {
            "amount": 1_000_000.0,
            "direction": "payable",
            "hedge_ratio": 0.8,
            "correlation": -0.30,
            "foreign_asset_volatility": 0.10,
            "proposed_instrument": "collar",
        },
        "hedge_program": {
            "name": "treasury_2026",
            "target_hedge_ratio": 0.80,
            "discretion_band": 0.10,
            "rebalance_frequency": "monthly",
            "allowed_instruments": ["forward", "collar"],
        },
    }


@pytest.fixture
def full(as_of) -> dict:
    return _full_config(as_of)


def _refusal(name: str, config: dict) -> str:
    with pytest.raises(ConfigurationError) as excinfo:
        commands.COMMANDS[name](config)
    assert excinfo.value.exit_code == 1
    return str(excinfo.value)


class TestTheFourTyposAreRefused:
    """The review's four cases, each of which exited 0 on main."""

    def test_a_misspelt_notional(self, full):
        full["swap"]["notionl"] = full["swap"].pop("notional")
        message = _refusal("price", full)
        assert "swap.notionl" in message
        assert "did you mean notional?" in message

    def test_a_misspelt_convexity_block(self, full):
        full["curve"]["convexty"] = full["curve"].pop("convexity")
        message = _refusal("price", full)
        assert "curve.convexty" in message
        assert "did you mean convexity?" in message

    def test_a_misspelt_key_tenors(self, full):
        full["risk"] = {"key_tenor": [0.5, 1.0, 2.0]}
        message = _refusal("price", full)
        assert "risk.key_tenor" in message
        assert "did you mean key_tenors?" in message

    def test_a_misspelt_shock_grid(self, full):
        full["hedge"] = {"shock_bps": [-200, 200]}
        message = _refusal("hedge", full)
        assert "hedge.shock_bps" in message
        assert "did you mean shocks_bp?" in message

    @pytest.mark.parametrize("name", ["bootstrap", "price", "hedge"])
    def test_the_convexity_typo_is_refused_by_every_command_that_builds_the_curve(
        self, name, full
    ):
        full["curve"]["convexty"] = full["curve"].pop("convexity")
        assert "curve.convexty" in _refusal(name, full)


class TestTheMessage:
    """Says where, says what was probably meant, and says what is accepted."""

    def test_a_key_inside_a_list_entry_is_named_by_its_index(self, full):
        full["curve"]["futures"][2]["settle_note"] = "from the CME page"
        message = _refusal("bootstrap", full)
        assert "curve.futures[2].settle_note" in message

    def test_a_typo_that_hides_a_required_key_names_both(self, full):
        full["curve"]["futures"][2]["prise"] = full["curve"]["futures"][2].pop("price")
        message = _refusal("bootstrap", full)
        assert "curve.futures[2].price is missing" in message
        assert "curve.futures[2].prise (did you mean price?)" in message

    def test_an_unknown_top_level_key(self, full):
        full["rsik"] = full.pop("risk")
        message = _refusal("price", full)
        assert "rsik (did you mean risk?)" in message

    def test_a_key_at_the_wrong_level_says_where_it_belongs(self, full):
        full["notional"] = full["swap"].pop("notional")
        message = _refusal("price", full)
        assert "notional (it belongs under swap)" in message

    def test_every_unknown_key_is_listed_at_once(self, full):
        full["swap"]["fixed_rte"] = 0.09
        full["risk"]["key_tenor"] = [5.0]
        message = _refusal("price", full)
        assert "swap.fixed_rte" in message
        assert "risk.key_tenor" in message

    def test_it_lists_what_the_block_accepts(self, full):
        full["swap"]["notionl"] = 5.0
        message = _refusal("price", full)
        assert "Accepted in swap: effective, fixed_rate, frequency_months, index, " in message

    def test_it_imitates_the_hedge_programme_refusal(self, full):
        # hedging/program.py:155-162 already refuses unknown keys; the CLI
        # config is held to the same sentence rather than a second dialect.
        full["swap"]["notionl"] = 5.0
        message = _refusal("price", full)
        assert message.startswith("unknown key(s) in the price config:")
        assert "quietly ignored" in message

    def test_it_points_at_describe(self, full):
        full["swap"]["notionl"] = 5.0
        assert "rateng describe --json" in _refusal("price", full)

    def test_the_refusal_is_chained_to_the_failure_it_explains(self, full):
        full["curve"]["futures"][2]["prise"] = full["curve"]["futures"][2].pop("price")
        with pytest.raises(ConfigurationError) as excinfo:
            commands.COMMANDS["bootstrap"](full)
        assert isinstance(excinfo.value.__cause__, KeyError)


class TestNoFalsePositives:
    """A key another command reads, or one that does not apply here, is not a typo."""

    @pytest.mark.parametrize("name", CONFIG_COMMANDS)
    def test_every_command_accepts_the_full_config(self, name, full):
        assert commands.COMMANDS[name](full)["schema_version"] == "1.0"

    def test_bootstrap_ignores_the_blocks_it_does_not_use(self, full):
        # swap, risk and hedge are price's and hedge's; the README's own
        # workflow runs all three commands on one file.
        assert commands.COMMANDS["bootstrap"](full)["command"] == "bootstrap"

    def test_price_accepts_curve_strict_which_only_bootstrap_reads(self, full):
        full["curve"]["strict"] = False
        assert commands.COMMANDS["price"](full)["command"] == "price"

    def test_fx_forward_accepts_the_volatility_hedge_structures_reads(self, full):
        assert commands.COMMANDS["fx-forward"](full)["result_type"] == "FXForwardResult"

    def test_a_proposed_instrument_without_a_programme_is_not_refused(self, full):
        del full["hedge_program"]
        payload = commands.COMMANDS["hedge-structures"](full)
        assert payload["result_type"] == "StructureComparison"

    def test_underscore_keys_are_comments_at_any_depth(self, full):
        full["_comment"] = "desk config, 2026-01-15 close"
        full["swap"]["_why"] = "the 2y payer the desk is quoting"
        full["curve"]["futures"][0]["_source"] = "CME settlement page"
        full["curve"]["convexity"]["_note"] = {"sigma": "realised, 60d"}
        assert commands.COMMANDS["price"](full)["command"] == "price"

    def test_a_key_commented_out_with_an_underscore_is_not_read(self, full):
        # The other half of the convention: a commented-out key is off.
        baseline = commands.COMMANDS["price"](copy.deepcopy(full))
        full["swap"]["_notional"] = full["swap"].pop("notional")
        commented = commands.COMMANDS["price"](full)
        assert commented["measures"]["pv"]["value"] == pytest.approx(
            baseline["measures"]["pv"]["value"] / 100.0
        )

    @pytest.mark.parametrize("name", ["describe", "list-instruments"])
    def test_the_configless_commands_ignore_whatever_they_are_given(self, name):
        assert commands.COMMANDS[name]({"anything": {"at": "all"}})["evidence"] is None

    def test_the_caller_s_mapping_is_not_modified(self, full):
        before = copy.deepcopy(full)
        commands.COMMANDS["hedge-structures"](full)
        assert full == before
        assert type(full["fx"]) is dict


class TestMissingKeys:
    """A required key that is absent is a sentence, not a bare KeyError."""

    def test_a_missing_futures_price_names_the_path(self, full):
        del full["curve"]["futures"][0]["price"]
        message = _refusal("bootstrap", full)
        assert "curve.futures[0].price is missing, and bootstrap needs it" in message

    def test_a_missing_block_is_named(self, full):
        del full["swap"]
        assert "swap is missing, and price needs it" in _refusal("price", full)

    def test_it_says_what_the_key_holds(self, full):
        del full["curve"]["futures"][0]["price"]
        assert "100 minus the rate in percent" in _refusal("bootstrap", full)

    def test_a_key_error_the_command_raises_itself_is_left_alone(self, monkeypatch, full):
        # Only a lookup the tracker answered becomes a ConfigurationError; a
        # KeyError from anywhere else is somebody else's bug and keeps its type.
        def broken(config):
            return {}["not-a-config-key"]

        checked = commands._reading_every_key("price", broken)
        monkeypatch.setitem(commands.COMMANDS, "price", checked)
        with pytest.raises(KeyError):
            commands.COMMANDS["price"](full)


class TestTheTracker:
    """Reading is recorded however the code reaches a value."""

    def test_get_getitem_and_membership_are_reads(self):
        tracked = track_reads({"a": 1, "b": 2, "c": 3, "d": 4})
        seen = (tracked["a"], tracked.get("b"), "c" in tracked, "z" in tracked)
        assert seen == (1, 2, True, False)
        assert tracked._read == {"a", "b", "c"}
        assert tracked._asked == {"a", "b", "c", "z"}

    @pytest.mark.parametrize(
        "consume",
        [
            dict,
            lambda block: {**block},
            lambda block: block.copy(),
            copy.deepcopy,
            lambda block: list(block.items()),
            lambda block: list(block.values()),
        ],
        ids=["dict", "unpack", "copy", "deepcopy", "items", "values"],
    )
    def test_copying_a_block_counts_as_reading_all_of_it(self, consume):
        # load_program(dict(block)) is how hedge-structures hands its
        # programme over, and load_program refuses unknown keys itself.
        tracked = track_reads({"a": 1, "b": {"c": 2}})
        consume(tracked)
        assert tracked._read == {"a", "b"}

    def test_listing_the_keys_is_not_reading_them(self):
        tracked = track_reads({"a": 1})
        sorted(tracked)
        list(tracked.keys())
        len(tracked)
        assert tracked._read == set()

    def test_nested_mappings_and_list_entries_are_tracked(self):
        tracked = track_reads({"a": {"b": 1}, "rows": [{"c": 2}, 3]})
        assert type(tracked["a"]) is type(tracked)
        assert type(tracked["rows"][0]) is type(tracked)
        assert tracked["rows"][1] == 3

    def test_it_compares_equal_to_what_it_wraps(self):
        source = {"a": {"b": [{"c": 1}]}}
        assert track_reads(source) == source


def _commands_reads(name: str, config: dict) -> tuple[set[str], set[str]]:
    """The documented paths a command asked for, and the ones it read.

    Paths use ``[]`` for a list entry, so ``curve.futures[].price`` is one path
    however many futures there are.
    """
    from rates_engine.app.config import _entered

    tracked = track_reads(copy.deepcopy(config))
    inspect.unwrap(commands.COMMANDS[name])(tracked)
    asked: set[str] = set()
    read: set[str] = set()
    for _, pattern, node, _ in _entered(tracked):
        prefix = f"{pattern}." if pattern else ""
        asked |= {f"{prefix}{key}" for key in node._asked}
        read |= {f"{prefix}{key}" for key in node._read}
    return asked, read


def _documented(level=CONFIG_SCHEMA, prefix="") -> dict[str, dict]:
    """Every documented path, flattened, with its specification."""
    found: dict[str, dict] = {}
    for key, spec in level.items():
        path = f"{prefix}{key}"
        found[path] = spec
        if "keys" in spec:
            found |= _documented(spec["keys"], f"{path}.")
        if "items" in spec:
            found |= _documented(spec["items"], f"{path}[].")
    return found


def _given(value, prefix="") -> set[str]:
    """Every path present in a plain config."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}{key}"
            found.add(path)
            found |= _given(child, f"{path}.")
    elif isinstance(value, list):
        for child in value:
            found |= _given(child, prefix.removesuffix(".") + "[].")
    return found


class TestDescribeDocumentsTheConfig:
    """``rateng describe --json`` is the shape every refusal points at."""

    def test_describe_carries_the_schema(self):
        payload = commands.COMMANDS["describe"](None)
        assert payload["config"]["keys"] == CONFIG_SCHEMA

    def test_the_fx_block_is_documented(self):
        # builders.py:212-213 sends the caller to describe for this shape.
        fx = CONFIG_SCHEMA["fx"]["keys"]
        assert set(fx) == {"spot", "delivery", "r_domestic", "r_foreign", "basis_bp", "volatility"}

    @pytest.mark.parametrize("key", ["r_domestic", "r_foreign"])
    def test_the_fx_rates_say_which_rate_they_are(self, key):
        description = CONFIG_SCHEMA["fx"]["keys"][key]["description"]
        assert "continuously compounded" in description
        assert "ACT/365F" in description

    def test_the_full_config_is_every_documented_key(self, full):
        documented = {
            path for path, spec in _documented().items() if "refused" not in spec
        }
        assert _given(full) == documented

    @pytest.mark.parametrize("name", CONFIG_COMMANDS)
    def test_every_key_a_command_asks_for_is_documented(self, name, full):
        # The drift this catches: a command body starts reading a key that
        # describe does not mention. The runtime check would still accept it
        # (it was read), but describe would be lying by omission, and a
        # second command that does not read it would refuse it as unknown.
        asked, _ = _commands_reads(name, full)
        assert asked - set(_documented()) == set()

    @pytest.mark.parametrize("path", sorted(p for p, s in _documented().items() if "used_by" in s))
    def test_used_by_is_exactly_the_commands_that_read_it(self, path, full):
        readers = {name for name in CONFIG_COMMANDS if path in _commands_reads(name, full)[1]}
        assert readers == set(_documented()[path]["used_by"]), path

    def test_every_description_is_a_sentence(self):
        for path, spec in _documented().items():
            text = spec.get("description") or spec.get("refused") or ""
            assert len(text) > 15, path
