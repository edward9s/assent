"""assent - a plan format + zero-token automatic scheduler for AI projects.

See assent/templates/format.md for the format contract (assent init copies it into
the project's .assent/). Zero third-party dependencies at runtime: this package may
import only the Python standard library.
"""


class AssentError(Exception):
    """An error the scheduler can anticipate; its message is shown to the user directly, and should not end in a traceback."""
