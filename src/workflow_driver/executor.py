from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib import error, request

from .config import RuntimeContext
from .utils import read_json, write_json

POLL_INTERVAL_SECONDS = 3.0
MAX_HISTORY_MESSAGES = 50
REPLY_SKIP_SENTINEL = "REPLY_SKIP"
ANNOUNCE_SKIP_SENTINEL = "ANNOUNCE_SKIP"
MODEL_SCHEMA_RETRY_LIMIT = 2


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


def invoke_gateway_tool(runtime: RuntimeContext, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    url, token = runtime.load_gateway_settings()
    body = json.dumps({"tool": tool, "args": args}, ensure_ascii=False).encode("utf-8")
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


def resolve_payload_from_context(runtime: RuntimeContext, step_context: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in (step_context.get("resolved_inputs") or {}).items():
        if isinstance(value, dict) and "artifact_path" in value and "field" in value:
            obj = read_json(runtime.resolve_path(value["artifact_path"]))
            payload[key] = obj.get(value["field"])
        elif isinstance(value, dict) and "builder" in value and "builder_payload" in value:
            builder_payload: dict[str, Any] = {}
            for builder_key, builder_value in (value.get("builder_payload") or {}).items():
                if isinstance(builder_value, dict) and "artifact_path" in builder_value and "field" in builder_value:
                    obj = read_json(runtime.resolve_path(builder_value["artifact_path"]))
                    builder_payload[builder_key] = obj[builder_value["field"]]
                elif isinstance(builder_value, str) and builder_value.startswith("tmp/"):
                    builder_payload[builder_key] = read_json(runtime.resolve_path(builder_value))
                else:
                    builder_payload[builder_key] = builder_value
            builder_path = runtime.resolve_path(value["builder"])
            builder_project_root = runtime.workspace
            prepared = json.loads(
                run_python_step(
                    runtime,
                    script_path=builder_path,
                    payload=builder_payload,
                    project_root=builder_project_root,
                    tmp_namespace="builders",
                ),
            )
            output_path = runtime.resolve_path(value["prepared_input_path"])
            write_json(output_path, prepared)
            payload[key] = prepared.get(key, prepared)
        elif isinstance(value, str) and value.startswith("tmp/"):
            obj = read_json(runtime.resolve_path(value))
            if key == "deep_dive_request" and isinstance(obj, dict):
                payload.update(obj)
            else:
                payload[key] = obj
        else:
            payload[key] = value
    return payload


def execute_script_step(
    runtime: RuntimeContext,
    step: dict[str, Any],
    run_id: str | None = None,
    *,
    timeout_seconds: int = 600,
) -> str:
    del run_id, timeout_seconds
    ctx = step["step_context"]
    execution = step.get("execution") or {}
    script_ref = execution.get("script_path") or step.get("script")
    if not script_ref:
        raise RuntimeError(f'script step {step["id"]} missing execution.script_path/script')
    script_path = runtime.resolve_path(script_ref)
    project_root_ref = execution.get("project_root")
    project_root = runtime.resolve_path(project_root_ref) if project_root_ref else runtime.workspace
    payload = resolve_payload_from_context(runtime, ctx)
    payload_text = run_python_step(
        runtime,
        script_path=script_path,
        payload=payload,
        project_root=project_root,
        tmp_namespace="scripts",
    )
    result = extract_json_object(payload_text)
    out_path = runtime.resolve_path(ctx["default_artifact_path"])
    if not out_path.exists():
        write_json(out_path, result)
    if not out_path.exists():
        raise RuntimeError(f"missing script artifact after execution: {out_path}")
    return runtime.to_output_path(out_path)


def prepare_model_input(runtime: RuntimeContext, step: dict[str, Any]) -> str:
    ctx = step["step_context"]
    payload = resolve_payload_from_context(runtime, ctx)
    prepared_input_path = ctx.get("prepared_input_path")
    if prepared_input_path:
        path = runtime.resolve_path(prepared_input_path)
        if not path.exists():
            write_json(path, payload)
    else:
        path = runtime.resolve_path(f'{ctx["artifacts_dir"]}/step{step["number"]}_{step["id"]}_input.json')
        write_json(path, payload)
        prepared_input_path = runtime.to_output_path(path)
    return prepared_input_path


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
    timeout_seconds: int = 600,
    session_label: str | None = None,
) -> Any:
    spawn_result = invoke_gateway_tool(
        runtime,
        "sessions_spawn",
        {
            "task": session_label or "workflow-model-step",
            "agentId": agent,
            "cleanup": "keep",
        },
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


def execute_model_step(
    runtime: RuntimeContext,
    step: dict[str, Any],
    run_id: str,
    *,
    timeout_seconds: int = 600,
) -> str:
    ctx = step["step_context"]
    execution = step.get("execution") or {}
    model_cfg = execution.get("model") or {}
    agent = model_cfg.get("agent") or step.get("actor")
    if not agent:
        raise RuntimeError(f'model step {step["id"]} missing execution.model.agent')

    prepared_input_path = prepare_model_input(runtime, step)
    prompt_text = step.get("prompt_text") or ""
    message_template = model_cfg.get("message_template") or (
        "你在执行 workflow 的第{step_number}步：{step_name}。请严格只使用这个输入文件：\n"
        "- {prepared_input_abs_path}\n\n"
        "要求：\n"
        "1. 只输出严格 JSON，不要解释\n"
        "2. 不能读取或引用其他输入来源\n"
        "3. 如果无法完成，也要输出 JSON，并在字段内表达\n\n"
        "请按照以下 prompt 输出：\n\n{prompt_text}"
    )
    base_message = message_template.format(
        step_number=step["number"],
        step_name=step["name"],
        step_id=step["id"],
        prepared_input_path=prepared_input_path,
        prepared_input_abs_path=str(runtime.resolve_path(prepared_input_path)),
        prompt_text=prompt_text,
        run_id=run_id,
    )
    session_label = f'{run_id}-{step["id"]}'
    project_root_ref = execution.get("project_root")
    project_root = runtime.resolve_path(project_root_ref) if project_root_ref else runtime.workspace

    last_error: Exception | None = None
    for attempt in range(1, MODEL_SCHEMA_RETRY_LIMIT + 2):
        message = base_message
        if attempt > 1:
            message += (
                "\n\n上一次输出未通过 schema/normalizer 校验。"
                "请严格只输出合法 JSON，且必须满足本步骤既定字段结构；"
                "不要输出解释，不要省略必填字段，不要把 object 写成 string。"
            )
        obj = run_session_model_step(
            runtime,
            agent,
            message,
            timeout_seconds=timeout_seconds,
            session_label=f"{session_label}-try{attempt}",
        )
        final_obj = obj
        try:
            if step.get("normalizer"):
                normalizer_path = runtime.resolve_path(step["normalizer"])
                final_obj = json.loads(
                    run_python_step(
                        runtime,
                        script_path=normalizer_path,
                        payload=obj,
                        project_root=project_root,
                        tmp_namespace="normalizers",
                    ),
                )
            out_path = runtime.resolve_path(ctx["default_artifact_path"])
            write_json(out_path, final_obj)
            return runtime.to_output_path(out_path)
        except Exception as exc:
            last_error = exc
            if attempt > MODEL_SCHEMA_RETRY_LIMIT:
                raise RuntimeError(f"model output schema validation failed after retries: {exc}") from exc
    raise RuntimeError(f"model output schema validation failed: {last_error}")


def resolve_data_value(runtime: RuntimeContext, value: Any) -> Any:
    if isinstance(value, dict) and "artifact_path" in value and "field" in value:
        obj = read_json(runtime.resolve_path(value["artifact_path"]))
        return obj.get(value["field"])
    if isinstance(value, str) and value.startswith("tmp/"):
        return read_json(runtime.resolve_path(value))
    return value


def execute_final_step(runtime: RuntimeContext, step: dict[str, Any], run_id: str) -> str:
    del run_id
    ctx = step["step_context"]
    resolved_inputs = ctx.get("resolved_inputs") or {}
    data_refs = step.get("data_refs") or list(resolved_inputs.keys())
    sections: list[str] = []
    data_map: dict[str, Any] = {}
    for name in data_refs:
        if name not in resolved_inputs:
            continue
        data_value = resolve_data_value(runtime, resolved_inputs[name])
        data_map[name] = data_value
        sections.append(f"{name} JSON：\n{json.dumps(data_value, ensure_ascii=False, indent=2)}")
    prompt_template = step.get("prompt_template") or step.get("prompt_text") or ""
    final_prompt = prompt_template.format(data_names="、".join(data_map.keys()))
    if sections:
        final_prompt = f"{final_prompt}\n\n下面是数据：\n\n" + "\n\n".join(sections)
    out_obj = {
        "final_prompt": final_prompt,
        "data_refs": data_refs,
        "data_map": data_map,
    }
    out_path = runtime.resolve_path(ctx["default_artifact_path"])
    write_json(out_path, out_obj)
    prompt_path = out_path.with_suffix(".prompt.txt")
    prompt_path.write_text(final_prompt, encoding="utf-8")
    return runtime.to_output_path(out_path)
