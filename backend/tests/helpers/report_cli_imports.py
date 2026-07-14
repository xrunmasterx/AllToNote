from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys


BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))

from app.cli.main import main


stdout = io.StringIO()
with contextlib.redirect_stdout(stdout):
    exit_code = main(sys.argv[1:])

print(
    json.dumps(
        {
            "exit_code": exit_code,
            "imported_modules": sorted(sys.modules),
            "stdout": stdout.getvalue(),
        }
    )
)
