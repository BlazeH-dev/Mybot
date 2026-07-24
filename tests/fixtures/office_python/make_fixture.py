"""Regenerate the committed neutral OfficePython OpenXML fixtures."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
FIXTURE_DIR = Path(__file__).resolve().parent
OFFICE_SCRIPT = ROOT / "nanobot" / "skills" / "office-python" / "scripts" / "office.py"


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("${ARTIFACT_ROOT}", str(FIXTURE_DIR))
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    return value


def main() -> None:
    for office_format in ("docx", "xlsx", "pptx"):
        template = json.loads(
            (FIXTURE_DIR / f"create_{office_format}.json").read_text(encoding="utf-8")
        )
        request = _expand(template)
        with tempfile.TemporaryDirectory(prefix="office-python-fixture-") as temporary:
            request_path = Path(temporary) / "request.json"
            result_path = Path(temporary) / "result.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            subprocess.run(
                [
                    sys.executable,
                    str(OFFICE_SCRIPT),
                    "--request",
                    str(request_path),
                    "--result",
                    str(result_path),
                ],
                cwd=ROOT,
                check=True,
            )


if __name__ == "__main__":
    main()
