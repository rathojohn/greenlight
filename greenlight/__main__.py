"""python -m greenlight: the CLI, for when the greenlight script isn't on PATH."""
import sys

from .cli import main

sys.exit(main())
