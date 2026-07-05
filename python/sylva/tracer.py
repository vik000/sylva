"""Runtime call tracer — records the *actual order* functions call each other
while the project's own test suite runs.

This is a pytest plugin. Enabled by setting `SYLVA_TRACE_OUT` (a JSON output
path) and `SYLVA_TRACE_ROOT` (the project root whose code to record), then
running pytest with `-p sylva.tracer`. `sylva trace` wires all of that for you.

It hooks `sys.setprofile` around each test and records every call between two
functions **that live under the project root**, in execution order, with the
call-stack depth. The result — per test — is the real succession
`test → A → B → C`, not a coverage-derived set.

Limitations (documented, not silent): per-thread (calls on other threads are not
followed) and Python-level (C functions are skipped). Real execution terminates,
so recursion/cycles terminate the trace naturally.
"""

import json
import os
import sys


class Recorder:
    """A `sys.setprofile` callable that accumulates ordered call events for code
    under `root`. Each event is
    `(seq, depth, caller_file, caller_line, caller_name, callee_file, callee_line, callee_name)`.
    """

    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.events = []
        self.depth = 0
        self.seq = 0

    def _own(self, code):
        f = code.co_filename
        if not f.startswith(self.root):
            return False
        # Skip installed deps that happen to live under the root.
        return (os.sep + "site-packages" + os.sep) not in f and \
               (os.sep + ".venv" + os.sep) not in f

    @staticmethod
    def _named(code):
        # Skip synthetic frames: <module>, <listcomp>, <lambda>, <genexpr>, ...
        return not code.co_name.startswith("<")

    def __call__(self, frame, event, arg):
        if event == "call":
            code = frame.f_code
            if not (self._own(code) and self._named(code)):
                return
            back = frame.f_back
            caller = back.f_code if back is not None else None
            if caller is not None and self._own(caller) and self._named(caller):
                cf, cl, cn = caller.co_filename, caller.co_firstlineno, caller.co_name
            else:
                cf, cl, cn = "", 0, ""  # entered from outside our code (a root)
            self.events.append(
                (self.seq, self.depth, cf, cl, cn,
                 code.co_filename, code.co_firstlineno, code.co_name)
            )
            self.seq += 1
            self.depth += 1
        elif event == "return":
            code = frame.f_code
            if self._own(code) and self._named(code) and self.depth > 0:
                self.depth -= 1


# --------------------------------------------------------------------------- #
# pytest plugin                                                               #
# --------------------------------------------------------------------------- #

_OUT = os.environ.get("SYLVA_TRACE_OUT")
_ROOT = os.environ.get("SYLVA_TRACE_ROOT") or os.getcwd()
_traces = {}


def pytest_configure(config):  # pragma: no cover - exercised via subprocess
    # Nothing to set up; presence of SYLVA_TRACE_OUT gates recording.
    pass


try:
    import pytest

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(item):  # pragma: no cover - runs in the pytest subprocess
        if not _OUT:
            yield
            return
        rec = Recorder(_ROOT)
        previous = sys.getprofile()
        sys.setprofile(rec)
        try:
            yield
        finally:
            sys.setprofile(previous)
            if rec.events:
                _traces[item.nodeid] = rec.events

    def pytest_sessionfinish(session, exitstatus):  # pragma: no cover
        if _OUT:
            with open(_OUT, "w") as f:
                json.dump(_traces, f)
except ImportError:  # pytest not installed where the module is merely imported
    pass
