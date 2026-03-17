from __future__ import annotations

import argparse
from typing import Any

from .config import RuntimeContext
from .engine import WorkflowEngine
from .executor import execute_final_step, execute_model_step, execute_script_step
from .utils import dump_output_json, load_input_json, read_json


def add_runtime_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", help="Workspace root. Defaults to $WORKFLOW_DRIVER_WORKSPACE or current directory.")
    parser.add_argument("--state-root", help="State and artifact root. Defaults to <workspace>/tmp.")
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="workflow-driver",
        description="Generic workflow driver packaged as a uv-friendly Python CLI.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    workflow_parser = subparsers.add_parser(
        "workflow",
        help="Run workflow state-machine actions from a request JSON file.",
    )
    workflow_parser.add_argument("--input", required=True, help="Request JSON file path.")
    workflow_parser.add_argument("--output", help="Response JSON file path.")
    workflow_parser.add_argument("--action", help="Override request.action.")
    workflow_parser.add_argument("--day-id", type=int, help="Override request.day_id.")
    workflow_parser.add_argument("--run-id", help="Override request.run_id.")
    workflow_parser.add_argument("--spec-path", help="Override request.spec_path.")
    add_runtime_options(workflow_parser)

    for command_name, help_text in (
        ("script-step", "Execute a prepared script step payload."),
        ("model-step", "Execute a prepared model step payload."),
        ("final-step", "Execute a prepared final step payload."),
    ):
        step_parser = subparsers.add_parser(command_name, help=help_text)
        step_parser.add_argument("--input", required=True, help="Prepared step JSON file path.")
        step_parser.add_argument("--output", help="Result JSON file path.")
        step_parser.add_argument("--run-id", default="manual-run", help="Run id used for model/final step context.")
        add_runtime_options(step_parser)

    return parser


def build_runtime(args: argparse.Namespace) -> RuntimeContext:
    return RuntimeContext.from_options(
        workspace=getattr(args, "workspace", None),
        state_root=getattr(args, "state_root", None),
        gateway_url=getattr(args, "gateway_url", None),
        gateway_token=getattr(args, "gateway_token", None),
        gateway_config=getattr(args, "gateway_config", None),
        script_command_template=getattr(args, "script_command_template", None),
    )


def apply_request_overrides(request_payload: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    payload = dict(request_payload or {})
    overrides = {
        "action": args.action,
        "day_id": args.day_id,
        "run_id": args.run_id,
        "spec_path": args.spec_path,
    }
    for key, value in overrides.items():
        if value is not None:
            payload[key] = value
    return payload


def run_workflow_command(args: argparse.Namespace) -> dict[str, Any]:
    runtime = build_runtime(args)
    engine = WorkflowEngine(runtime)
    request_payload = load_input_json(args.input)
    if not isinstance(request_payload, dict):
        raise SystemExit("workflow request payload must be a JSON object")
    return engine.run_request(apply_request_overrides(request_payload, args))


def load_step_payload(args: argparse.Namespace) -> tuple[RuntimeContext, dict[str, Any]]:
    runtime = build_runtime(args)
    step_payload = load_input_json(args.input)
    if not isinstance(step_payload, dict):
        raise SystemExit("step payload must be a JSON object")
    return runtime, step_payload


def run_step_command(args: argparse.Namespace) -> dict[str, Any]:
    runtime, step_payload = load_step_payload(args)
    if args.command == "script-step":
        artifact = execute_script_step(runtime, step_payload, args.run_id)
    elif args.command == "model-step":
        artifact = execute_model_step(runtime, step_payload, args.run_id)
    elif args.command == "final-step":
        artifact = execute_final_step(runtime, step_payload, args.run_id)
    else:
        raise SystemExit(f"unsupported command: {args.command}")
    artifact_data = read_json(runtime.resolve_path(artifact))
    return {
        "artifact": artifact,
        "artifact_data": artifact_data,
    }


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "workflow":
        result = run_workflow_command(args)
    else:
        result = run_step_command(args)
    dump_output_json(result, args.output)


if __name__ == "__main__":
    main()
