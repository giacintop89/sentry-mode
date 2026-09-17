"""Make the agent importable from the checkout without installing anything.

Paths are worked out from this file, not from wherever pytest was started, so the suite
runs the same whether it is invoked from the repository root, from `satellite/`, or
against an installed wheel with no `src/` directory beside it.
"""

import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SATELLITE = TESTS.parent
SOURCE = SATELLITE / "src"
REPOSITORY = SATELLITE.parent
CONTRACTS = REPOSITORY / "contracts" / "satellite" / "v1"

sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS / "unit"))  # so one test module can borrow another's fakes
if SOURCE.is_dir():
    sys.path.insert(0, str(SOURCE))
