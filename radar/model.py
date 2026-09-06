from typing import Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .errors import ModelError, RadarError
from .evidence import canonical
from .prompts import COMMON, TASKS

T = TypeVar("T", bound=BaseModel)


class StructuredModel(Protocol):
    mode: str

    def generate(self, task: str, payload: dict, schema: type[T]) -> T: ...


class UnconfiguredModel:
    mode = "unconfigured"

    def generate(self, task, payload, schema):
        raise RadarError("model_not_configured", "请配置内网模型地址与模型名，或使用显式 demo 模式", 503)


class HTTPModel:
    """面向 chat-completions 兼容的内网服务；不含默认外部地址。"""

    mode = "live"

    def __init__(self, url: str, model: str, api_key: str = "", timeout: float = 90,
                 max_context_chars: int = 180000, transport=None, *, temperature: float = 0,
                 max_tokens: int | None = 6000, top_p: float | None = 1,
                 verify_tls: bool = True, trust_env: bool = False, headers=None,
                 extra_body=None, task_options=None):
        self.url, self.model, self.api_key = url, model, api_key
        self.timeout, self.max_context_chars, self.transport = timeout, max_context_chars, transport
        self.temperature, self.max_tokens, self.top_p = temperature, max_tokens, top_p
        self.verify_tls, self.trust_env = verify_tls, trust_env
        self.headers, self.extra_body = headers or {}, extra_body or {}
        self.task_options = task_options or {}

    def _options(self, task):
        options = {
            "url": self.url, "model": self.model, "api_key": self.api_key,
            "timeout": self.timeout, "max_context_chars": self.max_context_chars,
            "temperature": self.temperature, "max_tokens": self.max_tokens,
            "top_p": self.top_p, "verify_tls": self.verify_tls,
            "trust_env": self.trust_env, "headers": self.headers, "extra_body": self.extra_body,
        }
        options.update(self.task_options.get(task, {}))
        return options

    def generate(self, task, payload, schema):
        options = self._options(task)
        system = COMMON + TASKS[task] + "\n输出 Schema：\n" + canonical(schema.model_json_schema())
        user = canonical(payload)
        if len(system) + len(user) > options["max_context_chars"]:
            raise RadarError("context_too_large", "本阶段上下文超限；请缩小输入范围或提高模型上下文预算，不会静默截断证据", 413)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        headers = dict(options["headers"])
        if options["api_key"] and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {options['api_key']}"
        # 仅对输出结构错误修复一次；网络错误不盲目重试。内部材料不写入日志。
        for attempt in range(2):
            try:
                body = dict(options["extra_body"])
                body.update({"model": options["model"], "messages": messages,
                             "temperature": options["temperature"],
                             "response_format": {"type": "json_object"}})
                if options["max_tokens"] is not None:
                    body["max_tokens"] = options["max_tokens"]
                if options["top_p"] is not None:
                    body["top_p"] = options["top_p"]
                with httpx.Client(timeout=options["timeout"], transport=self.transport,
                                  follow_redirects=False, trust_env=options["trust_env"],
                                  verify=options["verify_tls"]) as client:
                    response = client.post(options["url"], headers=headers, json=body)
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
            except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
                raise ModelError("模型连接或协议异常，请检查内网模型服务") from exc
            try:
                return schema.model_validate_json(content)
            except (ValidationError, ValueError, TypeError) as exc:
                if attempt:
                    raise ModelError("模型在一次格式修复后仍未返回有效结构") from exc
                messages.extend([
                    {"role": "assistant", "content": content if isinstance(content, str) else "{}"},
                    {"role": "user", "content": "返回值不符合 Schema。请只修复格式、字段和类型，重新输出完整 JSON，不增加事实。"},
                ])
        raise ModelError()


def model_from_config(config):
    if config.app.model_mode == "demo":
        from .demo import DemoModel
        return DemoModel()
    if config.app.model_mode != "live":
        return UnconfiguredModel()
    task_options = {}
    for task, task_config in config.tasks.items():
        profile = config.llm_profiles[task_config.profile]
        options = {
            "url": profile.url, "model": profile.model, "api_key": profile.resolved_api_key(),
            "timeout": task_config.timeout_seconds or profile.timeout_seconds,
            "max_context_chars": task_config.max_context_chars or profile.max_context_chars,
            "temperature": profile.temperature if task_config.temperature is None else task_config.temperature,
            "max_tokens": profile.max_tokens if task_config.max_tokens is None else task_config.max_tokens,
            "top_p": profile.top_p if task_config.top_p is None else task_config.top_p,
            "verify_tls": profile.verify_tls, "trust_env": profile.trust_env,
            "headers": profile.headers, "extra_body": profile.extra_body,
        }
        task_options[task] = options
    default = next(iter(task_options.values()))
    return HTTPModel(default["url"], default["model"], default["api_key"],
                     default["timeout"], default["max_context_chars"],
                     temperature=default["temperature"], max_tokens=default["max_tokens"],
                     top_p=default["top_p"], verify_tls=default["verify_tls"],
                     trust_env=default["trust_env"], headers=default["headers"],
                     extra_body=default["extra_body"], task_options=task_options)
