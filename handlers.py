# -*- coding: utf-8 -*-
"""
处置流程编排：审核 -> 撤回 -> 禁言 / 踢出 -> 记日志

严格执行需求中的顺序：
    1. 先撤回消息（官方 2 分钟时限，超时只记日志）
    2. 读取 ViolationsNum：
         < 1（首次）-> @警告 + 禁言 86400 秒 -> ViolationsNum += 1
         >= 1（再次）-> 直接踢出群聊
    3. 把违规内容、发送者 ID、时间、审核返回、当前违规次数写入日志文件

任何一步失败都只记录 error 日志，不影响后续步骤与机器人运行。

白名单
------
消息进入流程后的顺序是：
    机器人自己 -> 管理员指令 -> 白名单豁免 -> 正式审核
因此白名单成员不会被撤回/禁言/踢出，也不会消耗审核接口额度。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config import settings
from logger_setup import write_violation_record
from moderation import ContentModerator, ModerationResult
from parser import (
    MediaItem,
    extract_items,
    get_group_openid,
    get_message_timestamp,
    get_user_openid,
    is_merged_forward,
)
from qq_openapi import QQOpenAPI
from storage import ViolationStore
from whitelist import WhitelistManager

logger = logging.getLogger("msgguard.handler")

# 需求原文：“@发送者 警告一次，禁止发送色情、恐怖、涉政内容！本次禁言一天，下次踢出群聊！”
# 其中「@发送者」在 QQ 开放接口里要用 <@!user_openid> 拼接。
WARN_TEXT = "警告一次，禁止发送色情、恐怖、涉政内容！本次禁言一天，下次踢出群聊！"

HELP_TEXT = (
    "MsgGuard 管理指令（仅管理员）\n"
    "  #加白 <openid> [备注]   把成员加入白名单，永久豁免审核\n"
    "  #移白 <openid>          从白名单移除\n"
    "  #清零 <openid>          清零该成员的违规次数\n"
    "  #白名单                 列出当前全部白名单成员\n"
    "  #查次数 <openid>        查询违规次数\n"
    "  #帮助                   显示本说明\n"
    "提示：在群里 @对方后，正文会出现 <@!openid>，可直接粘进指令；"
    "也支持直接把 openid 写出来。"
)

# 匹配群里 @ 成员后残留的 <@!openid>
_AT_PATTERN = re.compile(r"<@!([A-Za-z0-9_\-]+)>")
# 所有以 # 开头的已知指令
_COMMANDS = ("#帮助", "#help", "#加白", "#移白", "#清零", "#白名单", "#查次数")


@dataclass
class MessageContext:
    """一次群消息处理的上下文。"""

    group_openid: str
    user_openid: str
    message: Any
    merged: bool = False
    timestamp: float = field(default_factory=time.time)
    message_id: str = ""

    @property
    def time_str(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.timestamp))


class GuardHandler:
    """群消息审核与处置的核心调度器。"""

    def __init__(
        self,
        moderator: ContentModerator,
        store: ViolationStore,
        openapi: QQOpenAPI,
        whitelist: Optional[WhitelistManager] = None,
    ) -> None:
        self.moderator = moderator
        self.store = store
        self.openapi = openapi
        self.whitelist = whitelist or WhitelistManager()

    # ------------------------------------------------------------------ #
    # 入口
    # ------------------------------------------------------------------ #
    async def handle_group_message(self, message) -> None:
        """处理一条群消息（on_group_message_create / on_group_at_message_create 共用）。"""
        group_openid = get_group_openid(message)
        user_openid = get_user_openid(message)

        if not group_openid or not user_openid:
            logger.debug("无法解析群或用户标识，跳过。")
            return

        # 忽略机器人自己发的消息，避免自审自罚
        if settings.bot_user_openid and user_openid == settings.bot_user_openid:
            return

        ctx = MessageContext(
            group_openid=group_openid,
            user_openid=user_openid,
            message=message,
            merged=is_merged_forward(message),
            timestamp=get_message_timestamp(message) or time.time(),
            message_id=str(getattr(message, "id", "") or ""),
        )

        # 1) 管理员指令优先处理（不送审）
        if await self._try_handle_command(ctx):
            return

        # 2) 白名单豁免：完全不调用审核接口
        if self.whitelist.is_exempt(user_openid):
            logger.debug("命中白名单，跳过审核：%s", user_openid)
            return

        items = extract_items(message)
        if not items:
            return

        logger.debug(
            "待审核消息：group=%s user=%s merged=%s items=%d",
            group_openid, user_openid, ctx.merged, len(items),
        )

        # 逐条送审；任意一条违规即触发处置（合并转发里的子消息无法单独撤回，只撤回整条转发消息）
        for item in items:
            try:
                result = await self.moderator.review(item.kind, item.value)
            except Exception as exc:
                # 审核接口异常绝不能影响机器人运行
                logger.error("审核过程异常，消息已跳过：%s", exc, exc_info=settings.debug)
                continue

            if result.error:
                logger.error("审核接口不可用，本次跳过该条内容：%s", result.error)
                continue

            if result.is_violation:
                await self._handle_violation(ctx, item, result)
                return   # 同一条消息只处置一次，避免重复禁言/踢出

    # ------------------------------------------------------------------ #
    # 管理员指令（白名单 / 清零违规次数）
    # ------------------------------------------------------------------ #
    async def handle_direct_message(self, message) -> None:
        """处理机器人私聊（C2C）消息：支持管理指令，其它内容回帮助。"""
        user_openid = get_user_openid(message)
        content = getattr(message, "content", "") or ""
        if not isinstance(content, str) or not content.strip():
            return

        ctx = MessageContext(
            group_openid="",
            user_openid=user_openid,
            message=message,
            timestamp=get_message_timestamp(message) or time.time(),
            message_id=str(getattr(message, "id", "") or ""),
        )
        if await self._try_handle_command(ctx):
            return
        await self._reply(ctx, HELP_TEXT)

    @staticmethod
    def _extract_target(args: str) -> tuple:
        """从指令参数中取出目标 openid 与剩余备注。

        兼容两种写法：
            #加白 ABC123 运营号
            #加白 <@!ABC123>       （群里 @ 对方后正文里的残留形式）
        """
        match = _AT_PATTERN.search(args)
        if match:
            rest = (args[:match.start()] + " " + args[match.end():]).strip()
            return match.group(1), rest
        parts = args.split(maxsplit=1)
        if not parts:
            return "", ""
        return parts[0], (parts[1].strip() if len(parts) > 1 else "")

    async def _try_handle_command(self, ctx: MessageContext) -> bool:
        """尝试解析并执行管理指令。

        返回 True 表示这条消息已被指令消费（不再进入审核流程）。
        """
        content = getattr(ctx.message, "content", "") or ""
        if not isinstance(content, str):
            return False

        text = content.strip()
        if not text.startswith("#"):
            return False

        tokens = text.split(None, 1)
        command = tokens[0].lower()
        args = tokens[1].strip() if len(tokens) > 1 else ""

        # 不是已知指令 -> 当作普通消息继续审核（避免误吞正常内容）
        if command not in _COMMANDS:
            return False

        # 权限校验：只有 WHITELIST_ADMINS 里的 openid 可以操作
        if not WhitelistManager.is_admin(ctx.user_openid):
            logger.warning("非管理员尝试执行指令，已拦截：%s -> %s", ctx.user_openid, command)
            await self._reply(ctx, f"你没有执行「{command}」的权限。")
            return True

        target, note = self._extract_target(args)

        if command in ("#帮助", "#help"):
            await self._reply(ctx, HELP_TEXT)

        elif command == "#白名单":
            await self._reply(ctx, self.whitelist.describe())

        elif command == "#查次数":
            if not target:
                await self._reply(ctx, "格式：#查次数 <openid>")
            else:
                await self._reply(
                    ctx,
                    f"{target} 当前违规次数：{self.store.get(target)}"
                    f"（{'白名单成员' if self.whitelist.is_exempt(target) else '普通成员'}）",
                )

        elif command == "#加白":
            if not target:
                await self._reply(ctx, "格式：#加白 <openid> [备注]")
            elif not self.whitelist.is_exempt(target):
                await self.whitelist.add(target, note)
                extra = ""
                if settings.whitelist_reset_violations:
                    await self.store.reset(target)      # 加白同时清零历史违规
                    extra = "，并已清零其违规次数"
                await self._reply(ctx, f"已加入白名单：{target}{extra}。")
            else:
                await self._reply(ctx, f"{target} 已在白名单中。")

        elif command == "#移白":
            if not target:
                await self._reply(ctx, "格式：#移白 <openid>")
            elif await self.whitelist.remove(target):
                await self._reply(ctx, f"已移出白名单：{target}。")
            else:
                await self._reply(ctx, f"{target} 不在白名单中。")

        elif command == "#清零":
            if not target:
                await self._reply(ctx, "格式：#清零 <openid>")
            else:
                await self.store.reset(target)
                await self._reply(ctx, f"已清零 {target} 的违规次数。")

        return True

    async def _reply(self, ctx: MessageContext, content: str) -> bool:
        """统一回复：优先 botpy reply，失败回退 OpenAPI。"""
        try:
            reply = getattr(ctx.message, "reply", None)
            if callable(reply):
                try:
                    await reply(msg_type=0, content=content)
                except TypeError:
                    # 少数版本签名为 reply(msg_type, content) 位置参数
                    await reply(0, content)
                return True
        except Exception as exc:
            logger.warning("botpy reply 失败，回退 OpenAPI：%s", exc)

        if ctx.group_openid:
            return await self.openapi.send_group_message(ctx.group_openid, content)
        # 私聊走 C2C 接口
        return await self.openapi.send_direct_message(ctx.user_openid, content)

    # ------------------------------------------------------------------ #
    # 违规处置
    # ------------------------------------------------------------------ #
    async def _handle_violation(
        self, ctx: MessageContext, item: MediaItem, result: ModerationResult
    ) -> None:
        """检测到违规后的完整处置链路。"""
        logger.warning(
            "🚨 检出违规内容 | group=%s user=%s type=%s 原因=%s",
            ctx.group_openid, ctx.user_openid, item.kind, "，".join(result.reasons),
        )

        actions: List[str] = []

        # 1) 先撤回（注意官方 2 分钟时限）
        if await self._recall(ctx):
            actions.append("recall")

        # 2) 根据当前违规次数决定处置方式
        current_count = self.store.get(ctx.user_openid)
        final_count = current_count

        if current_count < settings.repeat_threshold:
            # 首次违规：警告 + 禁言一天
            if await self._send_warning(ctx):
                actions.append("warn")
            if await self._mute(ctx):
                actions.append("mute")
            final_count = await self.store.increment(ctx.user_openid)
        elif settings.kick_on_repeat:
            # 第二次及以上：直接踢出
            if await self._kick(ctx):
                actions.append("kick")

        # 3) 记录审计日志
        self._log_violation(ctx, item, result, final_count, actions)

    async def _recall(self, ctx: MessageContext) -> bool:
        """撤回消息，超出 2 分钟时限则跳过。"""
        age = time.time() - ctx.timestamp
        if age > settings.recall_limit_seconds:
            logger.warning(
                "消息已发送 %.1f 秒，超过 %d 秒撤回时限，放弃撤回。",
                age, settings.recall_limit_seconds,
            )
            return False
        try:
            ok = await self.openapi.recall_message(ctx.message)
            if not ok:
                logger.error("撤回失败（可能是已超过 2 分钟时限或权限不足）。")
            return ok
        except Exception as exc:
            logger.error("撤回异常：%s", exc)
            return False

    async def _send_warning(self, ctx: MessageContext) -> bool:
        """@发送者 发送警告文案。"""
        # QQ 群 @ 成员语法：<@!user_openid>
        return await self._reply(ctx, f"<@!{ctx.user_openid}> {WARN_TEXT}")

    async def _mute(self, ctx: MessageContext) -> bool:
        """禁言：使用 botpy/官方禁言接口，duration 单位为秒。"""
        try:
            return await self.openapi.mute_member(
                ctx.group_openid, ctx.user_openid, settings.mute_seconds
            )
        except Exception as exc:
            logger.error("禁言异常：%s", exc)
            return False

    async def _kick(self, ctx: MessageContext) -> bool:
        """踢出群聊。"""
        try:
            return await self.openapi.kick_member(ctx.group_openid, ctx.user_openid)
        except Exception as exc:
            logger.error("踢出群聊异常：%s", exc)
            return False

    # ------------------------------------------------------------------ #
    # 审计日志
    # ------------------------------------------------------------------ #
    def _log_violation(
        self,
        ctx: MessageContext,
        item: MediaItem,
        result: ModerationResult,
        violations_num: int,
        actions: List[str],
    ) -> None:
        """把违规内容、发送者 ID、时间、审核返回、当前违规次数写入日志文件。"""
        record: Dict[str, Any] = {
            "time": ctx.time_str,
            "timestamp": ctx.timestamp,
            "group_openid": ctx.group_openid,
            "message_id": ctx.message_id,
            "sender_openid": ctx.user_openid,
            "merged_forward": ctx.merged,
            "violation_type": item.kind,          # text / image
            "violation_source": item.label,       # 合并转发中的路径，如 merged/2/1
            "violation_content": result.preview,  # 违规内容（摘要）
            "audit_result": result.raw,           # 审核接口原始返回
            "hit_scenes": result.hits,
            "violations_num": violations_num,     # 本次处置后的违规次数
            "actions": actions,
        }
        write_violation_record(record)
        # 违规是否额外打到控制台（审计文件始终会写）
        log = logger.warning if settings.log_violation_to_console else logger.info
        log(
            "违规记录已写入：用户=%s 次数=%s 处置=%s",
            ctx.user_openid, violations_num, actions or "无",
        )