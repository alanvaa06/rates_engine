# Releasing

## One-time setup

Done once, before the first tag, in this order. Nothing in the repository can
do it: environments and trusted publishers live in the GitHub and PyPI
settings, and the workflow only names them.

1. **Create the two GitHub environments.** In the repository, Settings ›
   Environments › New environment, create `testpypi` and `pypi`. On `pypi`,
   tick *Required reviewers* and add the maintainer who approves releases,
   and under *Deployment branches and tags* choose *Selected branches and
   tags* and add a tag rule `v*`. Giving `testpypi` the same tag rule is
   cheap and keeps a branch run off TestPyPI too.

   This has to happen before a tag is pushed. When a job names an
   environment that does not exist, GitHub creates it on the spot with no
   protection rules at all, so the PyPI job would publish without waiting
   for anyone — the approval described under *Publishing* would silently not
   exist. Check that `pypi` lists its reviewer before tagging.

2. **Register the pending publisher on PyPI.** At pypi.org, Your account ›
   Publishing › Add a new pending publisher › GitHub:

   | Field | Value |
   | --- | --- |
   | PyPI project name | `finport-ratesengine` |
   | Owner | `alanvaa06` |
   | Repository name | `rates_engine` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

   The first successful upload creates the project and turns the pending
   publisher into its trusted publisher. A pending publisher does not
   reserve the name, so tag soon after registering it.

3. **Do the same on TestPyPI**, at test.pypi.org, which needs an account of
   its own: the same values, with environment name `testpypi`.

The environment name has to match on both sides. A publisher registered
for `pypi` refuses a token minted by a job in any other environment, which
is what stops a workflow without the approval gate from publishing as this
project.

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

With `X.Y.Z` the version in `pyproject.toml`:

```bash
git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin vX.Y.Z
```

The tag is what triggers the release workflow. Its build job refuses to go
on unless the tag, `pyproject.toml` and the installed wheel's
`rates_engine.__version__` all say `X.Y.Z`, and it runs the whole suite
against the wheel, from the unpacked sdist, before either index sees a
file. A run started by hand has to be started from the tag (*Use workflow
from* › *Tags*); from a branch it stops at the version check.

## Publishing

Publication uses PyPI Trusted Publishing, so no API token is stored anywhere
in this repository or in its secrets. The workflow exchanges a short-lived
GitHub OIDC token for an upload token at publish time.

TestPyPI runs automatically on a tag. **PyPI requires Alan's approval**: the
job sits in the protected `pypi` environment and does not run until it is
released by hand, from the workflow run's *Review deployments* button. That
is true only once the environment exists with its reviewer (see *One-time
setup*).

```bash
pip install --index-url https://test.pypi.org/simple/ \
            --extra-index-url https://pypi.org/simple/ \
            finport-ratesengine==X.Y.Z
python -c "import rates_engine; print(rates_engine.__version__)"
rateng describe --json | head -c 200
```

Verify from TestPyPI before releasing the PyPI job. The extra index is needed
because TestPyPI does not mirror numpy, pandas or scipy.

## After publishing

Open the `[Unreleased]` section in `CHANGELOG.md` again, and bump the version
in `pyproject.toml` to the next patch with a `.dev0` suffix so that an
accidental build cannot masquerade as a release.
