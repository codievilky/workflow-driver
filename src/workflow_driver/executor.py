from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib import error, request

from .config import RuntimeContext
from .utils import stderr_log

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


def execute_model_step(
    runtime: RuntimeContext,
    *,
    step: dict[str, Any],
    spec_dir: Path,
    raw_inputs: dict[str, Any],
    run_id: str,
    skill: str,
    timeout_seconds: int = 600,
) -> Any:
    execution = step.get("execution") or {}
    model_cfg = execution.get("model") or {}
    agent = model_cfg.get("agent") or step.get("actor")
    if not agent:
        raise RuntimeError(f'model step {step["id"]} missing execution.model.agent')

    model_input = build_model_input(runtime, step=step, spec_dir=spec_dir, raw_inputs=raw_inputs)
    model_input_json = json.dumps(model_input, ensure_ascii=False, indent=2)
    stderr_log(
        f'[model] 准备调用模型 {agent} 处理第{step["number"]}步 {step["name"]} '
        f"(input_chars={len(model_input_json)})"
    )
    prompt_text = step.get("prompt_text") or ""
    message_template = model_cfg.get("message_template") or (
        "你在执行 workflow 的第{step_number}步：{step_name}（skill={skill}）。\n\n"
        "任务要求：\n{prompt_text}\n\n"
        "输出规则：只输出严格 JSON，不要解释；所有判断必须严格基于上方输入数据，不得读取或引用其他来源；\n\n"
        "输入数据如下：\n{prepared_input_json}\n\n"
    )
    base_message = message_template.format(
        step_number=step["number"],
        step_name=step["name"],
        step_id=step["id"],
        prepared_input_json=model_input_json,
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
            message += (
                "\n\n上一次输出未通过 schema/normalizer 校验。"
                "请严格只输出合法 JSON，且必须满足本步骤既定字段结构；"
                "不要输出解释，不要省略必填字段，不要把 object 写成 string。"
            )
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
            if attempt > MODEL_SCHEMA_RETRY_LIMIT:
                raise RuntimeError(f"model output schema validation failed after retries: {exc}") from exc
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
