"""PostToolUse hook: run ruff fix + format on Python files Claude edits.

Silently no-ops if the file isn't Python or ruff isn't installed yet.
"""

import json
import shutil
import subprocess
import sys

payload = json.load(sys.stdin)
path = payload.get("tool_input", {}).get("file_path", "")
ruff = shutil.which("ruff")

if path.endswith(".py") and ruff:
    subprocess.run([ruff, "check", "--fix", "--quiet", path], check=False)
    subprocess.run([ruff, "format", "--quiet", path], check=False)
