from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib import error, request

from .config import RuntimeContext
from .utils import read_json, stderr_log

POLL_INTERVAL_SECONDS = 3.0
MAX_HISTORY_MESSAGES = 50
REPLY_SKIP_SENTINEL = "REPLY_SKIP"
ANNOUNCE_SKIP_SENTINEL = "ANNOUNCE_SKIP"
MODEL_SCHEMA_RETRY_LIMIT = 2


def resolve_step_path(runtime: RuntimeContext, spec_dir: Path, path_ref: str | Path) -> Path:
    return runtime.resolve_path(path_ref, base_dir=spec_dir)


def resolve_step_project_root(
    runtime: RuntimeContext,
    spec_dir: Path,
    project_root_ref: str | None,
) -> Path:
    if project_root_ref:
        return resolve_step_path(runtime, spec_dir, project_root_ref)
    return spec_dir


def extract_json_object(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        raise RuntimeError("empty result")
    try:
        return json.loads(text)
    except Exception:
        pass

    lines = [line for line in text.splitlines() if not line.startswith("[")]
    cleaned = "\n".join(lines).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        pass

    in_string = False
    escape = False
    depth = 0
    start = None
    candidates: list[str] = []
    for idx, char in enumerate(cleaned):
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    candidates.append(cleaned[start : idx + 1])
                    start = None
    for candidate in reversed(candidates):
        try:
            return json.loads(candidate)
        except Exception:
            continue
    raise RuntimeError(f"invalid JSON result: {text[:1000]}")


def load_json_text(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception as exc:
        raise RuntimeError(f"invalid JSON text: {text[:1000]}") from exc


def invoke_gateway_tool(runtime: RuntimeContext, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    url, token = runtime.load_gateway_settings()
    request_body = {"tool": tool, "args": args}
    body = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
    if runtime.debug:
        stderr_log(
            f"[debug][gateway] tool={tool} url={url}\n"
            f"[debug][gateway] request_body=\n{json.dumps(request_body, ensure_ascii=False, indent=2)}"
        )
    req = request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=60) as response:
            payload = response.read().decode("utf-8")
    except error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"gateway HTTP {exc.code}: {payload}") from exc
    except Exception as exc:
        raise RuntimeError(f"gateway invoke failed: {exc}") from exc
    if runtime.debug:
        stderr_log(f"[debug][gateway] tool={tool} raw_response=\n{payload}")
    try:
        obj = json.loads(payload)
    except Exception as exc:
        raise RuntimeError(f"invalid gateway JSON: {payload[:1000]}") from exc
    if obj.get("ok") is False:
        raise RuntimeError(json.dumps(obj, ensure_ascii=False))
    if isinstance(obj.get("details"), dict):
        return obj["details"]
    if isinstance(obj.get("result"), dict):
        if isinstance(obj["result"].get("details"), dict):
            return obj["result"]["details"]
        return obj["result"]
    return obj


def run_python_step(
    runtime: RuntimeContext,
    *,
    script_path: Path,
    payload: dict[str, Any],
    project_root: Path | None = None,
    tmp_namespace: str = "driver",
) -> str:
    tmp_dir = runtime.resolve_under_state_root(".workflow-driver-internal", tmp_namespace)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    input_path = tmp_dir / ".executor-input.json"
    output_path = tmp_dir / ".executor-output.json"
    input_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    stderr_log(
        f"[script] 执行脚本 {script_path} "
        f"(input={runtime.to_output_path(input_path)}, output={runtime.to_output_path(output_path)})"
    )
    command = runtime.build_script_command(
        script_path=script_path,
        input_path=input_path,
        output_path=output_path,
        project_root=project_root,
    )
    proc = subprocess.run(
        command,
        text=True,
        capture_output=True,
        cwd=project_root or runtime.workspace,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip())
    if not output_path.exists():
        raise RuntimeError(f"step output not produced: {output_path}")
    return output_path.read_text(encoding="utf-8")


def run_transform_script(
    runtime: RuntimeContext,
    *,
    script_ref: str,
    spec_dir: Path,
    project_root_ref: str | None,
    payload: dict[str, Any],
    tmp_namespace: str,
) -> Any:
    script_path = resolve_step_path(runtime, spec_dir, script_ref)
    project_root = resolve_step_project_root(runtime, spec_dir, project_root_ref)
    output_text = run_python_step(
        runtime,
        script_path=script_path,
        payload=payload,
        project_root=project_root,
        tmp_namespace=tmp_namespace,
    )
    stderr_log(f"[script] 脚本执行完成 {script_path}")
    return load_json_text(output_text)


def build_model_input(
    runtime: RuntimeContext,
    *,
    step: dict[str, Any],
    spec_dir: Path,
    raw_inputs: dict[str, Any],
) -> Any:
    execution = step.get("execution") or {}
    input_builder_ref = execution.get("input_builder") or step.get("input_builder")
    project_root_ref = execution.get("project_root")
    if not input_builder_ref:
        return raw_inputs
    return run_transform_script(
        runtime,
        script_ref=input_builder_ref,
        spec_dir=spec_dir,
        project_root_ref=project_root_ref,
        payload=raw_inputs,
        tmp_namespace="model-builders",
    )


def build_input_refs_text(runtime: RuntimeContext, input_refs: list[dict[str, Any]]) -> str:
    if not input_refs:
        return ""

    lines: list[str] = []
    for ref in input_refs:
        name = str(ref.get("name") or "").strip()
        kind = str(ref.get("kind") or "").strip()
        description = str(ref.get("description") or "").strip()
        path_text = str(ref.get("path") or "").strip()
        field = str(ref.get("field") or "").strip()
        state_key = str(ref.get("state_key") or "").strip()
        context_key = str(ref.get("context_key") or "").strip()

        summary = description or f"输入 `{name}`"
        line = f"- {name}：{summary}"

        extra_parts: list[str] = []
        if path_text:
            extra_parts.append(f"文件={runtime.to_output_path(Path(path_text))}")
        elif kind == "state" and state_key:
            extra_parts.append(f"来源=state `{state_key}`")
        elif kind == "context" and context_key:
            extra_parts.append(f"来源=context `{context_key}`")

        if field:
            extra_parts.append(f"字段=`{field}`")

        if extra_parts:
            line += f"（{'，'.join(extra_parts)}）"
        lines.append(line)

    return "\n".join(lines)


def materialize_input_refs(
    runtime: RuntimeContext,
    *,
    step: dict[str, Any],
    raw_inputs: dict[str, Any],
    input_refs: list[dict[str, Any]],
    data_dir: Path | None,
) -> list[dict[str, Any]]:
    ref_root = data_dir or runtime.resolve_under_state_root(".workflow-driver-internal", "model-input-refs")
    ref_root.mkdir(parents=True, exist_ok=True)

    materialized_refs: list[dict[str, Any]] = []
    for ref in input_refs:
        copied = dict(ref)
        name = str(copied.get("name") or "").strip()
        if not name:
            continue

        needs_materialize = not copied.get("path") or bool(copied.get("field"))
        if needs_materialize:
            output_path = ref_root / f'step{step["number"]}_{step["id"]}_{name}.json'
            output_path.write_text(
                json.dumps(raw_inputs.get(name), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            copied["path"] = str(output_path)
            copied.pop("field", None)
            copied["materialized"] = True
        materialized_refs.append(copied)

    return materialized_refs


def extract_message_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                output = item.get("output")
                if isinstance(output, str):
                    parts.append(output)
                result = item.get("result")
                if isinstance(result, str):
                    parts.append(result)
                elif isinstance(result, dict):
                    try:
                        parts.append(json.dumps(result, ensure_ascii=False))
                    except Exception:
                        pass
        return "\n".join(part for part in parts if part)
    text = message.get("text")
    if isinstance(text, str):
        return text
    return ""


def get_messages(history_obj: Any) -> list[dict[str, Any]]:
    if isinstance(history_obj, list):
        return [message for message in history_obj if isinstance(message, dict)]
    if isinstance(history_obj, dict):
        messages = history_obj.get("messages")
        if isinstance(messages, list):
            return [message for message in messages if isinstance(message, dict)]
        details = history_obj.get("details")
        if isinstance(details, dict) and isinstance(details.get("messages"), list):
            return [message for message in details["messages"] if isinstance(message, dict)]
        result = history_obj.get("result")
        if isinstance(result, dict):
            inner_messages = result.get("messages")
            if isinstance(inner_messages, list):
                return [message for message in inner_messages if isinstance(message, dict)]
            inner_details = result.get("details")
            if isinstance(inner_details, dict) and isinstance(inner_details.get("messages"), list):
                return [message for message in inner_details["messages"] if isinstance(message, dict)]
        content = history_obj.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                    try:
                        parsed = json.loads(item["text"])
                    except Exception:
                        continue
                    if isinstance(parsed, dict) and isinstance(parsed.get("messages"), list):
                        return [message for message in parsed["messages"] if isinstance(message, dict)]
            return [{"role": "assistant", "content": content}]
    return []


def find_latest_assistant_json(messages: list[dict[str, Any]]) -> Any:
    final_like: list[dict[str, Any]] = []
    fallback: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        stop_reason = message.get("stopReason")
        if stop_reason == "stop":
            final_like.append(message)
        else:
            fallback.append(message)
    candidates = list(reversed(final_like)) + list(reversed(fallback))
    for candidate in candidates:
        text = extract_message_text(candidate)
        if not text or text.strip() in {REPLY_SKIP_SENTINEL, ANNOUNCE_SKIP_SENTINEL}:
            continue
        try:
            return extract_json_object(text)
        except Exception:
            continue
    raise RuntimeError("assistant JSON result not found in session history")


def run_session_model_step(
    runtime: RuntimeContext,
    agent: str,
    message: str,
    *,
    workspace: str | None = None,
    timeout_seconds: int = 600,
    session_label: str | None = None,
) -> Any:
    spawn_args: dict[str, Any] = {
        "task": session_label or "workflow-model-step",
        "agentId": agent,
        "cleanup": "keep",
    }
    if workspace:
        spawn_args["workspace"] = workspace
    spawn_result = invoke_gateway_tool(
        runtime,
        "sessions_spawn",
        spawn_args,
    )
    child_session_key = spawn_result.get("childSessionKey")
    if not child_session_key:
        raise RuntimeError(f"sessions_spawn missing childSessionKey: {json.dumps(spawn_result, ensure_ascii=False)}")

    send_result = invoke_gateway_tool(
        runtime,
        "sessions_send",
        {
            "sessionKey": child_session_key,
            "message": message,
            "timeoutSeconds": 0,
        },
    )
    if isinstance(send_result, dict) and send_result.get("status") not in (None, "accepted", "ok", "timeout"):
        raise RuntimeError(f"sessions_send failed: {json.dumps(send_result, ensure_ascii=False)}")

    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        history_result = invoke_gateway_tool(
            runtime,
            "sessions_history",
            {
                "sessionKey": child_session_key,
                "limit": MAX_HISTORY_MESSAGES,
                "includeTools": True,
            },
        )
        messages = get_messages(history_result)
        try:
            return find_latest_assistant_json(messages)
        except Exception as exc:
            last_error = exc
        time.sleep(POLL_INTERVAL_SECONDS)
    raise RuntimeError(f"model step timed out waiting for child session result: {last_error}")


def execute_script_step(
    runtime: RuntimeContext,
    *,
    step: dict[str, Any],
    spec_dir: Path,
    raw_inputs: dict[str, Any],
) -> Any:
    execution = step.get("execution") or {}
    script_ref = execution.get("script_path") or step.get("script")
    if not script_ref:
        raise RuntimeError(f'script step {step["id"]} missing execution.script_path/script')
    project_root_ref = execution.get("project_root")
    return run_transform_script(
        runtime,
        script_ref=script_ref,
        spec_dir=spec_dir,
        project_root_ref=project_root_ref,
        payload=raw_inputs,
        tmp_namespace="scripts",
    )


def execute_multi_output_script_step(
    runtime: RuntimeContext,
    *,
    step: dict[str, Any],
    spec_dir: Path,
    raw_inputs: dict[str, Any],
    output_path: Path,
) -> Any:
    """Execute a script step that writes multiple output files to output_path's parent directory.

    The script receives output_path as --output and uses its parent directory to write
    all output files. Returns the parsed content of output_path (the primary output).
    """
    execution = step.get("execution") or {}
    script_ref = execution.get("script_path") or step.get("script")
    if not script_ref:
        raise RuntimeError(f'multi-output script step {step["id"]} missing execution.script_path/script')
    project_root_ref = execution.get("project_root")
    project_root = resolve_step_project_root(runtime, spec_dir, project_root_ref)
    script_path = resolve_step_path(runtime, spec_dir, script_ref)
    tmp_dir = runtime.resolve_under_state_root(".workflow-driver-internal", "scripts")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    input_path = tmp_dir / ".executor-input.json"
    input_path.write_text(json.dumps(raw_inputs, ensure_ascii=False, indent=2), encoding="utf-8")
    stderr_log(
        f"[script] 执行多输出脚本 {script_path} "
        f"(input={runtime.to_output_path(input_path)}, "
        f"output_dir={runtime.to_output_path(output_path.parent)})"
    )
    command = runtime.build_script_command(
        script_path=script_path,
        input_path=input_path,
        output_path=output_path,
        project_root=project_root,
    )
    proc = subprocess.run(
        command,
        text=True,
        capture_output=True,
        cwd=project_root or runtime.workspace,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip())
    if not output_path.exists():
        raise RuntimeError(f"multi-output step primary output not produced: {output_path}")
    stderr_log(f"[script] 多输出脚本执行完成 {script_path}")
    return read_json(output_path)


def execute_model_step(
    runtime: RuntimeContext,
    *,
    step: dict[str, Any],
    spec_dir: Path,
    raw_inputs: dict[str, Any],
    input_refs: list[dict[str, Any]] | None,
    run_id: str,
    skill: str,
    timeout_seconds: int = 600,
    data_dir: Path | None = None,
) -> Any:
    execution = step.get("execution") or {}
    model_cfg = execution.get("model") or {}
    agent = model_cfg.get("agent") or step.get("actor")
    if not agent:
        raise RuntimeError(f'model step {step["id"]} missing execution.model.agent')

    prompt_text = step.get("prompt_text") or ""
    refs = list(input_refs or [])
    if not refs:
        refs = [{"name": key, "kind": "raw", "description": ""} for key in raw_inputs.keys()]
    refs = materialize_input_refs(
        runtime,
        step=step,
        raw_inputs=raw_inputs,
        input_refs=refs,
        data_dir=data_dir,
    )
    input_refs_text = build_input_refs_text(runtime, refs)

    stderr_log(
        f'[model] 准备调用模型 {agent} 处理第{step["number"]}步 {step["name"]} '
        f"(input_refs={len(refs)})"
    )
    materialized_refs = [ref for ref in refs if ref.get("materialized")]
    if materialized_refs:
        materialized_summary = ", ".join(
            f'{ref.get("name")}={runtime.to_output_path(Path(str(ref.get("path"))))}'
            for ref in materialized_refs
            if ref.get("name") and ref.get("path")
        )
        stderr_log(
            f'[model] 第{step["number"]}步 {step["name"]} '
            f'materialized {len(materialized_refs)} 个输入文件: {materialized_summary}'
        )
    message_template = model_cfg.get("message_template") or (
        "你在执行 workflow 的第{step_number}步：{step_name}（skill={skill}）。\n\n"
        "任务要求：\n{prompt_text}\n\n"
        "输出规则：只输出严格 JSON，不要解释；所有判断必须严格基于下列输入文件中的数据，不得读取或引用其他来源。\n\n"
        "依赖输入文件：\n{input_refs_text}\n\n"
        "请逐个读取上述输入文件获取所需数据；若某项只是源文件中的一部分，driver 已将该部分单独物化为输入文件。\n\n"
    )
    base_message = message_template.format(
        step_number=step["number"],
        step_name=step["name"],
        step_id=step["id"],
        input_refs_text=input_refs_text or "- 无输入文件",
        prompt_text=prompt_text,
        skill=skill,
    )
    session_label = f'{run_id}-{step["id"]}'
    project_root_ref = execution.get("project_root")
    project_root = resolve_step_project_root(runtime, spec_dir, project_root_ref)

    last_error: Exception | None = None
    for attempt in range(1, MODEL_SCHEMA_RETRY_LIMIT + 2):
        message = base_message
        if attempt > 1:
            retry_reason = str(last_error) if last_error else "输出格式不符合要求"
            stderr_log(
                f'[model] 第{step["number"]}步 {step["name"]} '
                f'发起第 {attempt} 次重试，上次失败原因: {retry_reason}'
            )
            message += (
                f"\n\n上一次输出未通过 schema/normalizer 校验（失败原因：{retry_reason}）。"
                "请严格只输出合法 JSON，且必须满足本步骤既定字段结构；"
                "不要输出解释，不要省略必填字段，不要把 object 写成 string。"
            )
        else:
            stderr_log(f'[model] 第{step["number"]}步 {step["name"]} 发起模型请求，第 {attempt} 次尝试')
        if runtime.debug:
            stderr_log(
                f'[debug][model] 第{step["number"]}步 {step["name"]} 第 {attempt} 次尝试完整请求参数:\n'
                f"  agent={agent}\n"
                f"  session_label={session_label}-try{attempt}\n"
                f"  workspace={runtime.workspace}\n"
                f"  timeout_seconds={timeout_seconds}\n"
                f"  message=\n{message}"
            )
        obj = run_session_model_step(
            runtime,
            agent,
            message,
            workspace=str(runtime.workspace),
            timeout_seconds=timeout_seconds,
            session_label=f"{session_label}-try{attempt}",
        )
        if runtime.debug:
            stderr_log(
                f'[debug][model] 第{step["number"]}步 {step["name"]} 第 {attempt} 次尝试完整返回:\n'
                f"{json.dumps(obj, ensure_ascii=False, indent=2)}"
            )
        final_obj = obj
        try:
            normalizer_ref = execution.get("normalizer") or step.get("normalizer")
            if normalizer_ref:
                stderr_log(f'[model] 第{step["number"]}步 {step["name"]} 准备执行 normalizer {normalizer_ref}')
                final_obj = run_transform_script(
                    runtime,
                    script_ref=normalizer_ref,
                    spec_dir=spec_dir,
                    project_root_ref=str(project_root),
                    payload=obj,
                    tmp_namespace="normalizers",
                )
            stderr_log(f'[model] 第{step["number"]}步 {step["name"]} 模型执行完成')
            return final_obj
        except Exception as exc:
            last_error = exc
            stderr_log(
                f'[model] 第{step["number"]}步 {step["name"]} '
                f'第 {attempt} 次尝试 normalizer/schema 校验失败: {exc}'
            )
            if attempt > MODEL_SCHEMA_RETRY_LIMIT:
                raise RuntimeError(
                    f"model output schema validation failed after {attempt} attempts: {exc}"
                ) from exc
    raise RuntimeError(f"model output schema validation failed: {last_error}")


def execute_final_step(
    *,
    step: dict[str, Any],
    resolved_inputs: dict[str, Any],
) -> dict[str, Any]:
    data_refs = step.get("data_refs") or list(resolved_inputs.keys())
    data_map: dict[str, Any] = {}
    sections: list[str] = []
    for name in data_refs:
        if name not in resolved_inputs:
            continue
        data_value = resolved_inputs[name]
        data_map[name] = data_value
        sections.append(f"{name} JSON：\n{json.dumps(data_value, ensure_ascii=False, indent=2)}")
    prompt_template = step.get("prompt_template") or step.get("prompt_text") or ""
    final_prompt = prompt_template.format(data_names="、".join(data_map.keys()))
    if sections:
        final_prompt = f"{final_prompt}\n\n下面是数据：\n\n" + "\n\n".join(sections)
    return {
        "final_prompt": final_prompt,
        "data_refs": data_refs,
        "data_map": data_map,
    }
