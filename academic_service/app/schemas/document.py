# -*- coding: utf-8 -*-
"""doc_fulltext action 专属参数 schema。

对应下游 proto DocFullTextRequest 的可变部分。
常量（字段名等）复用 app.clients.doc_fulltext_client。
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class DeviceParams(BaseModel):
    """设备信息参数（请求级可选覆盖；缺省由 settings 默认值填充）。"""

    app_version: Optional[str] = None
    device_id: Optional[str] = None
    device_model: Optional[str] = None
    device_type: Optional[str] = None
    prd_pkg_name: Optional[str] = None


class DocFullTextParams(BaseModel):
    """doc_fulltext 的 action 专属参数。

    校验规则：
        - file_id 与 doc_hash 二选一（至少传一个）；
        - splitter 必须是 0（CHAPTER）或 1（SMALL_CHUNK）；
        - pages 中每个元素必须是非负整数（负数/非整数拒绝）；
        - with_rect 默认 False。
    """

    file_id: Optional[str] = Field(default=None, description="文件ID，与 doc_hash 二选一")
    doc_hash: Optional[str] = Field(default=None, description="文档hash，与 file_id 二选一")
    splitter: int = Field(default=1, description="切分方式：0=大块CHAPTER, 1=小块SMALL_CHUNK")
    pages: List[int] = Field(default_factory=list, description="页码范围，空表示全部")
    with_rect: bool = Field(default=False, description="是否返回 rect")
    device: Optional[DeviceParams] = Field(default=None, description="设备信息覆盖")

    @field_validator("splitter")
    @classmethod
    def _check_splitter(cls, v: int) -> int:
        if v not in (0, 1):
            raise ValueError("splitter 必须为 0(CHAPTER) 或 1(SMALL_CHUNK)")
        return v

    @field_validator("pages")
    @classmethod
    def _check_pages(cls, v: List[int]) -> List[int]:
        for p in v:
            # pydantic 已保证元素为 int；这里再拒绝负数
            if not isinstance(p, int) or isinstance(p, bool):
                raise ValueError(f"pages 元素必须为整数，得到 {p!r}")
            if p < 0:
                raise ValueError(f"pages 元素不能为负数，得到 {p}")
        return v

    @model_validator(mode="after")
    def _check_id_or_hash(self) -> "DocFullTextParams":
        if not self.file_id and not self.doc_hash:
            raise ValueError("file_id 与 doc_hash 至少传一个")
        return self
