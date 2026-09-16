# Releasing

## Before tagging

1. `pytest -q` is green, and the skipped tests are the expected ones — the
   CME comparisons, and nothing else. A newly skipped test is a regression
   wearing a disguise.
2. `ruff check src tests` and `mypy src/rates_engine` are clean.
3. `CHANGELOG.md` has a dated section for the version, including any change to
   a golden tolerance.
4. The version in `pyproject.toml` matches `rates_engine.__version__`, which
   `tests/test_readme_quickstart.py` checks.

## Tagging

```bash
git tag -a v0.1.0 -m "v0.1.0"
git push origin v0.1.0
```

The tag is what triggers the release workflow.

## Publishing

Publication uses PyPI Trusted Publishing, so no API token is stored anywhere
in this repository or in its secrets. The workflow exchanges a short-lived
GitHub OIDC token for an upload token at publish time.

TestPyPI runs automatically on a tag. **PyPI requires Alan's approval**: the
job sits in a protected environment and does not run until it is released by
hand.

```bash
pip install --index-url https://test.pypi.org/simple/ \
            --extra-index-url https://pypi.org/simple/ \
            finport-ratesengine==0.1.0
python -c "import rates_engine; print(rates_engine.__version__)"
rateng describe --json | head -c 200
```

Verify from TestPyPI before releasing the PyPI job. The extra index is needed
because TestPyPI does not mirror numpy, pandas or scipy.

## After publishing

Open the `[Unreleased]` section in `CHANGELOG.md` again, and bump the version
in `pyproject.toml` to the next patch with a `.dev0` suffix so that an
accidental build cannot masquerade as a release.
