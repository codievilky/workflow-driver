from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .config import RuntimeContext
from .engine import WorkflowEngine
from .utils import dump_output_json, load_input_json, stderr_log


def parse_value(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return text


def parse_key_value(item: str) -> tuple[str, Any]:
    if "=" not in item:
        raise SystemExit(f"invalid key=value pair: {item}")
    key, raw_value = item.split("=", 1)
    key = key.strip()
    if not key:
        raise SystemExit(f"invalid empty key in pair: {item}")
    return key, parse_value(raw_value)


def load_mapping_file(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    data = load_input_json(path)
    if not isinstance(data, dict):
        raise SystemExit(f"mapping file must be a JSON object: {path}")
    return data


def parse_mapping_items(items: list[str] | None) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for item in items or []:
        key, value = parse_key_value(item)
        mapping[key] = value
    return mapping


def add_runtime_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", help="Workspace root. Defaults to the directory containing --spec.")
    parser.add_argument("--state-root", help="Driver internal state root. Defaults to <workspace>/tmp.")
    parser.add_argument("--gateway-url", help="Gateway URL for model-step execution.")
    parser.add_argument("--gateway-token", help="Gateway bearer token for model-step execution.")
    parser.add_argument("--gateway-config", help="Gateway config JSON path. Defaults to ~/.workflow-driver/config.json.")
    parser.add_argument(
        "--script-command-template",
        help=(
            "Optional script command template. "
            "Supported placeholders: {script_path}, {input_path}, {output_path}, {workspace}, {project_root}."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Enable debug mode: print full model request parameters and full model responses to stderr.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="workflow-driver",
        description="Run a workflow spec with dependency backtracking and artifact reuse.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Execute a workflow spec or a single target step.")
    run_parser.add_argument("--spec", "--spec-path", dest="spec", required=True, help="Workflow spec YAML path.")
    run_parser.add_argument("--output", help="Optional output JSON file path.")
    run_parser.add_argument("--data-dir", help="Artifact directory. Defaults to <workspace>/tmp.")
    run_parser.add_argument("--step-number", type=int, help="Target step number. Defaults to the last step.")
    run_parser.add_argument("--run-id", help="Explicit run id.")
    run_parser.add_argument("--day-id", type=int, help="Convenience state value for day_id.")
    run_parser.add_argument("--state-file", help="JSON object file merged into state values.")
    run_parser.add_argument("--state", action="append", help="State key=value pair. Value supports JSON.")
    run_parser.add_argument("--context-file", help="JSON object file merged into context values.")
    run_parser.add_argument("--context", action="append", help="Context key=value pair. Value supports JSON.")
    run_parser.add_argument("--skill", required=True, help="Skill name that identifies this workflow invocation.")
    run_parser.add_argument("--callback-session-id", help="Optional callback session id for final prompt delivery.")
    run_parser.add_argument("--callback-session-key", help="Optional callback routing key for final prompt delivery.")
    run_parser.add_argument("--force", action="store_true", help="Force re-execution even if artifacts already exist.")
    add_runtime_options(run_parser)

    return parser


def build_runtime(args: argparse.Namespace) -> RuntimeContext:
    workspace = getattr(args, "workspace", None)
    spec_path = getattr(args, "spec", None)
    if workspace is None and spec_path:
        workspace = str(Path(spec_path).expanduser().resolve().parent)
    return RuntimeContext.from_options(
        workspace=workspace,
        state_root=getattr(args, "state_root", None),
        gateway_url=getattr(args, "gateway_url", None),
        gateway_token=getattr(args, "gateway_token", None),
        gateway_config=getattr(args, "gateway_config", None),
        script_command_template=getattr(args, "script_command_template", None),
        debug=bool(getattr(args, "debug", False)),
    )


def build_run_options(args: argparse.Namespace) -> dict[str, Any]:
    spec_path = args.spec
    if not args.workspace:
        spec_path = str(Path(args.spec).expanduser().resolve())

    state_values = load_mapping_file(args.state_file)
    state_values.update(parse_mapping_items(args.state))
    context_values = load_mapping_file(args.context_file)
    context_values.update(parse_mapping_items(args.context))

    return {
        "spec_path": spec_path,
        "data_dir": args.data_dir,
        "step_number": args.step_number,
        "run_id": args.run_id,
        "day_id": args.day_id,
        "skill": args.skill,
        "state_values": state_values,
        "context_values": context_values,
        "callback_session_id": args.callback_session_id,
        "callback_session_key": args.callback_session_key,
        "force": args.force,
    }


def resolve_default_output_path(runtime: RuntimeContext, result: dict[str, Any]) -> str:
    data_dir_value = result.get("data_dir")
    if not isinstance(data_dir_value, str) or not data_dir_value:
        data_dir_path = runtime.state_root
    else:
        data_dir_path = runtime.resolve_path(data_dir_value)
    run_id = result.get("run_id") or "workflow-driver"
    return str(data_dir_path / f"{run_id}.result.json")


def main() -> None:
    args = build_parser().parse_args()
    runtime = build_runtime(args)
    engine = WorkflowEngine(runtime)

    if args.command != "run":
        raise SystemExit(f"unsupported command: {args.command}")

    result = engine.run(build_run_options(args))
    final_output = result.get("final_result", result)
    output_path = args.output or resolve_default_output_path(runtime, result)
    if not args.output:
        stderr_log(f"[io] 未指定 --output，自动写入 {output_path}")
    dump_output_json(final_output, output=output_path)


if __name__ == "__main__":
    main()
