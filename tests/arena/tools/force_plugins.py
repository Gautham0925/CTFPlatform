"""pytest plugin: run the NATIVE CTFd suite with Arena loaded in every app.

    PYTHONPATH=tests/arena/tools python -m pytest -p force_plugins tests --ignore=tests/arena

Native tests call create_ctfd(enable_plugins=False) by default; this flips
the default so each native test app also loads Arena. It proves Arena does
not change native behaviour (see TESTING_GUIDE.md for the two expected,
explained differences).
"""

import inspect


def pytest_configure(config):
    import tests.helpers as h

    params = list(inspect.signature(h.create_ctfd).parameters)
    defaults = list(h.create_ctfd.__defaults__)
    idx = params.index("enable_plugins") - (len(params) - len(defaults))
    defaults[idx] = True
    h.create_ctfd.__defaults__ = tuple(defaults)
