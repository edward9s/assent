"""Keep the test suite's implicit Assent home process-local."""

import atexit
import os
import shutil
import tempfile


_TEST_ASSENT_HOME = tempfile.mkdtemp(prefix="assent-tests-")
os.environ["ASSENT_HOME"] = _TEST_ASSENT_HOME
atexit.register(shutil.rmtree, _TEST_ASSENT_HOME, ignore_errors=True)
