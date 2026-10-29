"""The whole config is checked against the schema before any command computes.

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
calling the MCP tool has no other way to find out. ``require_config``, which
every handler that reads a config calls first, now checks the whole document
against ``CONFIG_SCHEMA``: a key it does not document is refused by path,
with the closest accepted key, whichever command reads the file.

The expected values here are refusals and paths, not numbers, so the
reference is the key each test misspells rather than anything the engine
computes. The last class holds the schema to the handlers themselves, by
recording what each handler body looks up on an unchecked config.
"""

from __future__ import annotations

import copy
from datetime import timedelta

import pytest

from rates_engine.app import builders, commands
from rates_engine.app.config import CONFIG_SCHEMA, require_config
from rates_engine.conventions.schedule import imm_date, next_imm_on_or_after
from rates_engine.core.errors import ConfigurationError, RatesEngineError

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
    # Declared on one IMM entry only: "SR1" on an IMM quarter is refused by
    # the futures-strip hedge, and the others exercise the period default.
    futures[0]["contract"] = "SR3"
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
        "hedge": {
            "contract_dv01": 25.0,
            "require_full_coverage": True,
            "shocks_bp": [-100, -10, 10, 100],
        },
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


class TestTheWholeFileIsChecked:
    """A typo in a block only another command reads is still a typo.

    Read-tracking, the first version of this check, looked only at the
    blocks the running command used, so with one desk file ``rateng price``
    accepted ``hedge.shock_bps`` and ``rateng hedge`` accepted
    ``risk.key_tenor``, both with exit 0 (Alan's review of #17).
    """

    @pytest.mark.parametrize(
        ("name", "block", "typo", "meant"),
        [
            ("price", "hedge", "shock_bps", "shocks_bp"),
            ("hedge", "risk", "key_tenor", "key_tenors"),
            ("bootstrap", "swap", "notionl", "notional"),
            ("fx-forward", "exposure", "hedge_ration", "hedge_ratio"),
            ("hedge-structures", "curve", "convexty", "convexity"),
        ],
    )
    def test_another_command_s_block(self, name, block, typo, meant, full):
        full[block][typo] = full[block].pop(meant)
        assert f"{block}.{typo} (did you mean {meant}?)" in _refusal(name, full)

    @pytest.mark.parametrize("name", CONFIG_COMMANDS)
    def test_every_command_refuses_the_same_typo(self, name, full):
        full["curve"]["futures"][4]["prise"] = 96.0
        assert "curve.futures[4].prise (did you mean price?)" in _refusal(name, full)

    def test_nothing_is_computed_first(self, full, monkeypatch):
        # The refusal comes from the check, before any curve is built.
        def no_bootstrap(*args, **kwargs):
            raise AssertionError("bootstrapped a curve for a config that is refused")

        monkeypatch.setattr(commands, "bootstrap_discount_curve", no_bootstrap)
        full["swap"]["notionl"] = 5.0
        assert "swap.notionl" in _refusal("price", full)


class TestTheMessage:
    """Says where, says what was probably meant, and says what is accepted."""

    def test_a_key_inside_a_list_entry_is_named_by_its_index(self, full):
        full["curve"]["futures"][2]["settle_note"] = "from the CME page"
        assert "curve.futures[2].settle_note" in _refusal("bootstrap", full)

    def test_a_typo_that_hides_a_required_key_names_both(self, full):
        full["curve"]["futures"][2]["prise"] = full["curve"]["futures"][2].pop("price")
        message = _refusal("bootstrap", full)
        assert "curve.futures[2].price is missing, and bootstrap needs it" in message
        assert "curve.futures[2].prise (did you mean price?)" in message

    def test_an_unknown_top_level_key(self, full):
        full["rsik"] = full.pop("risk")
        assert "rsik (did you mean risk?)" in _refusal("price", full)

    def test_a_key_at_the_wrong_level_says_where_it_belongs(self, full):
        full["notional"] = full["swap"].pop("notional")
        assert "notional (it belongs under swap)" in _refusal("price", full)

    @pytest.mark.parametrize(
        ("block", "given", "meant"),
        [
            ("swap", "rate", "fixed_rate"),
            ("swap", "start", "effective"),
            ("swap", "end", "maturity"),
            ("par_swap", "fixed_rate", "rate"),
            ("par_swap", "end", "maturity"),
        ],
    )
    def test_the_other_block_s_name_for_a_key_gets_this_block_s(self, block, given, meant, full):
        # "belongs under" sent swap.rate to curve.par_swaps[] and swap.start
        # to curve.futures[]: true of the name, wrong for the caller, who
        # meant this swap's fixed_rate and effective (Alan's review of round
        # 2, cleanup).
        if block == "swap":
            entry, where = full["swap"], "swap"
        else:
            entry, where = full["curve"]["par_swaps"][0], "curve.par_swaps[0]"
        entry[given] = entry.pop(meant)
        message = _refusal("price", full)
        assert f"{where}.{given} (did you mean {meant}?)" in message
        assert "belongs under" not in message

    def test_what_a_list_accepts_is_said_once(self, full):
        # It was said once per entry: three entries with the same typo read
        # "Accepted in curve.futures[0]: ...", "[1]: ...", "[2]: ..." (Alan's
        # review of round 2, cleanup).
        for entry in full["curve"]["futures"][:3]:
            entry["prise"] = entry["price"]
        message = _refusal("bootstrap", full)
        assert message.count("Accepted in") == 1
        assert "Accepted in curve.futures[]: contract, end, label, price, start." in message

    def test_every_problem_is_listed_at_once(self, full):
        full["swap"]["fixed_rte"] = 0.09
        full["risk"]["key_tenor"] = [5.0]
        del full["as_of"]
        message = _refusal("price", full)
        assert "swap.fixed_rte" in message
        assert "risk.key_tenor" in message
        assert "as_of is missing, and price needs it" in message

    def test_it_lists_what_the_block_accepts(self, full):
        full["swap"]["notionl"] = 5.0
        message = _refusal("price", full)
        assert "Accepted in swap: effective, fixed_rate, frequency_months, index, " in message

    def test_it_imitates_the_hedge_programme_refusal(self, full):
        # hedging/program.py:155-162 refuses unknown programme keys in this
        # sentence; the config is held to the same one.
        full["swap"]["notionl"] = 5.0
        message = _refusal("price", full)
        assert message.startswith("unknown key(s) in the price config:")
        assert "quietly ignored" in message

    def test_it_points_at_describe(self, full):
        full["swap"]["notionl"] = 5.0
        assert "rateng describe --json" in _refusal("price", full)


class TestRefusedKeys:
    """The v0.3 par-swap keys are documented in order to refuse them."""

    def test_the_v03_shape_is_refused_with_the_migration(self, full):
        full["curve"]["par_swaps"] = [
            {"start": "2026-01-15", "payment_dates": ["2029-01-15"],
             "year_fractions": [3.05], "rate": 0.041}
        ]
        message = _refusal("bootstrap", full)
        assert "curve.par_swaps[0] uses start, payment_dates, year_fractions" in message
        assert "effective, maturity and rate" in message

    def test_a_refused_key_is_never_suggested(self, full):
        # On #17 as first reviewed, `strat` was answered with "did you mean
        # start?", the v0.3 key that is itself refused, and the accepted list
        # included all three v0.3 keys (Alan's review, point 3).
        full["curve"]["par_swaps"][0]["strat"] = "2026-01-15"
        message = _refusal("bootstrap", full)
        assert "did you mean start?" not in message
        # Nor a weak guess at an accepted key: strat is 0.67 from rate.
        assert "curve.par_swaps[0].strat." in message
        accepted = message.split("Accepted in curve.par_swaps[]: ")[1].split(".")[0]
        for refused in ("start", "payment_dates", "year_fractions"):
            assert refused not in accepted.split(", ")

    def test_the_builder_refuses_in_the_same_words(self, as_of):
        # calibration_nodes still checks a par swap itself, for a caller that
        # skips require_config; the words are the validator's.
        entry = {"start": "2026-01-15", "rate": 0.041}
        config = {"curve": {"par_swaps": [entry]}}
        with pytest.raises(ConfigurationError) as direct:
            builders.calibration_nodes(config, as_of)
        with pytest.raises(ConfigurationError) as checked:
            require_config({"as_of": as_of.isoformat(), **config}, "bootstrap")
        for sentence in (
            "curve.par_swaps[0] uses start: the v0.3 par-swap shape",
            "curve.par_swaps[0].effective is missing",
            "curve.par_swaps[0].maturity is missing",
        ):
            assert sentence in str(direct.value)
            assert sentence in str(checked.value)

    def test_the_builder_reads_the_schema_rather_than_a_list_of_its_own(self, as_of):
        # It kept two tuples of par-swap keys beside CONFIG_SCHEMA, so it
        # missed what they left out (Alan's review of round 2, cleanup): a
        # misspelt key, or a null where a value is due.
        entry = {"effective": "2026-01-15", "maturity": "2029-01-15", "rate": 0.041,
                 "payment_lag_day": 0, "frequency_months": None}
        with pytest.raises(ConfigurationError) as direct:
            builders.calibration_nodes({"curve": {"par_swaps": [entry]}}, as_of)
        message = str(direct.value)
        assert "curve.par_swaps[0].payment_lag_day (did you mean payment_lag_days?)" in message
        assert "curve.par_swaps[0].frequency_months is null" in message


class TestNoFalsePositives:
    """A key another command reads, or one that does not apply here, is not a typo."""

    @pytest.mark.parametrize("name", CONFIG_COMMANDS)
    def test_every_command_accepts_the_full_config(self, name, full):
        assert commands.COMMANDS[name](full)["schema_version"] == "1.0"

    def test_price_accepts_curve_strict_which_only_bootstrap_reads(self, full):
        full["curve"]["strict"] = False
        assert commands.COMMANDS["price"](full)["command"] == "price"

    def test_a_proposed_instrument_without_a_programme_is_not_refused(self, full):
        del full["hedge_program"]
        payload = commands.COMMANDS["hedge-structures"](full)
        assert payload["result_type"] == "StructureComparison"

    def test_a_required_key_of_another_command_s_block_is_not_demanded(self, full):
        # bootstrap reads no swap, so a half-written swap block is the
        # business of price and hedge, which refuse it.
        del full["swap"]["fixed_rate"]
        assert commands.COMMANDS["bootstrap"](full)["command"] == "bootstrap"
        assert "swap.fixed_rate is missing, and price needs it" in _refusal("price", full)

    def test_underscore_keys_are_comments_at_any_depth(self, full):
        full["_comment"] = "desk config, 2026-01-15 close"
        full["swap"]["_why"] = "the 2y payer the desk is quoting"
        full["curve"]["futures"][0]["_source"] = "CME settlement page"
        full["curve"]["convexity"]["_note"] = {"sigma": "realised, 60d"}
        assert commands.COMMANDS["price"](full)["command"] == "price"

    def test_a_comment_in_the_hedge_programme_is_a_comment_too(self, full):
        # load_program refuses any key it does not know, and the programme
        # used to reach it with its comments: `_note` was exit 1 here while
        # it passed in every other block (Alan's review, point 2).
        full["hedge_program"]["_note"] = "approved by ALCO, 2026-09"
        payload = commands.COMMANDS["hedge-structures"](full)
        assert payload["program_audit"]["compliant"] is True

    def test_a_key_commented_out_with_an_underscore_is_not_read(self, full):
        baseline = commands.COMMANDS["price"](copy.deepcopy(full))
        full["swap"]["_notional"] = full["swap"].pop("notional")
        commented = commands.COMMANDS["price"](full)
        assert commented["measures"]["pv"]["value"] == pytest.approx(
            baseline["measures"]["pv"]["value"] / 100.0
        )

    @pytest.mark.parametrize("name", ["describe", "list-instruments"])
    def test_the_configless_commands_ignore_whatever_they_are_given(self, name):
        assert commands.COMMANDS[name]({"anything": {"at": "all"}})["evidence"] is None


class TestNullsAndShapes:
    """``null`` means absent where absent means null, and is refused elsewhere."""

    def test_null_where_the_default_is_null_is_the_same_as_absent(self, full):
        # hedge.contract_dv01 defaults to null since fix/futures-strip-hedge.
        # On this branch alone a null used to reach float(None) and exit 1
        # with a TypeError (Alan's review, point 1).
        absent = copy.deepcopy(full)
        del absent["hedge"]["contract_dv01"]
        full["hedge"]["contract_dv01"] = None
        assert commands.COMMANDS["hedge"](full) == commands.COMMANDS["hedge"](absent)

    @pytest.mark.parametrize(
        ("block", "key", "default"),
        [("swap", "notional", "1000000.0"), ("curve", "strict", "true"), ("hedge", "shocks_bp", "[")],
    )
    def test_null_where_the_default_is_a_value_is_refused(self, block, key, default, full):
        # notional: null reached float(None); strict: null read as false and
        # quietly turned the residual check off.
        full[block][key] = None
        name = "bootstrap" if key == "strict" else "hedge"
        message = _refusal(name, full)
        assert f"{block}.{key} is null: give a value, or leave the key out" in message
        assert f"its default, {default}" in message

    def test_null_for_a_required_key_is_missing(self, full):
        full["swap"]["fixed_rate"] = None
        assert "swap.fixed_rate is missing, and price needs it" in _refusal("price", full)

    @pytest.mark.parametrize(
        ("change", "said"),
        [
            (lambda c: c.__setitem__("swap", 5), "swap must be a mapping of keys, not the number 5"),
            (lambda c: c["curve"].__setitem__("futures", {}), "curve.futures must be a list"),
            (
                lambda c: c["curve"]["futures"].__setitem__(3, "SR3"),
                "curve.futures[3] must be a mapping of keys, not the string 'SR3'",
            ),
        ],
        ids=["block", "list", "list-entry"],
    )
    def test_a_block_of_the_wrong_shape_is_refused(self, change, said, full):
        change(full)
        assert said in _refusal("hedge", full)

    def test_a_null_is_called_null(self, full):
        # It was "not NoneType", Python's name for it rather than the file's
        # (Alan's review of round 2, cleanup).
        full["curve"]["futures"][3] = None
        assert "curve.futures[3] must be a mapping of keys, not null" in _refusal("hedge", full)


class TestAListIsAList:
    """A string where a list belongs is refused, not read a character at a time.

    On round 2 of #17 (f913dfc), through ``rateng --json`` on one desk file,
    ``hedge.shocks_bp: "25"`` exited 0 with the shocks ``[2.0, 5.0]`` and
    ``risk.key_tenors: "12"`` exited 0 with the key tenors ``[1.0, 2.0]``;
    ``shocks_bp: 10``, ``key_tenors: 2.0`` and ``key_tenors: [null]``
    reached a bare ``TypeError`` (Alan's review of round 2, point 1). The
    schema checked blocks for their shape but not the leaves that hold a
    list.
    """

    @pytest.mark.parametrize(
        ("name", "block", "key", "value", "said"),
        [
            ("hedge", "hedge", "shocks_bp", "25", "hedge.shocks_bp must be a list of numbers, "
             "not the string '25'"),
            ("hedge", "hedge", "shocks_bp", 10, "hedge.shocks_bp must be a list of numbers, "
             "not the number 10"),
            ("price", "risk", "key_tenors", "12", "risk.key_tenors must be a list of numbers, "
             "not the string '12'"),
            ("price", "risk", "key_tenors", 2.0, "risk.key_tenors must be a list of numbers, "
             "not the number 2.0"),
            ("price", "risk", "key_tenors", [None], "risk.key_tenors[0] must be a finite "
             "number, not null"),
            ("price", "risk", "key_tenors", [1.0, "2"], "risk.key_tenors[1] must be a finite "
             "number, not the string '2'"),
            ("price", "risk", "key_tenors", [float("nan")], "risk.key_tenors[0] must be a "
             "finite number, not the number nan"),
            ("hedge", "hedge", "shocks_bp", [10, True], "hedge.shocks_bp[1] must be a finite "
             "number, not true or false"),
            ("hedge-structures", "hedge_program", "allowed_instruments", "collar",
             "hedge_program.allowed_instruments must be a list of strings, not the string "
             "'collar'"),
        ],
        ids=[
            "shocks-string", "shocks-number", "tenors-string", "tenors-number", "tenors-null",
            "tenors-numeral", "tenors-nan", "shocks-boolean", "instruments-string",
        ],
    )
    def test_it_is_refused_by_path(self, name, block, key, value, said, full):
        full[block][key] = value
        assert said in _refusal(name, full)

    def test_every_command_refuses_it(self, full):
        # A shape, like a block's, is wrong in the file whichever command reads it.
        full["hedge"]["shocks_bp"] = "25"
        for name in CONFIG_COMMANDS:
            assert "hedge.shocks_bp must be a list of numbers" in _refusal(name, full)

    def test_every_list_in_the_full_config_says_what_it_holds(self, full):
        # A list-valued key added without a type would be iterated letter by
        # letter again; this names it first.
        lists = {
            path
            for path, value in _leaves(full).items()
            if isinstance(value, list) and "items" not in DOCUMENTED[path][0]
        }
        assert lists == {"risk.key_tenors", "hedge.shocks_bp", "hedge_program.allowed_instruments"}
        for path in lists:
            assert DOCUMENTED[path][0].get("type") in {"list of numbers", "list of strings"}, path


class TestTheConvexityModelIsNamed:
    """A convexity block names its model, or the sigma beside it prices nothing.

    ``curve.convexity: {"sigma": 0.01}`` took the model ``none``, so ``rateng
    price`` exited 0 with a PV of 114,866.89, the PV with no adjustment at
    all; with ``model: ho_lee`` it is 92,199.33 (Alan's review of round 2,
    point 2, reproduced on f913dfc). That is the PV the ``convexty`` typo
    gave, reached with every key spelt right.
    """

    @pytest.mark.parametrize(
        "block", [{"sigma": 0.01}, {"kappa": 0.03}, {"sigma": 0.01, "kappa": 0.03}],
        ids=["sigma", "kappa", "both"],
    )
    @pytest.mark.parametrize("name", ["bootstrap", "price", "hedge"])
    def test_a_block_without_a_model_is_refused(self, name, block, full):
        full["curve"]["convexity"] = block
        assert f"curve.convexity.model is missing, and {name} needs it" in _refusal(name, full)

    def test_model_none_still_ignores_its_sigma(self, full):
        # #15 lets "none" ignore the sigma it never uses, at Alan's request;
        # naming the model is what this asks for, not dropping the sigma.
        absent = copy.deepcopy(full)
        del absent["curve"]["convexity"]
        full["curve"]["convexity"] = {"model": "none", "sigma": 0.01}
        none = commands.COMMANDS["price"](full)["measures"]["pv"]["value"]
        assert none == commands.COMMANDS["price"](absent)["measures"]["pv"]["value"]


class TestAnEmptyBlockIsLeftOut:
    """An optional block with nothing in it, or nothing but comments, is no block.

    Round 2 of #17 refused ``hedge_program: {}`` and ``curve.stub: {}`` for
    the keys they lacked, and so a block holding only ``_disabled: true``;
    round 1 had skipped them (Alan's review of round 2, point 3). Leaving a
    block in place and empty is how a file switches it off without losing
    where it goes.
    """

    @pytest.mark.parametrize("empty", [{}, {"_disabled": True}], ids=["empty", "comments"])
    @pytest.mark.parametrize(
        ("name", "path"),
        [
            ("hedge-structures", ("hedge_program",)),
            ("bootstrap", ("curve", "stub")),
            ("bootstrap", ("curve", "convexity")),
            ("price", ("risk",)),
            ("hedge", ("hedge",)),
        ],
        ids=["hedge_program", "curve.stub", "curve.convexity", "risk", "hedge"],
    )
    def test_it_is_checked_as_if_left_out(self, name, path, empty, full):
        absent = copy.deepcopy(full)
        *parents, last = path
        given, left_out = full, absent
        for part in parents:
            given, left_out = given[part], left_out[part]
        given[last] = empty
        del left_out[last]
        assert require_config(full, name) == require_config(absent, name)

    @pytest.mark.parametrize("empty", [{}, {"_disabled": True}], ids=["empty", "comments"])
    def test_an_empty_programme_audits_nothing(self, empty, full):
        full["hedge_program"] = empty
        assert "program_audit" not in commands.COMMANDS["hedge-structures"](full)

    def test_a_comment_switches_nothing_off_in_a_block_with_keys(self, full):
        # `_disabled` is a comment like any other: beside real keys, the
        # block is read.
        full["hedge_program"]["_disabled"] = True
        assert "program_audit" in commands.COMMANDS["hedge-structures"](full)

    def test_an_empty_required_block_still_names_its_keys(self, full):
        full["swap"] = {"_note": "to be agreed"}
        message = _refusal("price", full)
        for key in ("effective", "maturity", "fixed_rate"):
            assert f"swap.{key} is missing, and price needs it" in message

    def test_an_empty_list_entry_is_still_refused(self, full):
        # An entry is not a block: an empty one in a list is a quote left
        # half-written, not a quote switched off.
        full["curve"]["futures"][0] = {}
        assert "curve.futures[0].start is missing, and bootstrap needs it" in _refusal(
            "bootstrap", full
        )


class TestTheCheckedCopy:
    """What the handler reads is a clean copy, and the caller's mapping is untouched."""

    def test_comments_and_absent_nulls_are_gone(self, full):
        full["_comment"] = "x"
        full["curve"]["futures"][0]["_source"] = "y"
        checked = require_config(full, "price")
        assert "_comment" not in checked
        assert "_source" not in checked["curve"]["futures"][0]
        assert "long_end_source" not in checked["curve"]
        assert "kappa" not in checked["curve"]["convexity"]

    def test_the_caller_s_mapping_is_not_modified(self, full):
        before = copy.deepcopy(full)
        checked = require_config(full, "hedge-structures")
        checked["fx"]["spot"] = 0.0
        checked["risk"]["key_tenors"].append(9.0)
        assert full == before

    def test_a_config_that_is_not_a_mapping_is_refused(self):
        with pytest.raises(ConfigurationError, match="mapping of keys; got a list"):
            require_config([1, 2], "price")


def _documented(level=CONFIG_SCHEMA, prefix="", readers=None) -> dict[str, tuple[dict, list]]:
    """Every documented path, flattened: its specification and who reads it."""
    found: dict[str, tuple[dict, list]] = {}
    for key, spec in level.items():
        path = f"{prefix}{key}"
        uses = spec.get("used_by", readers)
        found[path] = (spec, uses)
        if "keys" in spec:
            found |= _documented(spec["keys"], f"{path}.", uses)
        if "items" in spec:
            found |= _documented(spec["items"], f"{path}[].", uses)
    return found


DOCUMENTED = _documented()


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


def _leaves(value, prefix="") -> dict:
    """Every documented path of a plain config with the value it holds; list entries at [0]."""
    found: dict = {}
    for key, child in value.items():
        path = f"{prefix}{key}"
        found[path] = child
        if isinstance(child, dict):
            found |= _leaves(child, f"{path}.")
        elif isinstance(child, list) and child and isinstance(child[0], dict):
            found |= _leaves(child[0], f"{path}[].")
    return found


class _Recorder(dict):
    """A test-only mapping that notes every key a handler looks up, by path.

    Paths use ``[]`` for a list entry. ``asked`` collects lookups of present
    and absent keys alike, ``read`` only the present ones; copying the block
    (``dict(block)``) reads all of it, as CPython copies a dict subclass that
    overrides ``__iter__`` through ``__getitem__``, and so does walking its
    ``items()`` or ``values()``.
    """

    def __init__(self, source, pattern, asked, read):
        super().__init__(
            (key, _recorded(value, f"{pattern}.{key}" if pattern else key, asked, read))
            for key, value in source.items()
        )
        self._pattern, self._asked, self._read = pattern, asked, read

    def _note(self, key):
        path = f"{self._pattern}.{key}" if self._pattern else str(key)
        self._asked.add(path)
        if dict.__contains__(self, key):
            self._read.add(path)

    def __getitem__(self, key):
        self._note(key)
        return dict.__getitem__(self, key)

    def get(self, key, default=None):
        self._note(key)
        return dict.get(self, key, default)

    def __contains__(self, key):
        self._note(key)
        return dict.__contains__(self, key)

    def __iter__(self):
        return dict.__iter__(self)

    def items(self):
        for key in dict.__iter__(self):
            self._note(key)
        return dict.items(self)

    def values(self):
        for key in dict.__iter__(self):
            self._note(key)
        return dict.values(self)


def _recorded(value, pattern, asked, read):
    if isinstance(value, dict):
        return _Recorder(value, pattern, asked, read)
    if isinstance(value, list):
        return [_recorded(element, f"{pattern}[]", asked, read) for element in value]
    return value


def _lookups(name: str, config: dict, monkeypatch) -> tuple[set[str], set[str]]:
    """What a handler body looks up on ``config`` when nothing checks it first."""
    asked: set[str] = set()
    read: set[str] = set()
    monkeypatch.setattr(commands, "require_config", lambda config, command: config)
    monkeypatch.setattr(builders, "require_block", lambda block, where, command=None: block)
    commands.COMMANDS[name](_recorded(copy.deepcopy(config), "", asked, read))
    monkeypatch.undo()
    return asked, read


READ_ON_ANOTHER_BRANCH: dict[str, str] = {
    "curve.futures[].contract": (
        "#14 (fix/futures-strip-hedge): the builder hands it to FuturesNode, and the "
        "hedge sizes the entry at that contract's tick"
    ),
    "hedge.require_full_coverage": (
        "#14 (fix/futures-strip-hedge): the hedge passes it to strip_hedge, which "
        "refuses a strip with a gap in the swap's span"
    ),
}
"""Documented keys whose reader is in another PR of the set, by path.

The schema documents them on this branch so that one file serves the whole
set, and the declared merge order lands the reader first (#14 before #17).
Until then nothing here reads them, and
``test_every_documented_key_is_read_by_some_command`` would fail on this
branch alone without this list. It holds keys, not a pattern, so it covers
these two and nothing that comes after them; an entry is deleted once its
reader is merged, which #21, built on every branch, does for both.
"""


class TestTheSchemaMatchesTheHandlers:
    """``describe`` is the shape every refusal points at, so it has to be true.

    The reference is the handler bodies: what each one looks up on a config
    nothing has checked, recorded key by key.
    """

    def test_describe_carries_a_copy_of_the_schema(self):
        payload = commands.COMMANDS["describe"](None)
        assert payload["config"]["keys"] == CONFIG_SCHEMA
        assert payload["config"]["keys"] is not CONFIG_SCHEMA

    def test_editing_the_describe_payload_does_not_change_the_check(self, full):
        # describe used to hand out CONFIG_SCHEMA itself, so a caller editing
        # the payload changed validation for the rest of the process (Alan's
        # review, point 5).
        payload = commands.COMMANDS["describe"](None)
        payload["config"]["keys"]["swap"]["keys"]["notionl"] = {"description": "x" * 20}
        full["swap"]["notionl"] = 5.0
        assert "swap.notionl" in _refusal("price", full)

    def test_the_fx_block_is_documented(self):
        # builders.py's refusal of an empty fx block sends the caller to
        # describe for this shape.
        fx = CONFIG_SCHEMA["fx"]["keys"]
        assert set(fx) == {"spot", "delivery", "r_domestic", "r_foreign", "basis_bp", "volatility"}

    @pytest.mark.parametrize("key", ["r_domestic", "r_foreign"])
    def test_the_fx_rates_say_which_rate_they_are(self, key):
        description = CONFIG_SCHEMA["fx"]["keys"][key]["description"]
        assert "continuously compounded" in description
        assert "ACT/365F" in description

    def test_the_full_config_is_every_documented_key(self, full):
        accepted = {path for path, (spec, _) in DOCUMENTED.items() if "refused" not in spec}
        assert _given(full) == accepted

    @pytest.mark.parametrize("name", CONFIG_COMMANDS)
    def test_every_key_a_handler_looks_up_is_documented(self, name, full, monkeypatch):
        # A key a handler starts reading without an entry here is refused as
        # unknown by every command; this names it before a user meets it.
        asked, _ = _lookups(name, full, monkeypatch)
        assert asked - set(DOCUMENTED) == set()

    @pytest.mark.parametrize(
        "path", sorted(p for p, (s, _) in DOCUMENTED.items() if "used_by" in s)
    )
    def test_used_by_is_exactly_the_commands_that_read_it(self, path, full, monkeypatch):
        readers = {
            name for name in CONFIG_COMMANDS if path in _lookups(name, full, monkeypatch)[1]
        }
        assert readers == set(DOCUMENTED[path][0]["used_by"]), path

    @pytest.mark.parametrize("name", CONFIG_COMMANDS)
    def test_every_required_key_is_read_by_the_commands_that_require_it(
        self, name, full, monkeypatch
    ):
        _, read = _lookups(name, full, monkeypatch)
        required = {
            path
            for path, (spec, uses) in DOCUMENTED.items()
            if spec.get("required") and name in (uses or ())
        }
        assert required - read == set()

    def test_every_documented_key_is_read_by_some_command(self, full, monkeypatch):
        # The schema is what makes a key pass, so a key documented here and
        # read by no handler is the silent default this module exists to
        # stop, with a description to make it look deliberate. Before this
        # test only used_by paths and required keys were tied to a reader,
        # and on this branch alone the two keys below were accepted and
        # ignored with the suite green (Alan's review of round 2, point 5).
        read: set[str] = set()
        for name in CONFIG_COMMANDS:
            read |= _lookups(name, full, monkeypatch)[1]
        accepted = {path for path, (spec, _) in DOCUMENTED.items() if "refused" not in spec}
        assert accepted - read - set(READ_ON_ANOTHER_BRANCH) == set()

    def test_a_key_read_on_another_branch_is_documented(self):
        # An entry below that the schema dropped would hide nothing, but it
        # would no longer say anything true either.
        for path, reader in READ_ON_ANOTHER_BRANCH.items():
            assert path in DOCUMENTED, path
            assert reader.startswith("#"), path

    def test_every_description_is_a_sentence(self):
        for path, (spec, _) in DOCUMENTED.items():
            text = spec.get("description") or spec.get("refused") or ""
            assert len(text) > 15, path


def _without(config: dict, path: str, *, null: bool) -> dict:
    """``config`` with one documented key removed, or set to null; lists at entry 0."""
    changed = copy.deepcopy(config)
    node = changed
    *parents, last = path.split(".")
    for part in parents:
        node = node[part.removesuffix("[]")]
        if part.endswith("[]"):
            node = node[0]
    if null:
        node[last] = None
    else:
        node.pop(last, None)
    return changed


_CASES = [
    (name, path)
    for path, (spec, uses) in DOCUMENTED.items()
    if "refused" not in spec
    for name in CONFIG_COMMANDS
    if name in (uses or ())
]


class TestNoKeyCrashesAHandler:
    """Leaving any key out, or nulling it, ends in a refusal or a result.

    Never in ``KeyError`` or ``TypeError``: the schema's ``required`` and
    ``default`` have to match what the handlers need, and this is where a
    key a handler cannot do without, but the schema calls optional, shows.
    """

    @pytest.mark.parametrize("null", [False, True], ids=["absent", "null"])
    @pytest.mark.parametrize(("name", "path"), _CASES, ids=[f"{n}:{p}" for n, p in _CASES])
    def test_it_is_a_refusal_or_a_result(self, name, path, null, full):
        try:
            commands.COMMANDS[name](_without(full, path, null=null))
        except (RatesEngineError, ValueError):
            pass
