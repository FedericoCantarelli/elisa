"""Allow running el as `python -m elisa`."""

import sys

from elisa.cli import main


sys.exit(main())
