"""Exec wrapper, only invoked by the bounded decoder controller."""

import os
import resource
import sys

resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 1024**2, 512 * 1024**2))
resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
# RLIMIT_AS is enforceable on Linux; macOS relies on the parent's RSS watchdog.
if sys.platform.startswith("linux"):
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
os.execv(sys.argv[1], sys.argv[1:])
