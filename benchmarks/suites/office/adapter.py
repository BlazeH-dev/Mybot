"""Trusted Office evaluation-suite entry point.

The gateway registry imports the packaged implementation directly. This file
is the stable extension location mirrored by future suites.
"""

from nanobot.evaluations.catalog import OfficeEvaluationAdapter

adapter = OfficeEvaluationAdapter()
