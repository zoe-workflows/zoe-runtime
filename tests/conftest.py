"""Make the ``tests/`` directory importable for every suite.

``tests/skill_package.py`` locates the separately-distributed skill checkout and
is imported by ``tests/*.py`` and ``tests/contract/*.py``. Registering the
directory here keeps that import working regardless of which sub-directory a
test module lives in.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
