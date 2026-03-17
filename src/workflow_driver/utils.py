from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def compact(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            compacted = compact(item)
            if compacted in (None, "", [], {}):
                continue
            out[key] = compacted
        return out
    if isinstance(value, list):
        out: list[Any] = []
        for item in value:
            compacted = compact(item)
            if compacted in (None, "", [], {}):
                continue
            out.append(compacted)
        return out
    return value


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_input_json(path: str | Path) -> Any:
    return read_json(Path(path))


def dump_output_json(payload: Any, output: str | None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if output:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        stderr_log(f"[io] wrote JSON output to {out_path}")
        return
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")


def stderr_log(message: str) -> None:
    sys.stderr.write(message.rstrip() + "\n")
