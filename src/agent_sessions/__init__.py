"""agent-sessions: find, list and resume saved AI coding-agent sessions."""

import logging

__version__ = "0.1.0"

# Until ``logs.logger()`` puts the log file behind it, what the package logs
# goes nowhere: without a handler, ``logging`` would print warnings to stderr.
logging.getLogger(__name__).addHandler(logging.NullHandler())
