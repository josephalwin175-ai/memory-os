"""Launcher that works straight from a checkout or plugin folder, with no install."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory_os.cli import main  # noqa: E402

sys.exit(main())
