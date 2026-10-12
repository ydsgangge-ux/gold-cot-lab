"""Gold COT Lab backend package.

Ensures the project root is importable so that the CLI scripts under
``scripts/`` can be reused by the API layer (``POST /api/admin/compute-indicators``
runs exactly the same recompute path as the command line). This has to happen
here rather than in ``main.py``: submodules are imported before any statement in
``main`` that is not itself an import.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))