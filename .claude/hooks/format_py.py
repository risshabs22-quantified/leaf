"""PostToolUse hook: format a Python file with ruff after Claude edits it. Never blocks.

Unused imports (F401) are left alone, so an import added in one edit isn't deleted
before the next edit uses it.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    path = json.load(sys.stdin).get("tool_input", {}).get("file_path", "")
except (json.JSONDecodeError, AttributeError):
    sys.exit(0)

root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
if path.endswith(".py") and Path(path).exists():
    venv_ruff = root / ".venv" / "bin" / "ruff"
    ruff = str(venv_ruff) if venv_ruff.exists() else shutil.which("ruff")
    if ruff:
        subprocess.run([ruff, "format", "--quiet", path], cwd=root, check=False)
        subprocess.run(
            [ruff, "check", "--fix", "--unfixable", "F401", "--quiet", path], cwd=root, check=False
        )
sys.exit(0)
