from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .errors import RadarError
from .prompts import TASKS


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AppSettings(ConfigModel):
    model_mode: Literal["live", "demo", "unconfigured"] = "unconfigured"
    database_path: str = "data/radar.sqlite3"


class ReportSettings(ConfigModel):
    root_dir: str = "reports"
    history_dir: str = "data/report_versions"


class LLMProfile(ConfigModel):
    url: str
    model: str
    api_key: str = ""
    api_key_env: str = ""
    timeout_seconds: float = Field(default=90, gt=0, le=600)
    max_context_chars: int = Field(default=180000, ge=1000)
    temperature: float = Field(default=0, ge=0, le=2)
    max_tokens: int | None = Field(default=6000, ge=1)
    top_p: float | None = Field(default=1, gt=0, le=1)
    verify_tls: bool = True
    trust_env: bool = False
    headers: dict[str, str] = Field(default_factory=dict)
    extra_body: dict = Field(default_factory=dict)

    def resolved_api_key(self):
        if self.api_key:
            return self.api_key
        return os.getenv(self.api_key_env, "") if self.api_key_env else ""


class TaskSettings(ConfigModel):
    profile: str = "default"
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1)
    top_p: float | None = Field(default=None, gt=0, le=1)
    timeout_seconds: float | None = Field(default=None, gt=0, le=600)
    max_context_chars: int | None = Field(default=None, ge=1000)


class RadarConfig(ConfigModel):
    app: AppSettings = Field(default_factory=AppSettings)
    reports: ReportSettings = Field(default_factory=ReportSettings)
    llm_profiles: dict[str, LLMProfile] = Field(default_factory=dict)
    tasks: dict[str, TaskSettings] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_routes(self):
        unknown = set(self.tasks) - set(TASKS)
        if unknown:
            raise ValueError(f"存在未知模型任务：{', '.join(sorted(unknown))}")
        for task, settings in self.tasks.items():
            if settings.profile not in self.llm_profiles:
                raise ValueError(f"任务 {task} 引用了不存在的模型配置 {settings.profile}")
        if self.app.model_mode == "live":
            if not self.llm_profiles:
                raise ValueError("live 模式至少需要一个 llm_profiles 配置")
            missing = set(TASKS) - set(self.tasks)
            if missing:
                raise ValueError(f"live 模式缺少任务配置：{', '.join(sorted(missing))}")
        return self


def load_config(path: str | Path | None = None):
    selected = Path(path or os.getenv("RADAR_CONFIG", "config/radar.toml"))
    if not selected.is_file():
        raise RadarError("config_not_found", f"找不到配置文件：{selected}", 500)
    try:
        with selected.open("rb") as stream:
            return RadarConfig.model_validate(tomllib.load(stream))
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        raise RadarError("invalid_config", f"配置文件无效：{exc}", 500) from exc
