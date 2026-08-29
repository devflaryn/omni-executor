"""The shared test double for the omnidroid engine.

Lives in its own module rather than in conftest.py because conftest is loaded
by pytest as a plugin, not as an importable module — `from conftest import ...`
fails at collection. Both conftest's `captured` fixture and test_pool's
`_engine` helper build on what is here.
"""


# What a CURRENT engine prints for `<subcommand> --help`.
#
# The app probes this to find out whether the bundled engine accepts a flag
# before sending it (Api._engine_accepts), because the app and the engine are
# frozen from two SEPARATE checkouts and the engine can be older than the app —
# which is exactly how `unrecognized arguments: --gpu auto` reached a user.
#
# argparse prints the usage block first and lists every option in it, which is
# what keeps a flag inside the 1000-char cap run_engine applies to non-JSON
# stdout. Doubles that answer probes must keep that property or they stop
# resembling the thing they stand in for.
ENGINE_HELP = (
    "usage: omnidroid <cmd> [-h] [--place PLACE] [--mode {gaming,farming}]\n"
    "                      [--gpu {auto,headless,window,off}] [--hide]\n"
    "                      [--start] [--json] name\n"
    "\n"
    "options:\n"
    "  -h, --help  show this help message and exit\n"
)

# ONE usage block stands in for every subcommand. That is enough because the
# app only ever asks "does THIS subcommand's help mention THIS flag", so the
# double just has to advertise every flag the app knows how to send. A test
# that needs a flag to be ABSENT builds its own text instead — see
# test_engine_gpu_flag.py.


class EngineCalls(list):
    """Engine argv the app ran, in order.

    Capability probes (`<subcommand> --help`) are recorded in `.probes` rather
    than in the list itself. They are infrastructure — the app asking what the
    engine can parse — not work the user asked for, and the tests select a
    command by its argv[0]/argv[1]; folding probes in would make
    `next(c for c in calls if c[0] == "start")` return the probe instead of the
    launch. Kept visible rather than swallowed, so a test can still assert on
    how often the app probes and for what.
    """

    def __init__(self):
        super().__init__()
        self.probes = []


def probe_reply():
    """What a modern engine returns for a `--help` capability probe."""
    return {"ok": True, "message": ENGINE_HELP, "exit_code": 0}
