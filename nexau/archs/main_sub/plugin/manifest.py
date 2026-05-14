"""Pydantic schema for RFC-0024 plugin manifests."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexau.archs.main_sub.config.schema import MCPServerConfig, PluginEntryConfig, SubAgentConfigEntry, ToolConfigEntry

PluginConfigValue = str | int | float | bool | list[str]
PluginConfigValues = Mapping[str, PluginConfigValue]


def _plugin_manifest_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_dir():
        return candidate / "plugin.yaml"
    return candidate


def _load_plugin_manifest_mapping(path: Path) -> dict[str, object]:
    if not path.exists():
        raise FileNotFoundError(f"Plugin YAML file not found: {path}")

    loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"plugin.yaml must be a mapping: {path}")

    raw = cast(dict[object, object], loaded)
    result: dict[str, object] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ValueError(f"plugin.yaml keys must be strings: {path}")
        result[key] = value
    return result


def _plugin_path_uri(plugin_dir: Path, base_path: str | Path | None) -> str:
    resolved_plugin_dir = plugin_dir.resolve(strict=False)
    if base_path is None:
        body = str(resolved_plugin_dir)
    else:
        resolved_base_path = Path(base_path).resolve(strict=False)
        try:
            body = os.path.relpath(resolved_plugin_dir, resolved_base_path)
        except ValueError:
            body = str(resolved_plugin_dir)
        if not Path(body).is_absolute() and not body.startswith((".", os.sep)):
            body = f".{os.sep}{body}"
    # Plugin entries use URI-style forward slashes even when the local path was
    # built with Windows separators.
    return f"path:{body.replace(os.sep, '/')}"


class Plugin(PluginEntryConfig):
    """Programmatic plugin entry for ``AgentConfig`` construction."""

    @classmethod
    def from_yaml(
        cls,
        yaml_path: str | Path,
        *,
        config: PluginConfigValues | None = None,
        base_path: str | Path | None = None,
    ) -> Plugin:
        """Create a plugin entry from a plugin manifest YAML file or directory."""
        manifest_path = _plugin_manifest_path(yaml_path)
        PluginManifest.from_yaml(manifest_path)
        return cls(use=_plugin_path_uri(manifest_path.parent, base_path), config=dict(config or {}))

    @classmethod
    def from_path(
        cls,
        plugin_dir: str | Path,
        *,
        config: PluginConfigValues | None = None,
        base_path: str | Path | None = None,
    ) -> Plugin:
        """Create a plugin entry from a plugin directory containing ``plugin.yaml``."""
        return cls.from_yaml(Path(plugin_dir) / "plugin.yaml", config=config, base_path=base_path)


class PluginEngineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nexau: str


class PluginConfigProperty(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["string", "integer", "number", "boolean", "string_array"]
    required: bool = False
    default: Any = None
    description: str | None = None
    enum: list[str] | None = None

    @model_validator(mode="after")
    def _validate_enum(self) -> PluginConfigProperty:
        if self.enum is not None and self.type != "string":
            raise ValueError("enum is only supported for string config properties")
        return self


class PluginConfigSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    properties: dict[str, PluginConfigProperty] = Field(default_factory=dict)


class SkillContribution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    path: str


class MiddlewareContribution(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    import_path: str = Field(alias="import")
    params: dict[str, Any] | None = None


def _empty_mcp_server_list() -> list[MCPServerConfig]:
    return []


def _empty_tool_list() -> list[ToolConfigEntry]:
    return []


def _empty_skill_list() -> list[SkillContribution]:
    return []


def _empty_sub_agent_list() -> list[SubAgentConfigEntry]:
    return []


def _empty_middleware_list() -> list[MiddlewareContribution]:
    return []


class PluginContributions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mcp_servers: list[MCPServerConfig] = Field(default_factory=_empty_mcp_server_list)
    tools: list[ToolConfigEntry] = Field(default_factory=_empty_tool_list)
    skills: list[SkillContribution] = Field(default_factory=_empty_skill_list)
    sub_agents: list[SubAgentConfigEntry] = Field(default_factory=_empty_sub_agent_list)
    middlewares: list[MiddlewareContribution] = Field(default_factory=_empty_middleware_list)


class PluginManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["plugin"] | None = None
    name: str
    version: str
    description: str | None = None
    engines: PluginEngineConfig
    config: PluginConfigSchema = Field(default_factory=PluginConfigSchema)
    system_prompt_fragment: str | None = None
    contributes: PluginContributions = Field(default_factory=PluginContributions)

    @classmethod
    def from_yaml(cls, yaml_path: str | Path) -> PluginManifest:
        """Load and validate a plugin manifest from YAML."""
        return cls.model_validate(_load_plugin_manifest_mapping(_plugin_manifest_path(yaml_path)))
