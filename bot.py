# -*- coding: utf-8 -*-
"""
MsgGuard · QQ 群消息违规自动审核机器人（主入口）

流程概览
--------
    收到群消息
        ├─ 普通文本 / 图片 / 合并转发(102)
        ├─ 递归拆解合并转发 -> 逐条送阿里云内容审核（porn、terror）
        ├─ label != normal 且 rate >= 0.8  -> 违规
        │       ├─ 1. 撤回消息（2 分钟时限）
        │       ├─ 2. ViolationsNum < 1  -> @警告 + 禁言 86400s + 次数+1
        │       │      ViolationsNum >= 1 -> 踢出群聊
        │       └─ 3. 写审计日志（内容/发送者/时间/审核返回/违规次数）
        └─ 任何异常只记日志，不影响机器人运行

启动：
    pip install -r requirements.txt
    复制 .env.example 为 .env 并填入配置
    python bot.py
"""

from __future__ import annotations

import sys
import time
from typing import Any, Dict, List

from config import settings
from handlers import GuardHandler
from logger_setup import setup_logging
from moderation import ContentModerator
from qq_openapi import QQOpenAPI
from storage import ViolationStore
from whitelist import WhitelistManager

logger = setup_logging()

# 全局单例（botpy 事件回调需要能访问）
store = ViolationStore()
whitelist = WhitelistManager()
moderator = ContentModerator()
client_ref: Dict[str, Any] = {}


# --------------------------------------------------------------------------- #
# 事件去重：on_group_message_create 与 on_group_at_message_create
# 在部分网关版本下会对**同一条消息**都触发，这里保证只处理一次。
# --------------------------------------------------------------------------- #
_SEEN_MESSAGES: Dict[str, float] = {}
_SEEN_TTL_SECONDS = 300  # 撤回时限只有 2 分钟，缓存 5 分钟足够


def _is_duplicate(message) -> bool:
    try:
        key = f"{getattr(message, 'group_openid', '')}:{getattr(message, 'id', '')}"
    except Exception:
        return False
    if not key or key.endswith(":"):
        return False

    now = time.time()
    if len(_SEEN_MESSAGES) > 1024:  # 简单清理，防止内存无限增长
        for old_key, ts in list(_SEEN_MESSAGES.items()):
            if now - ts > _SEEN_TTL_SECONDS:
                _SEEN_MESSAGES.pop(old_key, None)

    if _SEEN_MESSAGES.get(key, 0) > now - _SEEN_TTL_SECONDS:
        return True
    _SEEN_MESSAGES[key] = now
    return False


# --------------------------------------------------------------------------- #
# botpy 客户端构造（不同版本参数/事件名有差异，这里做兼容处理）
# --------------------------------------------------------------------------- #
def _build_intents():
    """申请「群全量消息」权限。

    注意：需求要求同时处理 on_group_message_create 与 on_group_at_message_create，
    前者对应 public_group_messages（全量消息权限），后者对应群 @机器人 消息，
    不同 botpy 版本字段名可能是 group_and_at_message_create，这里逐个尝试。
    """
    import botpy

    for kwargs in (
        {"public_group_messages": True},
        {"group_and_at_message_create": True},
        {},
    ):
        try:
            return botpy.Intents(**kwargs)
        except TypeError as exc:
            logger.debug("Intents 构造参数 %s 不被支持：%s", kwargs, exc)
    return botpy.Intents()


def create_client():
    """构造 botpy.Client，兼容不同版本的构造参数。"""
    import botpy

    if not settings.qq_appid or not settings.qq_secret:
        raise RuntimeError("请先在 .env 中配置 QQ_APPID 与 QQ_SECRET")

    intents = _build_intents()
    candidates: List[Dict[str, Any]] = [
        {
            "appid": settings.qq_appid,
            "secret": settings.qq_secret,
            "port": settings.qq_port,
            "is_public": True,
            "intents": intents,
        },
        {"appid": settings.qq_appid, "secret": settings.qq_secret,
         "port": settings.qq_port, "intents": intents},
        {"appid": settings.qq_appid, "secret": settings.qq_secret, "port": settings.qq_port},
    ]

    last_error: Exception | None = None
    for kwargs in candidates:
        try:
            log_kwargs = {k: ("***" if k == "secret" else v) for k, v in kwargs.items() if k != "intents"}
            logger.info("创建 botpy.Client：%s", log_kwargs)
            return botpy.Client(**kwargs)
        except TypeError as exc:
            last_error = exc
            logger.debug("Client 参数不被支持：%s", exc)

    raise RuntimeError(f"创建 botpy.Client 失败：{last_error}")


# --------------------------------------------------------------------------- #
# 事件处理
# --------------------------------------------------------------------------- #
def build_handler(client) -> GuardHandler:
    openapi = QQOpenAPI(client)
    handler = GuardHandler(
        moderator=moderator, store=store, openapi=openapi, whitelist=whitelist
    )
    client_ref["client"] = client
    client_ref["openapi"] = openapi
    return handler


def register_events(client, handler: GuardHandler) -> None:
    """注册两个事件回调（详见模块注释中的版本差异说明）。"""

    @client.event
    async def on_group_message_create(message):
        """群内全量消息事件（需要在机器人后台开启「群消息」全量权限）。"""
        if _is_duplicate(message):
            return
        try:
            await handler.handle_group_message(message)
        except Exception as exc:
            logger.error("处理 on_group_message_create 异常：%s", exc, exc_info=True)

    @client.event
    async def on_group_at_message_create(message):
        """群内 @机器人 消息事件。

        部分网关版本下与 on_group_message_create 同时触发同一条消息，
        这里通过 _is_duplicate 去重，保证一条消息只审核/处置一次。
        """
        if _is_duplicate(message):
            return
        try:
            await handler.handle_group_message(message)
        except Exception as exc:
            logger.error("处理 on_group_at_message_create 异常：%s", exc, exc_info=True)

    @client.event
    async def on_direct_message_create(message):
        """单聊（C2C）事件：只用于响应白名单管理指令。"""
        try:
            await handler.handle_direct_message(message)
        except Exception as exc:
            logger.error("处理 on_direct_message_create 异常：%s", exc, exc_info=True)

    @client.event
    async def on_ready():
        logger.info("机器人已上线，开始监听群消息。审核开关=%s", settings.moderation_enabled)


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #
def self_check(handler: GuardHandler) -> None:
    """启动自检，把关键配置和依赖状态打一遍，方便排查。"""
    logger.info("-" * 68)
    logger.info("MsgGuard 配置自检")
    logger.info("  模式            : %s", "启用审核处置" if settings.moderation_enabled else "仅记录不处置")
    logger.info("  审核场景        : %s", settings.scenes)
    logger.info("  审核接入地域    : %s", settings.region)
    logger.info("  置信度阈值      : %.2f", settings.violation_rate_threshold)
    logger.info("  首次禁言秒数    : %d", settings.mute_seconds)
    logger.info("  二次处置        : %s", "踢出群聊" if settings.kick_on_repeat else "仅记录")
    logger.info("  违规记录文件    : %s", settings.data_file)
    logger.info("  日志目录        : %s", settings.log_dir)
    logger.info("  历史违规人数    : %d", len(store))
    logger.info("  白名单          : %s（%d 人，文件 %s）",
                "启用" if settings.whitelist_enabled else "停用",
                len(whitelist.entries()), settings.whitelist_file)
    logger.info("  白名单管理员    : %s",
                "，".join(settings.whitelist_admins) if settings.whitelist_admins
                else "未配置（加白/移白/清零指令已禁用）")
    logger.info("  内容审核可用    : %s", "是" if moderator.available else "否（检查 AccessKey 与 SDK）")
    logger.info("-" * 68)


def main() -> int:
    # 1) 恢复本地状态：违规次数 + 白名单
    store.load()
    whitelist.load()

    # 2) 构造 botpy 客户端
    try:
        client = create_client()
    except Exception as exc:
        logger.error("启动失败：%s", exc)
        return 1

    # 3) 组装审核与处置处理器
    handler = build_handler(client)
    register_events(client, handler)
    self_check(handler)

    # 4) 运行
    try:
        client.run()
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在停止……")
    finally:
        import asyncio

        try:
            asyncio.run(store.flush())
            asyncio.run(whitelist.flush())
        except Exception:
            pass
        openapi = client_ref.get("openapi")
        if openapi is not None:
            try:
                asyncio.run(openapi.close())
            except Exception:
                pass
        logger.info("MsgGuard 已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())