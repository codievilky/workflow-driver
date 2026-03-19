from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

_LOG_FILE_PATH: Path | None = None


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


def configure_log_file(path: str | Path | None, *, truncate: bool = True) -> Path | None:
    global _LOG_FILE_PATH
    if path is None:
        _LOG_FILE_PATH = None
        return None
    log_path = Path(path).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if truncate:
        log_path.write_text("", encoding="utf-8")
    else:
        log_path.touch(exist_ok=True)
    _LOG_FILE_PATH = log_path
    return _LOG_FILE_PATH


def current_log_file() -> Path | None:
    return _LOG_FILE_PATH


def stderr_log(message: str) -> None:
    line = message.rstrip() + "\n"
    sys.stderr.write(line)
    if _LOG_FILE_PATH:
        with _LOG_FILE_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line)
