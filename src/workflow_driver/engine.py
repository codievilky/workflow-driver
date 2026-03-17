from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import RuntimeContext
from .executor import execute_final_step, execute_model_step, execute_script_step
from .utils import compact, now_iso, now_stamp, read_json, read_yaml, write_json

FINAL_RESULT_RESERVED_KEYS = {
    "final_render_payload",
    "final_render_schema",
    "final_render_prompt",
}


class WorkflowEngine:
    def __init__(self, runtime: RuntimeContext):
        self.runtime = runtime

    def load_spec(self, spec_path: str) -> dict[str, Any]:
        data = read_yaml(self.runtime.resolve_path(spec_path))
        if not isinstance(data, dict):
            raise SystemExit("invalid workflow spec yaml")
        return data

    def state_dir(self, run_id: str) -> Path:
        path = self.runtime.resolve_under_state_root(run_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def state_path(self, run_id: str) -> Path:
        return self.state_dir(run_id) / "workflow_state.json"

    def trace_path(self, run_id: str) -> Path:
        return self.state_dir(run_id) / "trace.jsonl"

    def append_trace(self, run_id: str, event: dict[str, Any]) -> None:
        path = self.trace_path(run_id)
        payload = dict(event)
        payload.setdefault("ts", now_iso())
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(compact(payload), ensure_ascii=False) + "\n")

    def load_state(self, run_id: str) -> dict[str, Any]:
        path = self.state_path(run_id)
        if path.exists():
            return read_json(path)
        return {}

    def save_state(self, run_id: str, state: dict[str, Any]) -> None:
        state["updated_at"] = now_iso()
        write_json(self.state_path(run_id), state)

    @staticmethod
    def next_step(spec: dict[str, Any], current_step: str | None) -> dict[str, Any] | None:
        steps = spec["steps"]
        if current_step is None:
            return steps[0]
        index = next(i for i, step in enumerate(steps) if step["id"] == current_step)
        if index + 1 >= len(steps):
            return None
        return steps[index + 1]

    @staticmethod
    def step_index(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {step["id"]: step for step in spec["steps"]}

    @staticmethod
    def init_step_states(spec: dict[str, Any]) -> dict[str, Any]:
        states: dict[str, Any] = {}
        for step in spec["steps"]:
            states[step["id"]] = {
                "id": step["id"],
                "number": step["number"],
                "name": step["name"],
                "actor": step["actor"],
                "kind": step["kind"],
                "default_artifact_name": step.get("default_artifact_name")
                or f'step{step["number"]}_{step.get("output_key") or step["id"]}.json',
                "status": "pending",
            }
        return states

    @staticmethod
    def begin_step(state: dict[str, Any], step: dict[str, Any]) -> None:
        step_state = state["step_states"][step["id"]]
        if step_state.get("status") != "running":
            step_state["status"] = "running"
            step_state["started_at"] = now_iso()

    @staticmethod
    def finish_step(state: dict[str, Any], step: dict[str, Any], artifact: str | None = None) -> None:
        step_state = state["step_states"][step["id"]]
        step_state["status"] = "done"
        step_state["finished_at"] = now_iso()
        if artifact:
            step_state["artifact"] = artifact
        try:
            started = datetime.fromisoformat(step_state["started_at"])
            finished = datetime.fromisoformat(step_state["finished_at"])
            step_state["duration_ms"] = int((finished - started).total_seconds() * 1000)
        except Exception:
            pass

    @staticmethod
    def artifact_path_for(step_id: str, state: dict[str, Any]) -> str:
        return (state.get("artifacts") or {}).get(step_id) or (
            f'{state["artifacts_dir"]}/{state["step_states"][step_id]["default_artifact_name"]}'
        )

    def resolve_source(self, source: dict[str, Any], state: dict[str, Any]) -> Any:
        kind = source.get("kind")
        if kind == "state":
            return state.get(source["key"])
        if kind == "context":
            return (state.get("context") or {}).get(source["key"])
        if kind == "artifact":
            return self.artifact_path_for(source["artifact"], state)
        if kind == "artifact_field":
            return {
                "artifact_path": self.artifact_path_for(source["artifact"], state),
                "field": source["field"],
            }
        if kind == "built":
            payload: dict[str, Any] = {}
            for key, sub_source in (source.get("builder_payload") or {}).items():
                payload[key] = self.resolve_source(sub_source, state)
            output_name = source.get("output_name") or "built_output.json"
            prepared_input_name = source.get("prepared_input_name") or output_name
            return {
                "builder": source["builder"],
                "builder_payload": payload,
                "output_name": output_name,
                "output_path": f'{state["artifacts_dir"]}/{output_name}',
                "prepared_input_name": prepared_input_name,
                "prepared_input_path": f'{state["artifacts_dir"]}/{prepared_input_name}',
            }
        return None

    def resolve_inputs(self, step: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        run_id = state["run_id"]
        self.state_dir(run_id)
        resolved: dict[str, Any] = {}
        prepared_input_path = None
        model_input_contract = None
        for name, source in (step.get("input_sources") or {}).items():
            resolved[name] = self.resolve_source(source, state)
            if isinstance(resolved[name], dict) and "builder" in resolved[name] and "prepared_input_path" in resolved[name]:
                prepared_input_path = resolved[name]["prepared_input_path"]
                model_input_contract = {
                    "builder": resolved[name]["builder"],
                    "builder_payload": resolved[name]["builder_payload"],
                    "prepared_input_path": resolved[name]["prepared_input_path"],
                    "output_path": resolved[name]["output_path"],
                }
        default_artifact_name = step.get("default_artifact_name") or (
            f'step{step["number"]}_{step.get("output_key") or step["id"]}.json'
        )
        return compact(
            {
                "run_id": run_id,
                "artifacts_dir": state["artifacts_dir"],
                "required_inputs": step.get("inputs") or [],
                "resolved_inputs": resolved,
                "available_artifacts": state.get("artifacts") or {},
                "prepared_input_path": prepared_input_path,
                "model_input_contract": model_input_contract,
                "default_artifact_path": f'{state["artifacts_dir"]}/{default_artifact_name}',
            },
        )

    def step_payload(self, step: dict[str, Any] | None, state: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if step is None:
            return None
        payload = {
            "id": step["id"],
            "number": step["number"],
            "name": step["name"],
            "actor": step["actor"],
            "kind": step["kind"],
            "script": step.get("script"),
            "input_builder": step.get("input_builder"),
            "prompt_key": step.get("prompt_key"),
            "prompt_text": step.get("prompt_text"),
            "prompt_template": step.get("prompt_template"),
            "normalizer": step.get("normalizer"),
            "execution": step.get("execution") or {},
            "inputs": step.get("inputs") or [],
            "data_refs": step.get("data_refs") or [],
            "output_mode": step.get("output_mode"),
            "output_key": step.get("output_key"),
            "broadcast": step.get("broadcast"),
            "pause_point": step.get("pause_point", False),
        }
        if state is not None:
            payload["step_context"] = self.resolve_inputs(step, state)
        return compact(payload)

    def read_final_artifact(self, path: str | None) -> Any:
        if not path:
            return None
        try:
            return read_json(self.runtime.resolve_path(path))
        except Exception:
            return None

    @staticmethod
    def parse_callback_target(callback_session_key: str | None) -> dict[str, Any] | None:
        if not callback_session_key:
            return None
        parts = callback_session_key.split(":")
        if len(parts) < 4 or parts[0] != "agent":
            return None
        agent = parts[1]
        channel = parts[2]
        if channel == "telegram" and len(parts) >= 5:
            return {
                "agent": agent,
                "channel": "telegram",
                "reply_to": parts[-1],
                "mode": "channel_reply",
            }
        if channel == "qqbot" and len(parts) >= 6:
            return {
                "agent": agent,
                "channel": "qqbot",
                "reply_to": f"user:{parts[-1].upper()}",
                "mode": "channel_reply",
            }
        return None

    @staticmethod
    def is_uuid_like(value: str | None) -> bool:
        if not value:
            return False
        return bool(re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value))

    def send_callback(
        self,
        *,
        callback_session_id: str | None,
        callback_session_key: str | None,
        final_prompt: str,
    ) -> dict[str, Any]:
        if callback_session_id and self.is_uuid_like(callback_session_id):
            proc = subprocess.run(
                [
                    "openclaw",
                    "agent",
                    "--session-id",
                    callback_session_id,
                    "--message",
                    final_prompt,
                    "--deliver",
                    "--json",
                ],
                cwd=self.runtime.workspace,
                text=True,
                capture_output=True,
            )
            if proc.returncode == 0:
                return {
                    "callback_status": "sent",
                    "callback_mode": "session",
                    "callback_session_id": callback_session_id,
                }
            return {
                "callback_status": "failed",
                "callback_mode": "session",
                "callback_session_id": callback_session_id,
                "callback_error": (proc.stderr or proc.stdout).strip(),
            }

        target = self.parse_callback_target(callback_session_key)
        if target:
            proc = subprocess.run(
                [
                    "openclaw",
                    "agent",
                    "--agent",
                    target["agent"],
                    "--reply-channel",
                    target["channel"],
                    "--reply-to",
                    target["reply_to"],
                    "--message",
                    final_prompt,
                    "--deliver",
                    "--json",
                ],
                cwd=self.runtime.workspace,
                text=True,
                capture_output=True,
            )
            if proc.returncode == 0:
                return {
                    "callback_status": "sent",
                    "callback_mode": "channel_reply",
                    "callback_target": target,
                }
            return {
                "callback_status": "failed",
                "callback_mode": "channel_reply",
                "callback_target": target,
                "callback_error": (proc.stderr or proc.stdout).strip(),
            }

        return {"callback_status": "skipped", "callback_mode": "none"}

    @staticmethod
    def build_final_result_contract(*, artifact_path: str | None, artifact_data: Any) -> dict[str, Any]:
        contract: dict[str, Any] = {
            "final_result_path": artifact_path,
            "final_result": artifact_data,
            "final_report_path": artifact_path,
            "final_report": artifact_data,
            "result_for_caller": artifact_data,
        }
        if isinstance(artifact_data, dict) and "final_prompt" in artifact_data:
            contract["result_kind"] = "final_prompt"
            contract["caller_prompt"] = artifact_data.get("final_prompt")
            contract["caller_prompt_path"] = artifact_path.replace(".json", ".prompt.txt") if artifact_path else None
            contract["result_for_caller"] = {
                "final_prompt": artifact_data.get("final_prompt"),
                "final_prompt_path": contract["caller_prompt_path"],
                "data_refs": artifact_data.get("data_refs") or [],
            }
            contract["render_prompt"] = artifact_data.get("final_prompt")
            return contract
        if isinstance(artifact_data, dict):
            if "final_render_payload" in artifact_data:
                contract["render_payload"] = artifact_data.get("final_render_payload")
            if "final_render_schema" in artifact_data:
                contract["render_schema"] = artifact_data.get("final_render_schema")
            if "final_render_prompt" in artifact_data:
                contract["render_prompt"] = artifact_data.get("final_render_prompt")
            if FINAL_RESULT_RESERVED_KEYS & set(artifact_data.keys()):
                contract["result_kind"] = "final_package"
                contract["final_contract"] = {
                    "mode": "structured-package",
                    "reserved_keys": sorted(FINAL_RESULT_RESERVED_KEYS),
                }
            else:
                contract["result_kind"] = "artifact-pass-through"
                contract["final_contract"] = {
                    "mode": "artifact-pass-through",
                    "reserved_keys": sorted(FINAL_RESULT_RESERVED_KEYS),
                }
        else:
            contract["result_kind"] = "artifact-pass-through"
        return contract

    def default_run_id(self, workflow_id: str, day_id: Any) -> str:
        if day_id is not None:
            return f"{workflow_id}-{day_id}-{now_stamp()}"
        return f"{workflow_id}-{now_stamp()}"

    def build_initial_state(
        self,
        *,
        workflow_id: str,
        run_id: str,
        day_id: Any,
        spec_path: str,
        spec: dict[str, Any],
        context: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "workflow_id": workflow_id,
            "run_id": run_id,
            "day_id": day_id,
            "spec_path": spec_path,
            "status": "running",
            "warnings": [],
            "pause": False,
            "waiting_for_user": False,
            "blocked": False,
            "auto_continue": True,
            "failed": False,
            "failure_reason": None,
            "failed_step": None,
            "retryable": False,
            "started_at": now_iso(),
            "updated_at": now_iso(),
            "finished_at": None,
            "current_step": None,
            "completed_steps": [],
            "done": False,
            "artifacts_dir": self.runtime.to_output_path(self.state_dir(run_id)),
            "artifacts": {},
            "step_states": self.init_step_states(spec),
            "context": context or {},
        }

    def start_result(self, *, workflow_id: str, run_id: str, state: dict[str, Any], first_step: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
        return compact(
            {
                "workflow_id": workflow_id,
                "run_id": run_id,
                "status": state["status"],
                "warnings": state["warnings"],
                "pause": state["pause"],
                "waiting_for_user": state["waiting_for_user"],
                "blocked": state["blocked"],
                "auto_continue": state["auto_continue"],
                "failed": state["failed"],
                "failure_reason": state["failure_reason"],
                "failed_step": state["failed_step"],
                "retryable": state["retryable"],
                "started_at": state["started_at"],
                "current_step": None,
                "next_step": self.step_payload(first_step, state),
                "broadcast": f'将使用 driver 进行 {len(spec["steps"])} 步操作',
                "step_start_broadcast": f'打算进行：第{first_step["number"]}步 {first_step["name"]}',
                "artifacts_dir": state["artifacts_dir"],
            },
        )

    def run_request(self, req: dict[str, Any]) -> dict[str, Any]:
        action = req.get("action")
        spec_path = req.get("spec_path")
        if not spec_path:
            raise SystemExit("spec_path required")

        spec = self.load_spec(spec_path)
        day_id = req.get("day_id")
        workflow_id = spec.get("workflow_id") or "workflow"
        run_id = req.get("run_id") or self.default_run_id(workflow_id, day_id)
        step_map = self.step_index(spec)

        if action == "start":
            state = self.build_initial_state(
                workflow_id=workflow_id,
                run_id=run_id,
                day_id=day_id,
                spec_path=spec_path,
                spec=spec,
                context=req.get("context") or {},
            )
            first_step = spec["steps"][0]
            self.begin_step(state, first_step)
            self.save_state(run_id, state)
            self.append_trace(
                run_id,
                {
                    "event": "workflow_started",
                    "workflow_id": workflow_id,
                    "run_id": run_id,
                    "first_step": first_step["id"],
                },
            )
            self.append_trace(
                run_id,
                {
                    "event": "step_started",
                    "step_id": first_step["id"],
                    "actor": first_step["actor"],
                    "kind": first_step["kind"],
                },
            )
            return self.start_result(workflow_id=workflow_id, run_id=run_id, state=state, first_step=first_step, spec=spec)

        state = self.load_state(run_id)
        if not state and action == "run":
            state = self.build_initial_state(
                workflow_id=workflow_id,
                run_id=run_id,
                day_id=day_id,
                spec_path=spec_path,
                spec=spec,
                context=req.get("context") or {},
            )
            first_step = spec["steps"][0]
            self.begin_step(state, first_step)
            self.save_state(run_id, state)
            self.append_trace(
                run_id,
                {
                    "event": "workflow_started",
                    "workflow_id": workflow_id,
                    "run_id": run_id,
                    "first_step": first_step["id"],
                },
            )
            self.append_trace(
                run_id,
                {
                    "event": "step_started",
                    "step_id": first_step["id"],
                    "actor": first_step["actor"],
                    "kind": first_step["kind"],
                },
            )
        elif not state:
            raise SystemExit("workflow state not found; call action=start first")

        if action == "status":
            next_step = self.next_step(spec, state.get("current_step"))
            return compact(
                {
                    "workflow_id": workflow_id,
                    "run_id": run_id,
                    "status": state.get("status"),
                    "warnings": state.get("warnings") or [],
                    "pause": state.get("pause"),
                    "waiting_for_user": state.get("waiting_for_user"),
                    "blocked": state.get("blocked"),
                    "auto_continue": state.get("auto_continue"),
                    "failed": state.get("failed"),
                    "failure_reason": state.get("failure_reason"),
                    "failed_step": state.get("failed_step"),
                    "retryable": state.get("retryable"),
                    "started_at": state.get("started_at"),
                    "updated_at": state.get("updated_at"),
                    "finished_at": state.get("finished_at"),
                    "current_step": state.get("current_step"),
                    "next_step": self.step_payload(next_step, state),
                    "completed_steps": state.get("completed_steps") or [],
                    "artifacts_dir": state.get("artifacts_dir"),
                    "artifacts": state.get("artifacts") or {},
                    "step_states": state.get("step_states") or {},
                    "context": state.get("context") or {},
                },
            )

        if action == "run":
            return self._run_workflow_loop(req=req, workflow_id=workflow_id, run_id=run_id, spec=spec, state=state)

        if action == "run_script_step":
            next_step = self.next_step(spec, state.get("current_step"))
            if next_step is None:
                return {"workflow_id": workflow_id, "run_id": run_id, "status": "done"}
            if next_step.get("kind") != "script":
                return {
                    "workflow_id": workflow_id,
                    "run_id": run_id,
                    "status": state.get("status"),
                    "blocked": True,
                    "failed": False,
                    "next_step": self.step_payload(next_step, state),
                }
            artifact = execute_script_step(self.runtime, self.step_payload(next_step, state), run_id)
            return {
                "workflow_id": workflow_id,
                "run_id": run_id,
                "status": state.get("status"),
                "artifact": artifact,
                "next_step": self.step_payload(next_step, state),
            }

        if action in {"next", "complete"}:
            return self._advance_completed_step(
                action=action,
                req=req,
                workflow_id=workflow_id,
                run_id=run_id,
                spec=spec,
                state=state,
                step_map=step_map,
            )

        raise SystemExit("action must be one of: start, status, next, complete, run, run_script_step")

    def _run_workflow_loop(
        self,
        *,
        req: dict[str, Any],
        workflow_id: str,
        run_id: str,
        spec: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        executed_steps: list[dict[str, Any]] = []
        aggregated_broadcasts: list[str] = []
        final_result_path = None
        final_result = None

        while True:
            current_next = self.next_step(spec, state.get("current_step"))
            if current_next is None:
                state["done"] = True
                state["status"] = "done"
                state["finished_at"] = state.get("finished_at") or now_iso()
                self.save_state(run_id, state)
                self.append_trace(
                    run_id,
                    {
                        "event": "workflow_completed",
                        "workflow_id": workflow_id,
                        "run_id": run_id,
                    },
                )
                artifacts = state.get("artifacts") or {}
                current_step = state.get("current_step")
                if current_step and current_step in artifacts:
                    final_result_path = artifacts[current_step]
                    final_result = self.read_final_artifact(final_result_path)
                result = {
                    "workflow_id": workflow_id,
                    "run_id": run_id,
                    "status": "done",
                    "done": True,
                    "completed_steps": state.get("completed_steps") or [],
                    "executed_steps": executed_steps,
                    "artifacts_dir": state.get("artifacts_dir"),
                    "artifacts": artifacts,
                    "broadcast": "；".join(aggregated_broadcasts) if aggregated_broadcasts else "workflow complete",
                }
                result.update(self.build_final_result_contract(artifact_path=final_result_path, artifact_data=final_result))
                callback_info = {"callback_status": "skipped", "callback_mode": "none"}
                final_prompt_for_callback = None
                if isinstance(result.get("result_for_caller"), dict):
                    final_prompt_for_callback = result["result_for_caller"].get("final_prompt")
                if isinstance(final_prompt_for_callback, str) and final_prompt_for_callback.strip():
                    callback_info = self.send_callback(
                        callback_session_id=req.get("callback_session_id"),
                        callback_session_key=req.get("callback_session_key"),
                        final_prompt=final_prompt_for_callback,
                    )
                result.update(callback_info)
                preserved_result = compact(
                    {
                        key: value
                        for key, value in result.items()
                        if key
                        not in {
                            "final_result",
                            "final_report",
                            "final_result_path",
                            "final_report_path",
                            "final_contract",
                            "result_for_caller",
                            "render_payload",
                            "render_schema",
                            "render_prompt",
                            "caller_prompt",
                        }
                    },
                )
                preserved_result["final_result_path"] = final_result_path
                preserved_result["final_report_path"] = final_result_path
                preserved_result["final_result"] = final_result
                preserved_result["final_report"] = final_result
                if "result_for_caller" in result:
                    preserved_result["result_for_caller"] = result["result_for_caller"]
                if "render_payload" in result:
                    preserved_result["render_payload"] = result["render_payload"]
                if "render_schema" in result:
                    preserved_result["render_schema"] = result["render_schema"]
                if "render_prompt" in result:
                    preserved_result["render_prompt"] = result["render_prompt"]
                if "caller_prompt" in result:
                    preserved_result["caller_prompt"] = result["caller_prompt"]
                if "caller_prompt_path" in result:
                    preserved_result["caller_prompt_path"] = result["caller_prompt_path"]
                if "final_contract" in result:
                    preserved_result["final_contract"] = result["final_contract"]
                return preserved_result

            if (
                state.get("pause")
                or state.get("waiting_for_user")
                or state.get("blocked")
                or state.get("failed")
                or current_next.get("pause_point")
            ):
                return compact(
                    {
                        "workflow_id": workflow_id,
                        "run_id": run_id,
                        "status": state.get("status"),
                        "warnings": state.get("warnings") or [],
                        "pause": state.get("pause"),
                        "waiting_for_user": state.get("waiting_for_user"),
                        "blocked": state.get("blocked"),
                        "auto_continue": False,
                        "failed": state.get("failed"),
                        "failure_reason": state.get("failure_reason"),
                        "failed_step": state.get("failed_step"),
                        "retryable": state.get("retryable"),
                        "current_step": state.get("current_step"),
                        "next_step": self.step_payload(current_next, state),
                        "completed_steps": state.get("completed_steps") or [],
                        "executed_steps": executed_steps,
                        "artifacts_dir": state.get("artifacts_dir"),
                        "artifacts": state.get("artifacts") or {},
                        "broadcast": "；".join(aggregated_broadcasts) if aggregated_broadcasts else None,
                    },
                )

            runnable_step = self.step_payload(current_next, state)
            try:
                if current_next.get("kind") == "script":
                    artifact = execute_script_step(self.runtime, runnable_step, run_id)
                elif current_next.get("kind") == "model":
                    artifact = execute_model_step(self.runtime, runnable_step, run_id)
                elif current_next.get("kind") == "final":
                    artifact = execute_final_step(self.runtime, runnable_step, run_id)
                else:
                    raise RuntimeError(f'unsupported step kind: {current_next.get("kind")}')
            except Exception as exc:
                state["failed"] = True
                state["status"] = "failed"
                state["failure_reason"] = str(exc)
                state["failed_step"] = current_next["id"]
                state["retryable"] = True
                step_state = state["step_states"][current_next["id"]]
                step_state["status"] = "failed"
                step_state["failed_at"] = now_iso()
                self.save_state(run_id, state)
                self.append_trace(run_id, {"event": "step_failed", "step_id": current_next["id"], "error": str(exc)})
                return compact(
                    {
                        "workflow_id": workflow_id,
                        "run_id": run_id,
                        "status": "failed",
                        "failed": True,
                        "failure_reason": str(exc),
                        "failed_step": current_next["id"],
                        "retryable": True,
                        "current_step": state.get("current_step"),
                        "next_step": self.step_payload(current_next, state),
                        "completed_steps": state.get("completed_steps") or [],
                        "executed_steps": executed_steps,
                        "artifacts_dir": state.get("artifacts_dir"),
                        "artifacts": state.get("artifacts") or {},
                        "broadcast": "；".join(aggregated_broadcasts) if aggregated_broadcasts else None,
                    },
                )

            self.finish_step(state, current_next, artifact=artifact)
            state["current_step"] = current_next["id"]
            state.setdefault("completed_steps", []).append(current_next["id"])
            state.setdefault("artifacts", {})[current_next["id"]] = artifact
            self.append_trace(run_id, {"event": "step_completed", "step_id": current_next["id"], "artifact": artifact})
            executed_steps.append(
                {
                    "id": current_next["id"],
                    "number": current_next["number"],
                    "name": current_next["name"],
                    "kind": current_next["kind"],
                    "artifact": artifact,
                },
            )
            aggregated_broadcasts.append(f'第{current_next["number"]}步 {current_next["name"]} 执行成功')
            final_result_path = artifact
            final_result = self.read_final_artifact(artifact)

            next_step = self.next_step(spec, current_next["id"])
            if next_step is None:
                state["done"] = True
                state["status"] = "done"
                state["finished_at"] = now_iso()
                self.append_trace(
                    run_id,
                    {
                        "event": "workflow_completed",
                        "workflow_id": workflow_id,
                        "run_id": run_id,
                    },
                )
            else:
                self.begin_step(state, next_step)
                self.append_trace(
                    run_id,
                    {
                        "event": "step_started",
                        "step_id": next_step["id"],
                        "actor": next_step["actor"],
                        "kind": next_step["kind"],
                    },
                )
            self.save_state(run_id, state)

    def _advance_completed_step(
        self,
        *,
        action: str,
        req: dict[str, Any],
        workflow_id: str,
        run_id: str,
        spec: dict[str, Any],
        state: dict[str, Any],
        step_map: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        completed_step = req.get("completed_step")
        next_step = self.next_step(spec, state.get("current_step"))
        if next_step is None:
            state["done"] = True
            state["status"] = "done"
            state["finished_at"] = now_iso()
            self.save_state(run_id, state)
            self.append_trace(
                run_id,
                {
                    "event": "workflow_completed",
                    "workflow_id": workflow_id,
                    "run_id": run_id,
                },
            )
            return {"workflow_id": workflow_id, "run_id": run_id, "status": "done"}
        if completed_step != next_step["id"]:
            raise SystemExit(f'expected completed_step={next_step["id"]}, got {completed_step}')

        artifact = req.get("artifact") if action == "complete" else None
        self.finish_step(state, next_step, artifact=artifact)
        state["current_step"] = completed_step
        state.setdefault("completed_steps", []).append(completed_step)
        if artifact:
            state.setdefault("artifacts", {})[completed_step] = artifact
        self.append_trace(run_id, {"event": "step_completed", "step_id": completed_step, "artifact": artifact})

        following_step = self.next_step(spec, completed_step)
        if following_step is None:
            state["done"] = True
            state["status"] = "done"
            state["finished_at"] = now_iso()
            self.append_trace(
                run_id,
                {
                    "event": "workflow_completed",
                    "workflow_id": workflow_id,
                    "run_id": run_id,
                },
            )
        else:
            self.begin_step(state, following_step)
            self.append_trace(
                run_id,
                {
                    "event": "step_started",
                    "step_id": following_step["id"],
                    "actor": following_step["actor"],
                    "kind": following_step["kind"],
                },
            )
        self.save_state(run_id, state)
        completed_meta = step_map[completed_step]
        return compact(
            {
                "workflow_id": workflow_id,
                "run_id": run_id,
                "status": state.get("status"),
                "warnings": state.get("warnings") or [],
                "pause": state.get("pause"),
                "waiting_for_user": state.get("waiting_for_user"),
                "blocked": state.get("blocked"),
                "auto_continue": state.get("auto_continue"),
                "failed": state.get("failed"),
                "failure_reason": state.get("failure_reason"),
                "failed_step": state.get("failed_step"),
                "retryable": state.get("retryable"),
                "started_at": state.get("started_at"),
                "updated_at": state.get("updated_at"),
                "finished_at": state.get("finished_at"),
                "current_step": completed_step,
                "next_step": self.step_payload(following_step, state),
                "completed_steps": state.get("completed_steps") or [],
                "broadcast": f'第{completed_meta["number"]}步 {completed_meta["name"]} 执行成功',
                "step_start_broadcast": (
                    f'打算进行：第{following_step["number"]}步 {following_step["name"]}' if following_step else None
                ),
                "artifacts_dir": state.get("artifacts_dir"),
                "artifacts": state.get("artifacts") or {},
                "step_states": state.get("step_states") or {},
                "context": state.get("context") or {},
            },
        )
