"""agents - a plan format + zero-token automatic scheduler for AI projects.

See agents/templates/format.md for the format contract (agents init copies it into
the project's .agents/). Zero third-party dependencies at runtime: this package may
import only the Python standard library.
"""


class AgentsError(Exception):
    """An error the scheduler can anticipate; its message is shown to the user directly, and should not end in a traceback."""
