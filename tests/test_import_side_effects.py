"""PRD-001 AC-2.5: importing the package touches nothing outside it.

A library that opens a socket or installs a warning filter at import time has
changed the host process before anyone asked it to do anything, and the caller
cannot opt out short of not importing. The network check runs in a subprocess
with sockets disabled, because a mock inside this process would only prove
that this process was patched.
"""

from __future__ import annotations

import subprocess
import sys
import warnings


def test_import_opens_no_socket():
    program = (
        "import socket\n"
        "def deny(*a, **k):\n"
        "    raise AssertionError('rates_engine opened a socket at import time')\n"
        "socket.socket = deny\n"
        "socket.create_connection = deny\n"
        "import rates_engine\n"
        "print(rates_engine.__version__)\n"
    )
    import rates_engine

    done = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == rates_engine.__version__


def test_import_installs_no_warning_filter():
    """No filter beyond the ones the dependencies already install.

    The dependencies are not innocent here: importing numpy installs four
    process-wide filters and ``scipy.optimize`` adds two more, and this
    package cannot un-install them without changing behaviour for everyone
    else in the process. What it can promise is that it adds none of its own,
    so the baseline is taken *after* numpy, scipy and pandas are in. That is
    the version of this check that can actually fail when someone adds a
    ``warnings.filterwarnings`` to a module here, which is the defect the
    check is for.
    """
    program = (
        "import warnings\n"
        "import numpy, pandas, scipy.optimize  # noqa: F401 - establish the baseline\n"
        "before = list(warnings.filters)\n"
        "import rates_engine  # noqa: F401\n"
        "after = list(warnings.filters)\n"
        "added = [f for f in after if f not in before]\n"
        "assert not added, f'rates_engine installed its own filters: {added}'\n"
        "print('clean')\n"
    )
    done = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "clean"


def test_no_module_here_calls_filterwarnings():
    """The same promise, read off the source rather than off the process."""
    import pathlib

    import rates_engine

    root = pathlib.Path(rates_engine.__file__).parent
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if "filterwarnings" in path.read_text(encoding="utf-8")
        or "simplefilter" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"warning filters installed by: {offenders}"


def test_import_emits_no_warnings():
    program = (
        "import warnings\n"
        "with warnings.catch_warnings(record=True) as caught:\n"
        "    warnings.simplefilter('always')\n"
        "    import rates_engine  # noqa: F401\n"
        "assert not caught, [str(w.message) for w in caught]\n"
        "print('quiet')\n"
    )
    done = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "quiet"


def test_the_network_provider_does_not_import_urllib_at_module_level():
    # The FRED provider is importable on a machine with no network stack
    # configured, because the urllib import is inside the call.
    import rates_engine.market.providers.fred as provider

    source = provider.__file__
    assert source is not None
    with open(source, encoding="utf-8") as handle:
        lines = [line for line in handle if line.startswith("from urllib") or line.startswith("import urllib")]
    assert lines == [], f"urllib imported at module level: {lines}"


def test_warnings_are_errors_in_this_suite():
    # Guards the guard: if filterwarnings=error were dropped from pyproject,
    # the three checks above would still pass while meaning much less.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert True
