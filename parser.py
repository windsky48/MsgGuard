# -*- coding: utf-8 -*-
"""
消息解析：把 botpy 收到的群消息统一拆成「可审核的文本 / 图片」条目。

重点：合并转发（打包转发）消息
------------------------------
官方 API 中合并转发对应 `msg_type == 102`，内容位于 `msg_elements`，
且存在**递归嵌套**（一条合并转发里还能再套一条合并转发），
这里用递归遍历把所有叶子节点抽出来逐条送审。

同时做了多版本兼容：
    - 字段名兼容 msg_elements / msgElements
    - 节点既可能是 dict，也可能是 botpy 的 pydantic 对象，统一用 _get 取值
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional

logger = logging.getLogger("msgguard.parser")

# 合并转发的 msg_type：102 为需求给定值，9 为部分版本/频道侧的取值，一并处理
MERGED_MSG_TYPES = (102, 9)

# 递归保护，防止异常结构导致无限递归
MAX_DEPTH = 8


@dataclass
class MediaItem:
    """一条待审核内容。"""

    kind: str          # text / image
    value: str         # 文本内容 或 图片 URL
    label: str = ""    # 展示用标签，写日志时说明来源（例如「合并转发#2」）


def _get(obj: Any, *names: str) -> Any:
    """从 dict 或对象中取第一个存在且非空的字段。"""
    for name in names:
        if isinstance(obj, dict):
            value = obj.get(name)
        else:
            value = getattr(obj, name, None)
        if value not in (None, "", [], {}):
            return value
    return None


def is_merged_forward(message) -> bool:
    """判断是否为合并转发消息。"""
    if getattr(message, "msg_type", None) in MERGED_MSG_TYPES:
        return True
    # 兜底：没有 msg_type 但带 msg_elements，也按合并转发处理
    return bool(_get(message, "msg_elements", "msgElements", "msg_element"))


def _extract_from_element(element: Any, depth: int, path: str, out: List[MediaItem]) -> None:
    """处理一个 msg_element 节点。"""
    if depth > MAX_DEPTH:
        logger.warning("合并转发嵌套层级超过 %d，疑似异常结构，已截断。", MAX_DEPTH)
        return

    # 1) 先看是否是嵌套的转发节点（forwardElement / msgElements 又一层）
    nested = _get(element, "forwardElement", "forward_element")
    if nested is not None:
        _extract_from_element(nested, depth + 1, f"{path}/forward", out)
    nested_elements = _get(element, "msg_elements", "msgElements", "msg_element")
    if nested_elements is not None and nested_elements is not element:
        _extract_elements(nested_elements, depth + 1, f"{path}/msg", out)

    # 2) 文本节点：textElement.content / element.text / content
    text_element = _get(element, "textElement", "text_element")
    text = _get(text_element, "content", "text") if text_element is not None else None
    if text is None:
        text = _get(element, "text", "content")
    if isinstance(text, str) and text.strip():
        out.append(MediaItem(kind="text", value=text.strip(), label=path))

    # 3) 图片节点：picElement.url / imageElement / element.url
    pic_element = _get(element, "picElement", "pic_element", "imageElement", "image_element")
    image_url = _get(pic_element, "url", "picUrl", "pic_url", "originImageUrl", "origin_image_url")
    if image_url is None:
        image_url = _get(element, "url", "imageUrl", "image_url")
    if isinstance(image_url, str) and image_url.strip():
        out.append(MediaItem(kind="image", value=image_url.strip(), label=path))


def _extract_elements(elements: Any, depth: int, path: str, out: List[MediaItem]) -> None:
    """遍历一组 msg_element。"""
    if not isinstance(elements, (list, tuple)):
        return
    for index, element in enumerate(elements, start=1):
        try:
            _extract_from_element(element, depth, f"{path}/{index}", out)
        except Exception as exc:  # 单个子节点解析失败不影响其它节点
            logger.warning("解析合并转发子节点失败(%s/%s)：%s", path, index, exc)


def extract_items(message) -> List[MediaItem]:
    """从任意群消息中抽取所有待审核的文本 / 图片条目。"""
    if not is_merged_forward(message):
        items: List[MediaItem] = []
        content = _get(message, "content")
        if isinstance(content, str) and content.strip():
            items.append(MediaItem(kind="text", value=content.strip(), label="message"))

        # 普通图片消息：message.image.url
        image = _get(message, "image")
        image_url = _get(image, "url", "image_url", "file_url") if image is not None else None
        if image_url is None:
            image_url = _get(message, "image_url", "url")
        if isinstance(image_url, str) and image_url.strip() and image_url != content:
            items.append(MediaItem(kind="image", value=image_url.strip(), label="message"))
        return items

    # 合并转发：递归展开 msg_elements
    elements = _get(message, "msg_elements", "msgElements", "msg_element")
    merged_items: List[MediaItem] = []
    _extract_elements(elements, 1, "merged", merged_items)
    logger.debug("合并转发消息共解析出 %d 条待审内容。", len(merged_items))
    return merged_items


def get_user_openid(message) -> str:
    """取出发送者 user_openid（不同版本字段名不一致，逐个兜底）。"""
    author = _get(message, "author", "member", "user")
    if author is not None:
        for field in ("user_openid", "member_openid", "openid", "id"):
            value = _get(author, field)
            if value:
                return str(value)
    for field in ("user_openid", "member_openid", "author_openid"):
        value = _get(message, field)
        if value:
            return str(value)
    return ""


def get_group_openid(message) -> str:
    for field in ("group_openid", "groupOpenid", "channel_id", "guild_id"):
        value = _get(message, field)
        if value:
            return str(value)
    return ""


def get_message_timestamp(message) -> Optional[float]:
    """取消息时间戳（用于判断是否还在 2 分钟撤回时限内）。"""
    value = _get(message, "timestamp", "ts", "create_time")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            from datetime import datetime

            # 兼容 "2024-01-01T00:00:00+08:00" 与 "2024-01-01 00:00:00"
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None