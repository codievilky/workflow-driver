from __future__ import annotations

import json
import re
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


def safe_filename_part(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text)
    return text.strip("._") or "item"


def join_api_url(base_url: str, suffix: str) -> str:
    clean_base = base_url.rstrip("/")
    clean_suffix = suffix.strip("/")
    if clean_base.endswith(f"/{clean_suffix}"):
        return clean_base
    return f"{clean_base}/{clean_suffix}"


def post_json(
    runtime: RuntimeContext,
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: int,
    debug_label: str,
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if runtime.debug:
        stderr_log(
            f"[debug][{debug_label}] url={url}\n"
            f"[debug][{debug_label}] request_body=\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
        )
    req = request.Request(
        url,
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8")
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{debug_label} HTTP {exc.code}: {raw}") from exc
    except Exception as exc:
        raise RuntimeError(f"{debug_label} request failed: {exc}") from exc
    if runtime.debug:
        stderr_log(f"[debug][{debug_label}] raw_response=\n{raw}")
    try:
        obj = json.loads(raw)
    except Exception as exc:
        raise RuntimeError(f"invalid {debug_label} JSON: {raw[:1000]}") from exc
    return obj


def extract_text_from_content(content: Any) -> str:
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
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
        return "\n".join(part for part in parts if part)
    return ""


def extract_model_response_text(provider: str, response_obj: dict[str, Any]) -> str:
    if provider == "openai":
        choices = response_obj.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict):
                    text = extract_text_from_content(message.get("content"))
                    if text:
                        return text
                text = extract_text_from_content(first.get("text"))
                if text:
                    return text
        output_text = response_obj.get("output_text")
        if isinstance(output_text, str):
            return output_text
    if provider == "anthropic":
        text = extract_text_from_content(response_obj.get("content"))
        if text:
            return text
    raise RuntimeError(f"model response text not found: {json.dumps(response_obj, ensure_ascii=False)[:1000]}")


def invoke_openai_model(
    runtime: RuntimeContext,
    *,
    settings: dict[str, Any],
    messages: list[dict[str, str]],
    timeout_seconds: int,
) -> str:
    payload: dict[str, Any] = {
        "model": settings["model"],
        "messages": messages,
    }
    max_tokens_param = str(settings.get("max_tokens_param") or "max_tokens")
    payload[max_tokens_param] = settings["max_tokens"]
    if settings.get("temperature") is not None:
        payload["temperature"] = settings["temperature"]
    response_format = settings.get("response_format")
    if response_format:
        if isinstance(response_format, dict):
            payload["response_format"] = response_format
        elif str(response_format).strip() == "json_object":
            payload["response_format"] = {"type": "json_object"}

    headers = {
        "Authorization": f"Bearer {settings['api_key']}",
        "Content-Type": "application/json",
    }
    headers.update({str(key): str(value) for key, value in (settings.get("headers") or {}).items()})
    response_obj = post_json(
        runtime,
        url=join_api_url(settings["base_url"], "chat/completions"),
        headers=headers,
        payload=payload,
        timeout_seconds=timeout_seconds,
        debug_label="openai",
    )
    return extract_model_response_text("openai", response_obj)


def invoke_anthropic_model(
    runtime: RuntimeContext,
    *,
    settings: dict[str, Any],
    system_text: str,
    user_text: str,
    timeout_seconds: int,
) -> str:
    payload: dict[str, Any] = {
        "model": settings["model"],
        "max_tokens": settings["max_tokens"],
        "system": system_text,
        "messages": [
            {
                "role": "user",
                "content": user_text,
            },
        ],
    }
    if settings.get("temperature") is not None:
        payload["temperature"] = settings["temperature"]

    headers = {
        "x-api-key": settings["api_key"],
        "anthropic-version": str(settings.get("anthropic_version") or "2023-06-01"),
        "Content-Type": "application/json",
    }
    headers.update({str(key): str(value) for key, value in (settings.get("headers") or {}).items()})
    response_obj = post_json(
        runtime,
        url=join_api_url(settings["base_url"], "messages"),
        headers=headers,
        payload=payload,
        timeout_seconds=timeout_seconds,
        debug_label="anthropic",
    )
    return extract_model_response_text("anthropic", response_obj)


def run_api_model_step(
    runtime: RuntimeContext,
    *,
    settings: dict[str, Any],
    system_text: str,
    user_text: str,
    timeout_seconds: int,
) -> Any:
    provider = settings["provider"]
    if provider == "openai":
        text = invoke_openai_model(
            runtime,
            settings=settings,
            messages=[
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_text},
            ],
            timeout_seconds=timeout_seconds,
        )
    elif provider == "anthropic":
        text = invoke_anthropic_model(
            runtime,
            settings=settings,
            system_text=system_text,
            user_text=user_text,
            timeout_seconds=timeout_seconds,
        )
    else:
        raise RuntimeError(f"unsupported model api provider: {provider}")
    return extract_json_object(text)


def run_api_text_model_step(
    runtime: RuntimeContext,
    *,
    settings: dict[str, Any],
    system_text: str,
    user_text: str,
    timeout_seconds: int,
) -> str:
    provider = settings["provider"]
    if provider == "openai":
        return invoke_openai_model(
            runtime,
            settings=settings,
            messages=[
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_text},
            ],
            timeout_seconds=timeout_seconds,
        )
    if provider == "anthropic":
        return invoke_anthropic_model(
            runtime,
            settings=settings,
            system_text=system_text,
            user_text=user_text,
            timeout_seconds=timeout_seconds,
        )
    raise RuntimeError(f"unsupported model api provider: {provider}")


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


def normalize_reference_registry(raw_sources: Any) -> dict[str, dict[str, Any]]:
    if not raw_sources:
        return {}
    if isinstance(raw_sources, dict):
        registry: dict[str, dict[str, Any]] = {}
        for key, value in raw_sources.items():
            if isinstance(value, dict):
                item = dict(value)
            else:
                item = {"path": value}
            item.setdefault("name", str(key))
            registry[str(key)] = item
        return registry
    if isinstance(raw_sources, list):
        registry = {}
        for item in raw_sources:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("id") or item.get("key") or "").strip()
                if name:
                    registry[name] = dict(item)
        return registry
    return {}


def expand_reference_source(item: Any, registry: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if isinstance(item, str):
        key = item.strip()
        if not key:
            return None
        if key in registry:
            return dict(registry[key])
        return {"name": Path(key).name, "path": key}
    if not isinstance(item, dict):
        return None
    source = dict(item)
    ref_key = str(source.get("ref") or source.get("key") or "").strip()
    if ref_key and ref_key in registry:
        merged = dict(registry[ref_key])
        merged.update(source)
        merged.pop("ref", None)
        merged.pop("key", None)
        return merged
    return source


def collect_reference_sources(
    *,
    workflow_reference_sources: Any,
    default_reference_sources: list[Any] | None,
    step: dict[str, Any],
) -> list[dict[str, Any]]:
    registry = normalize_reference_registry(workflow_reference_sources)
    execution = step.get("execution") or {}
    raw_step_refs = (
        execution.get("reference_sources")
        or execution.get("references")
        or step.get("reference_sources")
        or step.get("references")
        or []
    )
    if isinstance(raw_step_refs, (str, dict)):
        raw_step_refs = [raw_step_refs]

    raw_refs: list[Any] = []
    for raw in default_reference_sources or []:
        raw_refs.append(raw)
    for raw in raw_step_refs or []:
        raw_refs.append(raw)

    seen: set[str] = set()
    refs: list[dict[str, Any]] = []
    for raw in raw_refs:
        ref = expand_reference_source(raw, registry)
        if not ref:
            continue
        identity = str(ref.get("path") or ref.get("content") or ref.get("name") or "")
        if identity and identity in seen:
            continue
        if identity:
            seen.add(identity)
        refs.append(ref)
    return refs


def load_reference_text(
    runtime: RuntimeContext,
    *,
    spec_dir: Path,
    source: dict[str, Any],
) -> dict[str, Any]:
    name = str(source.get("title") or source.get("name") or source.get("path") or "reference").strip()
    description = str(source.get("description") or "").strip()
    kind = str(source.get("kind") or "instruction").strip()
    path_text = str(source.get("path") or "").strip()
    content = source.get("content")
    resolved_path: Path | None = None
    if isinstance(content, str):
        text = content
    elif path_text:
        resolved_path = resolve_step_path(runtime, spec_dir, path_text)
        text = resolved_path.read_text(encoding=str(source.get("encoding") or "utf-8"))
    else:
        return {}

    max_chars = source.get("max_chars")
    truncated = False
    if max_chars not in (None, ""):
        limit = int(max_chars)
        if len(text) > limit:
            text = text[:limit]
            truncated = True

    return {
        "name": name,
        "description": description,
        "kind": kind,
        "path": str(resolved_path) if resolved_path else path_text,
        "content": text,
        "truncated": truncated,
    }


def build_reference_text(
    runtime: RuntimeContext,
    *,
    spec_dir: Path,
    references: list[dict[str, Any]],
) -> str:
    if not references:
        return ""
    sections: list[str] = []
    for idx, source in enumerate(references, start=1):
        loaded = load_reference_text(runtime, spec_dir=spec_dir, source=source)
        if not loaded:
            continue
        header = f"### 参考 {idx}：{loaded['name']}"
        meta_parts = []
        if loaded.get("kind"):
            meta_parts.append(f"类型={loaded['kind']}")
        if loaded.get("description"):
            meta_parts.append(f"用途={loaded['description']}")
        if loaded.get("path"):
            meta_parts.append(f"来源={runtime.to_output_path(Path(str(loaded['path'])))}")
        if loaded.get("truncated"):
            meta_parts.append("内容已按 max_chars 截断")
        meta = f"\n{'；'.join(meta_parts)}" if meta_parts else ""
        sections.append(f"{header}{meta}\n\n{loaded['content']}")
    return "\n\n".join(sections)


def format_model_data_value(value: Any) -> str:
    if isinstance(value, str):
        stripped = value.strip()
        try:
            parsed = json.loads(stripped)
        except Exception:
            return value
        return json.dumps(parsed, ensure_ascii=False, indent=2)
    return json.dumps(value, ensure_ascii=False, indent=2)


def build_model_input_data_text(
    runtime: RuntimeContext,
    *,
    refs: list[dict[str, Any]],
    model_inputs: Any,
) -> str:
    if not isinstance(model_inputs, dict):
        return format_model_data_value(model_inputs)
    if not refs:
        refs = [{"name": key, "kind": "raw", "description": ""} for key in model_inputs.keys()]
    sections: list[str] = []
    for idx, ref in enumerate(refs, start=1):
        name = str(ref.get("name") or "").strip()
        if not name:
            continue
        description = str(ref.get("description") or "").strip()
        path_text = str(ref.get("path") or "").strip()
        value = model_inputs.get(name)
        header = f"### 输入 {idx}：{name}"
        meta_parts: list[str] = []
        if description:
            meta_parts.append(f"含义={description}")
        if path_text:
            meta_parts.append(f"调试文件={runtime.to_output_path(Path(path_text))}")
        meta = f"\n{'；'.join(meta_parts)}" if meta_parts else ""
        sections.append(f"{header}{meta}\n\n```json\n{format_model_data_value(value)}\n```")
    return "\n\n".join(sections)


def write_model_prompt_audit_files(
    runtime: RuntimeContext,
    *,
    step: dict[str, Any],
    attempt: int,
    data_dir: Path | None,
    api_settings: dict[str, Any],
    timeout_seconds: int,
    system_text: str,
    user_text: str,
    input_refs: list[dict[str, Any]],
    reference_sources: list[dict[str, Any]],
) -> dict[str, Path]:
    prompt_root = data_dir or runtime.resolve_under_state_root(".workflow-driver-internal", "model-prompts")
    prompt_dir = prompt_root / "model-prompts" if data_dir else prompt_root
    prompt_dir.mkdir(parents=True, exist_ok=True)

    prefix = (
        f"step{safe_filename_part(step.get('number'))}_"
        f"{safe_filename_part(step.get('id'))}_"
        f"try{attempt}"
    )
    prompt_path = prompt_dir / f"{prefix}.prompt.md"
    request_path = prompt_dir / f"{prefix}.request.json"

    safe_api_settings = {
        "provider": api_settings.get("provider"),
        "model": api_settings.get("model"),
        "base_url": api_settings.get("base_url"),
        "max_tokens": api_settings.get("max_tokens"),
        "max_tokens_param": api_settings.get("max_tokens_param"),
        "temperature": api_settings.get("temperature"),
        "timeout_seconds": timeout_seconds,
    }
    request_payload = {
        "step": {
            "id": step.get("id"),
            "number": step.get("number"),
            "name": step.get("name"),
            "kind": step.get("kind"),
        },
        "attempt": attempt,
        "model_api": safe_api_settings,
        "input_refs": input_refs,
        "reference_sources": reference_sources,
        "messages": [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ],
    }
    request_path.write_text(json.dumps(request_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    prompt_text = (
        f"# Model Prompt Audit\n\n"
        f"- step: {step.get('number')} {step.get('name')} ({step.get('id')})\n"
        f"- attempt: {attempt}\n"
        f"- provider: {api_settings.get('provider')}\n"
        f"- model: {api_settings.get('model')}\n"
        f"- base_url: {api_settings.get('base_url')}\n"
        f"- request_json: {runtime.to_output_path(request_path)}\n\n"
        f"## System Prompt\n\n"
        f"----- SYSTEM PROMPT START -----\n"
        f"{system_text}\n"
        f"----- SYSTEM PROMPT END -----\n\n"
        f"## User Prompt\n\n"
        f"----- USER PROMPT START -----\n"
        f"{user_text}\n"
        f"----- USER PROMPT END -----\n"
    )
    prompt_path.write_text(prompt_text, encoding="utf-8")
    return {"prompt_path": prompt_path, "request_path": request_path}


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
    workflow_reference_sources: Any = None,
    default_reference_sources: list[Any] | None = None,
    run_id: str,
    skill: str,
    timeout_seconds: int = 600,
    data_dir: Path | None = None,
) -> Any:
    execution = step.get("execution") or {}
    model_cfg = execution.get("model") or {}
    agent = model_cfg.get("agent") or step.get("actor") or "model"
    api_settings = runtime.load_model_api_settings(model_cfg)
    api_timeout_seconds = int(api_settings.get("timeout_seconds") or timeout_seconds)

    prompt_text = step.get("prompt_text") or ""
    model_inputs = build_model_input(
        runtime,
        step=step,
        spec_dir=spec_dir,
        raw_inputs=raw_inputs,
    )
    refs = list(input_refs or [])
    if not isinstance(model_inputs, dict):
        model_inputs = {"input": model_inputs}
        refs = [{"name": "input", "kind": "prepared", "description": "模型输入"}]
    elif not refs or set(ref.get("name") for ref in refs if ref.get("name")) - set(model_inputs.keys()):
        refs = [{"name": key, "kind": "prepared", "description": ""} for key in model_inputs.keys()]
    refs = materialize_input_refs(
        runtime,
        step=step,
        raw_inputs=model_inputs,
        input_refs=refs,
        data_dir=data_dir,
    )
    input_refs_text = build_input_refs_text(runtime, refs)
    input_data_text = build_model_input_data_text(runtime, refs=refs, model_inputs=model_inputs)
    reference_sources = collect_reference_sources(
        workflow_reference_sources=workflow_reference_sources,
        default_reference_sources=default_reference_sources,
        step=step,
    )
    reference_text = build_reference_text(
        runtime,
        spec_dir=spec_dir,
        references=reference_sources,
    )

    stderr_log(
        f'[model] 准备调用 {api_settings["provider"]}/{api_settings["model"]} '
        f'处理第{step["number"]}步 {step["name"]} '
        f"(input_refs={len(refs)}, references={len(reference_sources)})"
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
    system_parts = [
        (
            "你是 workflow-driver 直接通过 API 调用的模型执行器。"
            "请用中文完成当前股票复盘步骤；不要调用工具，不要读取外部文件，"
            "不要引入本次输入和参考材料之外的新事实。"
        ),
        f"当前步骤原 actor/角色标识：{agent}。",
    ]
    if reference_text:
        system_parts.append(
            "以下内容属于行事方式、判断方法和风险约束，优先用于约束分析口径；"
            "它们不是行情数据，不要在最终 JSON 中机械复述文档名或执行链路。\n\n"
            f"{reference_text}"
        )
    system_text = "\n\n".join(system_parts)
    message_template = model_cfg.get("message_template") or (
        "你在执行 workflow 的第{step_number}步：{step_name}（skill={skill}）。\n\n"
        "任务要求：\n{prompt_text}\n\n"
        "输出规则：只输出严格 JSON，不要解释；所有判断必须严格基于下列已内联输入数据和系统参考材料，不得读取或引用其他来源。\n\n"
        "依赖输入清单：\n{input_refs_text}\n\n"
        "依赖输入数据：\n{input_data_text}\n\n"
        "请基于上面的数据完成任务，最后只返回一个可被 JSON.parse 解析的 JSON 对象。\n\n"
    )
    base_message = message_template.format(
        step_number=step["number"],
        step_name=step["name"],
        step_id=step["id"],
        input_refs_text=input_refs_text or "- 无输入文件",
        input_data_text=input_data_text or "- 无输入数据",
        reference_text=reference_text or "- 无参考文档",
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
        audit_paths = write_model_prompt_audit_files(
            runtime,
            step=step,
            attempt=attempt,
            data_dir=data_dir,
            api_settings=api_settings,
            timeout_seconds=api_timeout_seconds,
            system_text=system_text,
            user_text=message,
            input_refs=refs,
            reference_sources=reference_sources,
        )
        stderr_log(
            f'[model] 第{step["number"]}步 {step["name"]} 已记录请求 prompt: '
            f'{runtime.to_output_path(audit_paths["prompt_path"])}'
        )
        if runtime.debug:
            stderr_log(
                f'[debug][model] 第{step["number"]}步 {step["name"]} 第 {attempt} 次尝试完整请求参数:\n'
                f"  provider={api_settings['provider']}\n"
                f"  model={api_settings['model']}\n"
                f"  request_label={session_label}-try{attempt}\n"
                f"  workspace={runtime.workspace}\n"
                f"  timeout_seconds={api_timeout_seconds}\n"
                f"  system=\n{system_text}\n"
                f"  message=\n{message}"
            )
        try:
            obj = run_api_model_step(
                runtime,
                settings=api_settings,
                system_text=system_text,
                user_text=message,
                timeout_seconds=api_timeout_seconds,
            )
        except Exception as exc:
            last_error = exc
            stderr_log(
                f'[model] 第{step["number"]}步 {step["name"]} '
                f'第 {attempt} 次尝试模型请求/JSON 解析失败: {exc}'
            )
            if attempt > MODEL_SCHEMA_RETRY_LIMIT:
                raise RuntimeError(
                    f"model request or JSON parsing failed after {attempt} attempts: {exc}"
                ) from exc
            continue
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


def build_final_prompt_package(
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


def should_render_final_with_model(step: dict[str, Any]) -> bool:
    execution = step.get("execution") or {}
    mode = str(execution.get("mode") or "").strip().lower()
    output_mode = str(execution.get("output_mode") or step.get("output_mode") or "").strip().lower()
    return mode == "model" or output_mode in {"model", "model_text", "text_model", "final_text"}


def execute_final_step(
    runtime: RuntimeContext,
    *,
    step: dict[str, Any],
    spec_dir: Path,
    resolved_inputs: dict[str, Any],
    workflow_reference_sources: Any = None,
    default_reference_sources: list[Any] | None = None,
    run_id: str,
    skill: str,
    timeout_seconds: int = 600,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    prompt_package = build_final_prompt_package(step=step, resolved_inputs=resolved_inputs)
    if not should_render_final_with_model(step):
        return prompt_package

    execution = step.get("execution") or {}
    model_cfg = execution.get("model") or {}
    agent = model_cfg.get("agent") or step.get("actor") or "model"
    api_settings = runtime.load_model_api_settings(model_cfg)
    api_timeout_seconds = int(api_settings.get("timeout_seconds") or timeout_seconds)

    reference_sources = collect_reference_sources(
        workflow_reference_sources=workflow_reference_sources,
        default_reference_sources=default_reference_sources,
        step=step,
    )
    reference_text = build_reference_text(
        runtime,
        spec_dir=spec_dir,
        references=reference_sources,
    )
    system_parts = [
        (
            "你是 workflow-driver 直接通过 API 调用的最终回复生成模型。"
            "请用中文输出最终用户可直接阅读的股票复盘正文；不要调用工具，"
            "不要读取外部文件，不要引入本次输入和参考材料之外的新事实。"
        ),
        "只输出最终正文，不要输出 JSON，不要输出 Markdown 代码块，不要解释执行过程。",
        f"当前步骤原 actor/角色标识：{agent}。",
    ]
    if reference_text:
        system_parts.append(
            "以下内容属于行事方式、判断方法和风险约束，优先用于约束最终表达口径；"
            "它们不是行情数据，不要在最终正文中机械复述文档名或执行链路。\n\n"
            f"{reference_text}"
        )
    system_text = "\n\n".join(system_parts)

    message_template = model_cfg.get("message_template") or "{final_prompt}"
    user_text = message_template.format(
        final_prompt=prompt_package["final_prompt"],
        data_refs="、".join(prompt_package.get("data_refs") or []),
        skill=skill,
        step_number=step["number"],
        step_name=step["name"],
        step_id=step["id"],
        run_id=run_id,
    )

    stderr_log(
        f'[final] 准备调用 {api_settings["provider"]}/{api_settings["model"]} '
        f'生成第{step["number"]}步 {step["name"]} 最终正文'
    )
    last_error: Exception | None = None
    for attempt in range(1, MODEL_SCHEMA_RETRY_LIMIT + 2):
        if attempt > 1:
            retry_reason = str(last_error) if last_error else "模型请求失败"
            stderr_log(
                f'[final] 第{step["number"]}步 {step["name"]} '
                f'发起第 {attempt} 次重试，上次失败原因: {retry_reason}'
            )
        else:
            stderr_log(f'[final] 第{step["number"]}步 {step["name"]} 发起最终正文模型请求')

        audit_paths = write_model_prompt_audit_files(
            runtime,
            step=step,
            attempt=attempt,
            data_dir=data_dir,
            api_settings=api_settings,
            timeout_seconds=api_timeout_seconds,
            system_text=system_text,
            user_text=user_text,
            input_refs=[
                {
                    "name": name,
                    "kind": "final_data",
                    "description": f"最终渲染数据：{name}",
                }
                for name in prompt_package.get("data_refs", [])
            ],
            reference_sources=reference_sources,
        )
        stderr_log(
            f'[final] 第{step["number"]}步 {step["name"]} 已记录最终请求 prompt: '
            f'{runtime.to_output_path(audit_paths["prompt_path"])}'
        )
        try:
            final_text = run_api_text_model_step(
                runtime,
                settings=api_settings,
                system_text=system_text,
                user_text=user_text,
                timeout_seconds=api_timeout_seconds,
            ).strip()
        except Exception as exc:
            last_error = exc
            stderr_log(
                f'[final] 第{step["number"]}步 {step["name"]} '
                f'第 {attempt} 次最终正文模型请求失败: {exc}'
            )
            if attempt > MODEL_SCHEMA_RETRY_LIMIT:
                raise RuntimeError(f"final text model request failed after {attempt} attempts: {exc}") from exc
            continue
        if not final_text:
            last_error = RuntimeError("empty final text")
            if attempt > MODEL_SCHEMA_RETRY_LIMIT:
                raise RuntimeError("final text model returned empty output")
            continue
        stderr_log(f'[final] 第{step["number"]}步 {step["name"]} 最终正文生成完成')
        return {
            **prompt_package,
            "final_text": final_text,
            "final_message": final_text,
            "model_api": {
                "provider": api_settings.get("provider"),
                "model": api_settings.get("model"),
                "base_url": api_settings.get("base_url"),
            },
        }
    raise RuntimeError(f"final text model request failed: {last_error}")
