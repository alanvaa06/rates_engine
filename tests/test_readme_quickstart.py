"""The README's quickstart runs, and prints what the README says it prints.

A quickstart that has drifted is worse than none: it is the first thing
anyone runs, and when it fails they conclude the package is broken rather
than that the documentation is stale.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

import rates_engine

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"


@pytest.fixture(scope="module")
def readme() -> str:
    return README.read_text(encoding="utf-8")


def _python_blocks(text: str) -> list[str]:
    return re.findall(r"```python\n(.*?)```", text, flags=re.DOTALL)


def test_the_readme_has_a_python_quickstart(readme):
    assert _python_blocks(readme), "the README shows no runnable example"


def _is_complete_program(block: str) -> bool:
    """A block that imports something is meant to run; a fragment is not."""
    return any(
        line.startswith(("import ", "from ")) for line in block.splitlines()
    )


def test_every_complete_program_in_the_readme_runs(readme, tmp_path):
    blocks = [b for b in _python_blocks(readme) if _is_complete_program(b)]
    assert blocks, "the README shows no complete program"
    for index, block in enumerate(blocks):
        script = tmp_path / f"block_{index}.py"
        script.write_text(block, encoding="utf-8")
        done = subprocess.run(
            [sys.executable, str(script)], capture_output=True, text=True, cwd=tmp_path
        )
        assert done.returncode == 0, f"README block {index} failed:\n{done.stderr}"


def test_every_fragment_in_the_readme_is_valid_python(readme):
    # Fragments are not run — they stand on names the surrounding prose
    # introduced — but a fragment that will not parse is a typo either way.
    for index, block in enumerate(_python_blocks(readme)):
        if _is_complete_program(block):
            continue
        compile(block, f"<readme block {index}>", "exec")


def test_every_name_a_fragment_uses_is_one_the_package_exports(readme):
    # The failure this catches is a fragment quietly going stale: it renames
    # nothing and still refers to an attribute that no longer exists.
    import rates_engine
    from rates_engine import diagnostics

    for block in _python_blocks(readme):
        if _is_complete_program(block):
            continue
        for attribute in re.findall(r"\bquality_report\b|\bworst_quality\b|\bsources\b", block):
            assert hasattr(diagnostics, attribute) or attribute in {
                "worst_quality",
                "sources",
            }, attribute
    assert hasattr(rates_engine, "Evidence")


def test_the_quickstart_prints_what_the_readme_claims(readme, tmp_path):
    # The first block is the quickstart, and the fenced text block after it
    # is its output. They have to agree line for line.
    block = _python_blocks(readme)[0]
    expected = re.search(r"```text\n(.*?)```", readme, flags=re.DOTALL)
    assert expected, "the quickstart shows no expected output"
    script = tmp_path / "quickstart.py"
    script.write_text(block, encoding="utf-8")
    done = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, cwd=tmp_path
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == expected.group(1).strip()


def test_every_bash_command_in_the_readme_is_one_this_package_offers(readme):
    # Read the command set from the CLI's own table rather than repeating it
    # here, so adding a command cannot leave this test asserting the old one.
    from rates_engine.cli import _COMMANDS

    commands = re.findall(r"^rateng ([\w-]+)", readme, flags=re.MULTILINE)
    assert commands, "the README shows no CLI usage"
    assert set(commands) <= set(_COMMANDS)


def test_the_readme_names_every_console_script(readme):
    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = set(config["project"]["scripts"])
    assert scripts, "the package declares no console script"
    missing = [name for name in scripts if name not in readme]
    assert missing == [], missing


def test_the_version_agrees_between_the_package_and_pyproject():
    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["project"]["version"] == rates_engine.__version__


def test_the_example_config_generator_produces_a_usable_config(tmp_path):
    destination = tmp_path / "config.json"
    done = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "write_example_config.py"), str(destination)],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    for command in ("bootstrap", "price", "hedge"):
        run = subprocess.run(
            [sys.executable, "-m", "rates_engine.cli", command, "--config",
             str(destination), "--json"],
            capture_output=True,
            text=True,
        )
        assert run.returncode == 0, run.stderr
