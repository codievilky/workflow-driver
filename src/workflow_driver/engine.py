from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .config import RuntimeContext
from .executor import execute_final_step, execute_model_step, execute_multi_output_script_step, execute_script_step, run_transform_script
from .utils import compact, configure_log_file, now_iso, now_stamp, read_json, read_yaml, stderr_log, write_json

FINAL_RESULT_RESERVED_KEYS = {
    "final_render_payload",
    "final_render_schema",
    "final_render_prompt",
}


class WorkflowEngine:
    def __init__(self, runtime: RuntimeContext):
        self.runtime = runtime

    @staticmethod
    def step_label(step: dict[str, Any]) -> str:
        return f'第{step["number"]}步 {step["name"]} ({step["kind"]})'

    def log(self, message: str) -> None:
        stderr_log(message)

    def load_spec(self, spec_path: str) -> tuple[dict[str, Any], Path]:
        resolved_spec_path = self.runtime.resolve_path(spec_path)
        data = read_yaml(resolved_spec_path)
        if not isinstance(data, dict):
            raise SystemExit("invalid workflow spec yaml")
        return data, resolved_spec_path

    @staticmethod
    def step_index(spec: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[int, dict[str, Any]]]:
        by_id = {step["id"]: step for step in spec["steps"]}
        by_number = {int(step["number"]): step for step in spec["steps"]}
        return by_id, by_number

    @staticmethod
    def default_artifact_name(step: dict[str, Any]) -> str:
        outputs = step.get("outputs") or {}
        if outputs:
            first_output = next(iter(outputs.values()))
            return first_output.get("artifact_name") or f'step{step["number"]}_{next(iter(outputs))}.json'
        return step.get("default_artifact_name") or f'step{step["number"]}_{step.get("output_key") or step["id"]}.json'

    @staticmethod
    def build_output_index(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Build index: output_key → {"step_id": ..., "artifact_name": ...}

        Covers steps that declare an `outputs` map (multi-output steps).
        """
        index: dict[str, dict[str, Any]] = {}
        for step in spec.get("steps") or []:
            for output_key, output_spec in (step.get("outputs") or {}).items():
                index[output_key] = {
                    "step_id": step["id"],
                    "artifact_name": (output_spec or {}).get("artifact_name") or f"{output_key}.json",
                }
        return index

    def default_run_id(self, workflow_id: str, day_id: Any) -> str:
        if day_id is not None:
            return f"{workflow_id}-{day_id}-{now_stamp()}"
        return f"{workflow_id}-{now_stamp()}"

    def default_data_dir(self) -> Path:
        return self.runtime.resolve_path("tmp")

    @staticmethod
    def default_trace_log_name(run_id: str) -> str:
        return f"{run_id}.trace.log"

    def artifact_path(self, run_ctx: dict[str, Any], step: dict[str, Any]) -> Path:
        return run_ctx["data_dir"] / self.default_artifact_name(step)

    def built_output_path(
        self,
        run_ctx: dict[str, Any],
        current_step: dict[str, Any],
        source_name: str,
        source: dict[str, Any],
    ) -> Path:
        output_name = source.get("output_name") or f'step{current_step["number"]}_{source_name}.json'
        return run_ctx["data_dir"] / output_name

    def build_run_context(self, options: dict[str, Any]) -> dict[str, Any]:
        spec, resolved_spec_path = self.load_spec(options["spec_path"])
        step_by_id, step_by_number = self.step_index(spec)
        workflow_id = spec.get("workflow_id") or "workflow"
        run_id = options.get("run_id") or self.default_run_id(workflow_id, options.get("day_id"))
        data_dir = self.runtime.resolve_path(options["data_dir"]) if options.get("data_dir") else self.default_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)

        state_values = dict(options.get("state_values") or {})
        if options.get("day_id") is not None:
            state_values["day_id"] = options["day_id"]

        target_number = options.get("step_number") or max(step_by_number)
        target_step = step_by_number.get(int(target_number))
        if target_step is None:
            raise SystemExit(f"step number not found: {target_number}")

        return {
            "spec": spec,
            "spec_path": options["spec_path"],
            "spec_dir": resolved_spec_path.parent,
            "workflow_id": workflow_id,
            "run_id": run_id,
            "data_dir": data_dir,
            "step_by_id": step_by_id,
            "step_by_number": step_by_number,
            "target_step": target_step,
            "target_step_number": int(target_number),
            "state_values": state_values,
            "context_values": dict(options.get("context_values") or {}),
            "skill": options.get("skill") or "",
            "force": bool(options.get("force")),
            "callback_session_id": options.get("callback_session_id"),
            "callback_session_key": options.get("callback_session_key"),
            "output_index": self.build_output_index(spec),
            "artifact_cache": {},
            "artifact_paths": {},
            "executed_steps": [],
            "reused_steps": [],
            "built_cache": {},
            "built_outputs": {},
        }

    def resolve_source(
        self,
        run_ctx: dict[str, Any],
        current_step: dict[str, Any],
        source_name: str,
        source: dict[str, Any],
    ) -> Any:
        kind = source.get("kind")
        if kind == "state":
            self.log(f'[input] {self.step_label(current_step)} 读取 state `{source["key"]}`')
            return run_ctx["state_values"].get(source["key"])
        if kind == "context":
            self.log(f'[input] {self.step_label(current_step)} 读取 context `{source["key"]}`')
            return run_ctx["context_values"].get(source["key"])
        if kind == "artifact":
            artifact_id = source["artifact"]
            output_info = run_ctx["output_index"].get(artifact_id)
            if output_info:
                # Named output from a multi-output step
                artifact_file_path = run_ctx["data_dir"] / output_info["artifact_name"]
                self.log(
                    f'[input] {self.step_label(current_step)} 准备读取多输出文件 '
                    f'{self.runtime.to_output_path(artifact_file_path)} '
                    f'(来自步骤 {output_info["step_id"]})'
                )
                self.ensure_step(run_ctx, output_info["step_id"])
                return read_json(artifact_file_path)
            # Standard single-output step
            producer_step = run_ctx["step_by_id"][artifact_id]
            self.log(
                f'[input] {self.step_label(current_step)} 准备读取文件 '
                f'{self.runtime.to_output_path(self.artifact_path(run_ctx, producer_step))}'
            )
            producer = self.ensure_step(run_ctx, artifact_id)
            return producer["artifact_data"]
        if kind == "artifact_field":
            producer_step = run_ctx["step_by_id"][source["artifact"]]
            self.log(
                f'[input] {self.step_label(current_step)} 准备读取文件 '
                f'{self.runtime.to_output_path(self.artifact_path(run_ctx, producer_step))} '
                f'字段 `{source["field"]}`'
            )
            producer = self.ensure_step(run_ctx, source["artifact"])
            artifact_data = producer["artifact_data"]
            if not isinstance(artifact_data, dict):
                return None
            return artifact_data.get(source["field"])
        if kind == "built":
            return self.resolve_built_source(run_ctx, current_step, source_name, source)
        raise RuntimeError(f"unsupported input source kind: {kind}")

    def resolve_built_source(
        self,
        run_ctx: dict[str, Any],
        current_step: dict[str, Any],
        source_name: str,
        source: dict[str, Any],
    ) -> Any:
        cache_key = f'{current_step["id"]}:{source_name}'
        output_path = self.built_output_path(run_ctx, current_step, source_name, source)
        if cache_key in run_ctx["built_cache"]:
            return run_ctx["built_cache"][cache_key]
        if output_path.exists() and not run_ctx["force"]:
            self.log(
                f'[reuse] {self.step_label(current_step)} 复用生成文件 '
                f'{self.runtime.to_output_path(output_path)}'
            )
            built_obj = read_json(output_path)
            run_ctx["built_cache"][cache_key] = built_obj
            run_ctx["built_outputs"][cache_key] = output_path
            return built_obj

        builder_payload: dict[str, Any] = {}
        for builder_key, builder_source in (source.get("builder_payload") or {}).items():
            builder_payload[builder_key] = self.resolve_source(run_ctx, current_step, builder_key, builder_source)

        self.log(
            f'[build] {self.step_label(current_step)} 生成中间文件 '
            f'{self.runtime.to_output_path(output_path)} '
            f'使用脚本 {source["builder"]}'
        )
        built_obj = run_transform_script(
            self.runtime,
            script_ref=source["builder"],
            spec_dir=run_ctx["spec_dir"],
            project_root_ref=None,
            payload=builder_payload,
            tmp_namespace="built-sources",
        )
        write_json(output_path, built_obj)
        self.log(f'[write] 已生成文件 {self.runtime.to_output_path(output_path)}')
        run_ctx["built_cache"][cache_key] = built_obj
        run_ctx["built_outputs"][cache_key] = output_path
        return built_obj

    def resolve_step_inputs(self, run_ctx: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
        resolved_inputs: dict[str, Any] = {}
        for source_name, source in (step.get("input_sources") or {}).items():
            resolved_inputs[source_name] = self.resolve_source(run_ctx, step, source_name, source)
        missing = [
            name
            for name in (step.get("inputs") or [])
            if name not in resolved_inputs or resolved_inputs[name] is None
        ]
        if missing:
            raise RuntimeError(f'step {step["id"]} missing required inputs: {", ".join(missing)}')
        return resolved_inputs

    def build_step_input_refs(self, run_ctx: dict[str, Any], step: dict[str, Any]) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        input_names = list(step.get("inputs") or [])
        if not input_names:
            input_names = list((step.get("input_sources") or {}).keys())

        for source_name in input_names:
            source = (step.get("input_sources") or {}).get(source_name)
            if not isinstance(source, dict):
                continue

            kind = source.get("kind")
            ref: dict[str, Any] = {
                "name": source_name,
                "kind": kind,
                "description": source.get("description") or "",
            }

            if kind == "state":
                ref["state_key"] = source.get("key")
            elif kind == "context":
                ref["context_key"] = source.get("key")
            elif kind == "artifact":
                artifact_id = source.get("artifact")
                output_info = run_ctx["output_index"].get(artifact_id)
                if output_info:
                    ref["path"] = str(run_ctx["data_dir"] / output_info["artifact_name"])
                    ref["producer_step_id"] = output_info["step_id"]
                elif artifact_id in run_ctx["step_by_id"]:
                    ref["path"] = str(self.artifact_path(run_ctx, run_ctx["step_by_id"][artifact_id]))
                    ref["producer_step_id"] = artifact_id
            elif kind == "artifact_field":
                artifact_id = source.get("artifact")
                ref["field"] = source.get("field")
                if artifact_id in run_ctx["step_by_id"]:
                    ref["path"] = str(self.artifact_path(run_ctx, run_ctx["step_by_id"][artifact_id]))
                    ref["producer_step_id"] = artifact_id
            elif kind == "built":
                ref["path"] = str(self.built_output_path(run_ctx, step, source_name, source))
                ref["builder"] = source.get("builder")

            refs.append(ref)

        return refs

    def mark_reused(self, run_ctx: dict[str, Any], step: dict[str, Any], artifact_path: Path) -> None:
        if step["id"] in {item["id"] for item in run_ctx["reused_steps"]}:
            return
        run_ctx["reused_steps"].append(
            {
                "id": step["id"],
                "number": step["number"],
                "name": step["name"],
                "artifact": self.runtime.to_output_path(artifact_path),
            },
        )

    def mark_executed(self, run_ctx: dict[str, Any], step: dict[str, Any], artifact_path: Path) -> None:
        run_ctx["executed_steps"].append(
            {
                "id": step["id"],
                "number": step["number"],
                "name": step["name"],
                "kind": step["kind"],
                "artifact": self.runtime.to_output_path(artifact_path),
            },
        )

    def ensure_step(self, run_ctx: dict[str, Any], step_id: str) -> dict[str, Any]:
        if step_id in run_ctx["artifact_cache"]:
            return {
                "artifact_path": run_ctx["artifact_paths"][step_id],
                "artifact_data": run_ctx["artifact_cache"][step_id],
            }

        step = run_ctx["step_by_id"].get(step_id)
        if step is None:
            raise RuntimeError(f"unknown step id: {step_id}")

        artifact_path = self.artifact_path(run_ctx, step)
        if artifact_path.exists() and not run_ctx["force"]:
            self.log(f'[reuse] {self.step_label(step)} 复用产物 {self.runtime.to_output_path(artifact_path)}')
            artifact_data = read_json(artifact_path)
            run_ctx["artifact_cache"][step_id] = artifact_data
            run_ctx["artifact_paths"][step_id] = artifact_path
            self.mark_reused(run_ctx, step, artifact_path)
            return {"artifact_path": artifact_path, "artifact_data": artifact_data}

        self.log(
            f'[step] 开始执行 {self.step_label(step)}，目标产物 '
            f'{self.runtime.to_output_path(artifact_path)}'
        )
        resolved_inputs = self.resolve_step_inputs(run_ctx, step)
        wrote_directly = False
        if step.get("kind") == "script":
            if step.get("outputs"):
                # Multi-output: pass primary output path directly so the script
                # derives the output directory and writes all files there.
                artifact_data = execute_multi_output_script_step(
                    self.runtime,
                    step=step,
                    spec_dir=run_ctx["spec_dir"],
                    raw_inputs=resolved_inputs,
                    output_path=artifact_path,
                )
                wrote_directly = True
            else:
                artifact_data = execute_script_step(
                    self.runtime,
                    step=step,
                    spec_dir=run_ctx["spec_dir"],
                    raw_inputs=resolved_inputs,
                )
        elif step.get("kind") == "model":
            artifact_data = execute_model_step(
                self.runtime,
                step=step,
                spec_dir=run_ctx["spec_dir"],
                raw_inputs=resolved_inputs,
                input_refs=self.build_step_input_refs(run_ctx, step),
                run_id=run_ctx["run_id"],
                skill=run_ctx["skill"],
                data_dir=run_ctx["data_dir"],
            )
        elif step.get("kind") == "final":
            artifact_data = execute_final_step(step=step, resolved_inputs=resolved_inputs)
        else:
            raise RuntimeError(f'unsupported step kind: {step.get("kind")}')

        if not wrote_directly:
            write_json(artifact_path, artifact_data)
        self.log(f'[write] 已生成文件 {self.runtime.to_output_path(artifact_path)}')
        if step.get("kind") == "final" and isinstance(artifact_data, dict) and artifact_data.get("final_prompt"):
            artifact_path.with_suffix(".prompt.txt").write_text(artifact_data["final_prompt"], encoding="utf-8")
            self.log(
                f'[write] 已生成文件 '
                f'{self.runtime.to_output_path(artifact_path.with_suffix(".prompt.txt"))}'
            )
        run_ctx["artifact_cache"][step_id] = artifact_data
        run_ctx["artifact_paths"][step_id] = artifact_path
        self.mark_executed(run_ctx, step, artifact_path)
        return {"artifact_path": artifact_path, "artifact_data": artifact_data}

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
            self.log(f"[callback] 准备执行 openclaw session 回传 session_id={callback_session_id}")
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
                self.log(f"[callback] openclaw session 回传成功 session_id={callback_session_id}")
                return {
                    "callback_cli_invoked": True,
                    "callback_status": "sent",
                    "callback_mode": "session",
                    "callback_session_id": callback_session_id,
                }
            callback_error = (proc.stderr or proc.stdout).strip()
            self.log(f"[callback] openclaw session 回传失败 session_id={callback_session_id} error={callback_error}")
            return {
                "callback_cli_invoked": True,
                "callback_status": "failed",
                "callback_mode": "session",
                "callback_session_id": callback_session_id,
                "callback_error": callback_error,
            }

        target = self.parse_callback_target(callback_session_key)
        if target:
            self.log(
                f'[callback] 准备执行 openclaw channel 回传 agent={target["agent"]} '
                f'channel={target["channel"]} reply_to={target["reply_to"]}'
            )
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
                self.log(
                    f'[callback] openclaw channel 回传成功 agent={target["agent"]} '
                    f'channel={target["channel"]} reply_to={target["reply_to"]}'
                )
                return {
                    "callback_cli_invoked": True,
                    "callback_status": "sent",
                    "callback_mode": "channel_reply",
                    "callback_target": target,
                }
            callback_error = (proc.stderr or proc.stdout).strip()
            self.log(
                f'[callback] openclaw channel 回传失败 agent={target["agent"]} '
                f'channel={target["channel"]} reply_to={target["reply_to"]} error={callback_error}'
            )
            return {
                "callback_cli_invoked": True,
                "callback_status": "failed",
                "callback_mode": "channel_reply",
                "callback_target": target,
                "callback_error": callback_error,
            }

        self.log("[callback] 跳过 openclaw 回传：未提供可识别的 callback 目标")
        return {
            "callback_cli_invoked": False,
            "callback_status": "skipped",
            "callback_mode": "none",
        }

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

    def run(self, options: dict[str, Any]) -> dict[str, Any]:
        run_ctx = self.build_run_context(options)
        run_ctx["log_path"] = run_ctx["data_dir"] / self.default_trace_log_name(run_ctx["run_id"])
        configure_log_file(run_ctx["log_path"])
        self.log(f'[log] 写入运行日志 {self.runtime.to_output_path(run_ctx["log_path"])}')
        self.log(
            f'[run] workflow={run_ctx["workflow_id"]} target={self.step_label(run_ctx["target_step"])} '
            f'data_dir={self.runtime.to_output_path(run_ctx["data_dir"])}'
        )
        target_result = self.ensure_step(run_ctx, run_ctx["target_step"]["id"])
        artifact_path = self.runtime.to_output_path(target_result["artifact_path"])
        artifact_data = target_result["artifact_data"]

        result = compact(
            {
                "workflow_id": run_ctx["workflow_id"],
                "run_id": run_ctx["run_id"],
                "spec_path": run_ctx["spec_path"],
                "target_step": {
                    "id": run_ctx["target_step"]["id"],
                    "number": run_ctx["target_step"]["number"],
                    "name": run_ctx["target_step"]["name"],
                    "kind": run_ctx["target_step"]["kind"],
                },
                "data_dir": self.runtime.to_output_path(run_ctx["data_dir"]),
                "log_path": self.runtime.to_output_path(run_ctx["log_path"]),
                "executed_steps": run_ctx["executed_steps"],
                "reused_steps": run_ctx["reused_steps"],
                "artifacts": {
                    step_id: self.runtime.to_output_path(path)
                    for step_id, path in run_ctx["artifact_paths"].items()
                },
                "built_outputs": {
                    key: self.runtime.to_output_path(path)
                    for key, path in run_ctx["built_outputs"].items()
                },
            },
        )
        result.update(self.build_final_result_contract(artifact_path=artifact_path, artifact_data=artifact_data))

        callback_info = {
            "callback_cli_invoked": False,
            "callback_status": "skipped",
            "callback_mode": "none",
        }
        final_prompt_for_callback = None
        if isinstance(result.get("result_for_caller"), dict):
            final_prompt_for_callback = result["result_for_caller"].get("final_prompt")
        if isinstance(final_prompt_for_callback, str) and final_prompt_for_callback.strip():
            callback_info = self.send_callback(
                callback_session_id=run_ctx.get("callback_session_id"),
                callback_session_key=run_ctx.get("callback_session_key"),
                final_prompt=final_prompt_for_callback,
            )
        else:
            self.log("[callback] 跳过 openclaw 回传：结果中没有 final_prompt")
        result.update(callback_info)
        self.log(
            f'[summary] callback_cli_invoked={"yes" if result.get("callback_cli_invoked") else "no"} '
            f'callback_status={result.get("callback_status", "unknown")} '
            f'callback_mode={result.get("callback_mode", "unknown")}'
        )
        result["finished_at"] = now_iso()
        return result
