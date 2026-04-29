from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_GATEWAY_CONFIG = Path.home() / ".workflow-driver" / "config.json"
DEFAULT_NOTIFICATION_URL = "http://192.168.50.128:8111/message/send_info"


@dataclass(frozen=True)
class RuntimeContext:
    workspace: Path
    state_root: Path
    gateway_url: str | None
    gateway_token: str | None
    gateway_config: Path
    script_command_template: str | None
    debug: bool = False

    @classmethod
    def from_options(
        cls,
        *,
        workspace: str | None = None,
        state_root: str | None = None,
        gateway_url: str | None = None,
        gateway_token: str | None = None,
        gateway_config: str | None = None,
        script_command_template: str | None = None,
        debug: bool = False,
    ) -> "RuntimeContext":
        workspace_value = workspace or os.environ.get("WORKFLOW_DRIVER_WORKSPACE") or "."
        workspace_path = Path(workspace_value).expanduser().resolve()

        state_root_value = state_root or os.environ.get("WORKFLOW_DRIVER_STATE_ROOT")
        if state_root_value:
            candidate = Path(state_root_value).expanduser()
            state_root_path = candidate if candidate.is_absolute() else (workspace_path / candidate)
        else:
            state_root_path = workspace_path / "tmp"

        gateway_config_value = gateway_config or os.environ.get("WORKFLOW_DRIVER_GATEWAY_CONFIG")
        gateway_config_path = Path(gateway_config_value).expanduser() if gateway_config_value else DEFAULT_GATEWAY_CONFIG

        debug_env = os.environ.get("WORKFLOW_DRIVER_DEBUG", "").strip().lower()
        debug_value = debug or debug_env in ("1", "true", "yes")

        return cls(
            workspace=workspace_path,
            state_root=state_root_path.resolve(),
            gateway_url=gateway_url or os.environ.get("WORKFLOW_DRIVER_GATEWAY_URL"),
            gateway_token=gateway_token or os.environ.get("WORKFLOW_DRIVER_GATEWAY_TOKEN"),
            gateway_config=gateway_config_path,
            script_command_template=script_command_template or os.environ.get("WORKFLOW_DRIVER_SCRIPT_COMMAND"),
            debug=debug_value,
        )

    def resolve_path(self, path_ref: str | Path, *, base_dir: str | Path | None = None) -> Path:
        path = Path(path_ref).expanduser()
        if path.is_absolute():
            return path.resolve()
        if base_dir is not None:
            base_path = Path(base_dir).expanduser()
            if not base_path.is_absolute():
                base_path = (self.workspace / base_path).resolve()
            return (base_path / path).resolve()
        return (self.workspace / path).resolve()

    def resolve_under_state_root(self, *parts: str) -> Path:
        return self.state_root.joinpath(*parts)

    def to_output_path(self, path: Path) -> str:
        return str(path.resolve())

    def load_config(self) -> dict[str, Any]:
        if not self.gateway_config.exists():
            return {}
        data = json.loads(self.gateway_config.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return data

    def load_gateway_settings(self) -> tuple[str, str]:
        if self.gateway_url and self.gateway_token:
            return self.gateway_url, self.gateway_token

        if not self.gateway_config.exists():
            raise RuntimeError(
                "gateway settings not configured; provide --gateway-url/--gateway-token "
                "or create ~/.workflow-driver/config.json",
            )

        cfg = self.load_config()
        gateway = cfg.get("gateway") or {}
        auth = gateway.get("auth") or {}
        configured_url = gateway.get("url")
        port = gateway.get("port") or 18789
        token = self.gateway_token or auth.get("token")
        if not token:
            raise RuntimeError("gateway auth token not configured")
        url = self.gateway_url or configured_url or f"http://localhost:{port}/tools/invoke"
        return url, token

    @staticmethod
    def _pick_first(*values: Any) -> Any:
        for value in values:
            if value not in (None, ""):
                return value
        return None

    @staticmethod
    def _as_bool(value: Any, *, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        return default

    def load_model_api_settings(self, step_model_cfg: dict[str, Any] | None = None) -> dict[str, Any]:
        cfg = self.load_config()
        file_cfg = cfg.get("model_api") or cfg.get("api") or {}
        if not file_cfg and isinstance(cfg.get("model"), dict):
            candidate = cfg["model"]
            if any(key in candidate for key in ("api_type", "type", "provider", "base_url", "baseurl", "api_key")):
                file_cfg = candidate
        if not isinstance(file_cfg, dict):
            file_cfg = {}

        step_cfg = step_model_cfg or {}
        provider = self._pick_first(
            step_cfg.get("api_type"),
            step_cfg.get("type"),
            step_cfg.get("provider"),
            os.environ.get("WORKFLOW_DRIVER_MODEL_API_TYPE"),
            file_cfg.get("api_type"),
            file_cfg.get("type"),
            file_cfg.get("provider"),
        )
        if not provider:
            raise RuntimeError(
                "model api type not configured; set model_api.api_type to openai or anthropic "
                f"in {self.gateway_config}",
            )
        provider = str(provider).strip().lower()
        if provider in ("openai-compatible", "openai_compatible", "chat-completions"):
            provider = "openai"
        if provider in ("claude", "anthropic-messages"):
            provider = "anthropic"
        if provider not in ("openai", "anthropic"):
            raise RuntimeError(f"unsupported model api type: {provider}")

        base_url = self._pick_first(
            step_cfg.get("base_url"),
            step_cfg.get("baseurl"),
            os.environ.get("WORKFLOW_DRIVER_MODEL_BASE_URL"),
            file_cfg.get("base_url"),
            file_cfg.get("baseurl"),
        )
        api_key = self._pick_first(
            step_cfg.get("api_key"),
            os.environ.get("WORKFLOW_DRIVER_MODEL_API_KEY"),
            file_cfg.get("api_key"),
        )
        model_name = self._pick_first(
            step_cfg.get("model_name"),
            step_cfg.get("model_id"),
            step_cfg.get("name"),
            step_cfg.get("model"),
            os.environ.get("WORKFLOW_DRIVER_MODEL_NAME"),
            file_cfg.get("model_name"),
            file_cfg.get("model_id"),
            file_cfg.get("model"),
            file_cfg.get("name"),
        )

        missing = []
        if not base_url:
            missing.append("base_url")
        if not api_key:
            missing.append("api_key")
        if not model_name:
            missing.append("model")
        if missing:
            raise RuntimeError(
                "model api settings incomplete; missing "
                + ", ".join(missing)
                + f" in {self.gateway_config}",
            )

        def pick_int(key: str, default: int) -> int:
            value = self._pick_first(step_cfg.get(key), file_cfg.get(key))
            if value is None:
                return default
            return int(value)

        def pick_float(key: str, default: float | None) -> float | None:
            value = self._pick_first(step_cfg.get(key), file_cfg.get(key))
            if value is None:
                return default
            return float(value)

        headers = {}
        if isinstance(file_cfg.get("headers"), dict):
            headers.update(file_cfg["headers"])
        if isinstance(step_cfg.get("headers"), dict):
            headers.update(step_cfg["headers"])

        return {
            "provider": provider,
            "base_url": str(base_url).rstrip("/"),
            "api_key": str(api_key),
            "model": str(model_name),
            "max_tokens": pick_int("max_tokens", 8192),
            "max_tokens_param": self._pick_first(
                step_cfg.get("max_tokens_param"),
                file_cfg.get("max_tokens_param"),
                "max_tokens",
            ),
            "temperature": pick_float("temperature", 0.0),
            "timeout_seconds": pick_int("timeout_seconds", 600),
            "headers": headers,
            "response_format": self._pick_first(step_cfg.get("response_format"), file_cfg.get("response_format")),
            "anthropic_version": self._pick_first(
                step_cfg.get("anthropic_version"),
                file_cfg.get("anthropic_version"),
                "2023-06-01",
            ),
        }

    def load_notification_settings(self) -> dict[str, Any]:
        cfg = self.load_config()
        notify_cfg = cfg.get("notification") or cfg.get("notify") or {}
        if not isinstance(notify_cfg, dict):
            notify_cfg = {}
        enabled = self._as_bool(
            self._pick_first(
                os.environ.get("WORKFLOW_DRIVER_NOTIFY_ENABLED"),
                notify_cfg.get("enabled"),
            ),
            default=True,
        )
        timeout_seconds = int(
            self._pick_first(
                os.environ.get("WORKFLOW_DRIVER_NOTIFY_TIMEOUT_SECONDS"),
                notify_cfg.get("timeout_seconds"),
                30,
            ),
        )
        return {
            "enabled": enabled,
            "url": self._pick_first(
                os.environ.get("WORKFLOW_DRIVER_NOTIFY_URL"),
                notify_cfg.get("url"),
                DEFAULT_NOTIFICATION_URL,
            ),
            "timeout_seconds": timeout_seconds,
        }

    def build_script_command(
        self,
        *,
        script_path: Path,
        input_path: Path,
        output_path: Path,
        project_root: Path | None = None,
    ) -> list[str]:
        if self.script_command_template:
            command = self.script_command_template.format(
                script_path=str(script_path),
                input_path=str(input_path),
                output_path=str(output_path),
                workspace=str(self.workspace),
                project_root=str(project_root or self.workspace),
            )
            return shlex.split(command)

        command = ["uv", "run"]
        if project_root:
            command.extend(["--project", str(project_root)])
        command.extend(["python", str(script_path), "--input", str(input_path), "--output", str(output_path)])
        return command
