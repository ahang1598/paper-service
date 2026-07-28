# -*- coding: utf-8 -*-
"""Action → Handler 注册机制（接口兼容性核心）。

设计目标：
    新增一种查询（如根据 url 查摘要、查元数据）时，只需：
      1) 编写一个 BaseQueryHandler 子类；
      2) 用 @register("<action_name>") 装饰；
      3) 定义对应的 params schema。
    无需修改路由层，也无需修改 MCP 侧（MCP 只映射一个 query(action, params) 函数）。

Handler 契约：
    - params_schema：各 action 自己的参数校验类（pydantic BaseModel）；
    - execute(params)：一次性返回完整结果（HTTP 一次性查询用）；
    - stream(params)：异步产出事件（WebSocket 流式用），默认实现退化为单个 done 事件。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any, AsyncIterator, ClassVar, Optional, Type

from pydantic import BaseModel


class Event(dict):
    """流式事件（dict 子类，便于序列化为 JSON 发送给 WS 客户端）。

    约定字段：type ∈ {"progress", "chunk", "done", "error"}。
    """


@dataclass
class HandlerContext:
    """Handler 执行上下文，承载运行期依赖（如 client 工厂）。

    通过依赖注入传入 handler，便于测试替换。后续扩展（如缓存、限流）可在此增加字段。
    """

    # client 工厂：根据 settings 构造下游客户端。默认 None，由 deps 注入。
    client_factory: Optional[Any] = None
    # 默认设备信息（请求级 device 覆盖时作为兜底）。
    default_device: Optional[Any] = None
    # 当前请求的 request_id（由调用层注入，handler 用于构造下游请求）。
    request_id: str = ""
    # 全局 settings 引用（handler 需要读取运行期配置时使用）。
    settings: Optional[Any] = None


class BaseQueryHandler(abc.ABC):
    """查询处理器抽象基类。

    子类需设置：
        action: ClassVar[str]          —— 注册名
        params_schema: ClassVar[type[BaseModel]] —— 参数校验类

    子类需实现：
        async def execute(self, params, ctx) -> dict   —— 一次性结果

    可选覆盖：
        async def stream(self, params, ctx) -> AsyncIterator[Event]  —— 流式事件
    """

    action: ClassVar[str] = ""
    params_schema: ClassVar[Type[BaseModel]] = BaseModel

    @abc.abstractmethod
    async def execute(self, params: BaseModel, ctx: HandlerContext) -> dict[str, Any]:
        """一次性执行，返回结果 data（放入统一 envelope 的 data 字段）。"""
        raise NotImplementedError

    async def stream(
        self, params: BaseModel, ctx: HandlerContext
    ) -> AsyncIterator[Event]:
        """默认流式实现：先发 progress，再发 done（结果同 execute）。

        子类可覆盖以产出真实的增量事件（如逐 chunk 推送）。
        """
        yield Event({"type": "progress", "message": "started"})
        data = await self.execute(params, ctx)
        yield Event({"type": "done", "data": data})


# =====================================================================
# 注册表
# =====================================================================

# 全局注册表：action_name -> handler 类
_REGISTRY: dict[str, type[BaseQueryHandler]] = {}


def register(action: str):
    """类装饰器：将 handler 类注册到全局注册表。

    用法：
        @register("doc_fulltext")
        class DocFullTextHandler(BaseQueryHandler): ...
    """

    def _wrap(cls: type[BaseQueryHandler]) -> type[BaseQueryHandler]:
        if not action:
            raise ValueError("register: action 名不能为空")
        if action in _REGISTRY:
            raise ValueError(f"register: action '{action}' 已被注册")
        cls.action = action
        _REGISTRY[action] = cls
        return cls

    return _wrap


def get_handler_class(action: str) -> Optional[type[BaseQueryHandler]]:
    """根据 action 名获取 handler 类；不存在返回 None。"""
    return _REGISTRY.get(action)


def list_actions() -> list[str]:
    """列出所有已注册 action（调试 / 自省用）。"""
    return sorted(_REGISTRY.keys())


def clear_registry() -> None:
    """清空注册表（仅测试用）。"""
    _REGISTRY.clear()
