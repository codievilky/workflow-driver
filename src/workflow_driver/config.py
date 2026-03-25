from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path


DEFAULT_GATEWAY_CONFIG = Path.home() / ".workflow-driver" / "config.json"


@dataclass(frozen=True)
class RuntimeContext:
    workspace: Path
    state_root: Path
    gateway_url: str | None
    gateway_token: str | None
    gateway_config: Path
    script_command_template: str | None

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

        return cls(
            workspace=workspace_path,
            state_root=state_root_path.resolve(),
            gateway_url=gateway_url or os.environ.get("WORKFLOW_DRIVER_GATEWAY_URL"),
            gateway_token=gateway_token or os.environ.get("WORKFLOW_DRIVER_GATEWAY_TOKEN"),
            gateway_config=gateway_config_path,
            script_command_template=script_command_template or os.environ.get("WORKFLOW_DRIVER_SCRIPT_COMMAND"),
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

    def load_gateway_settings(self) -> tuple[str, str]:
        if self.gateway_url and self.gateway_token:
            return self.gateway_url, self.gateway_token

        if not self.gateway_config.exists():
            raise RuntimeError(
                "gateway settings not configured; provide --gateway-url/--gateway-token "
                "or create ~/.workflow-driver/config.json",
            )

        cfg = json.loads(self.gateway_config.read_text(encoding="utf-8"))
        gateway = cfg.get("gateway") or {}
        auth = gateway.get("auth") or {}
        configured_url = gateway.get("url")
        port = gateway.get("port") or 18789
        token = self.gateway_token or auth.get("token")
        if not token:
            raise RuntimeError("gateway auth token not configured")
        url = self.gateway_url or configured_url or f"http://localhost:{port}/tools/invoke"
        return url, token

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
