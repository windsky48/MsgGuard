# -*- coding: utf-8 -*-
"""
QQ 官方机器人 OpenAPI 封装

用途：botpy 对不同版本暴露的方法名不一致，本模块做「能力探测 + HTTP 兜底」：

| 能力     | 优先使用                        | 兜底 HTTP 接口                                        |
|----------|---------------------------------|-------------------------------------------------------|
| 撤回消息 | message.recall()（botpy 原生） | DELETE /v2/groups/{gid}/messages/{msg_id}             |
| 发消息   | message.reply()（botpy 原生）  | POST   /v2/groups/{gid}/messages                      |
| 禁言     | 无稳定 SDK 方法                 | POST   /v2/groups/{gid}/members/{openid}/mute          |
| 踢出     | 无稳定 SDK 方法                 | DELETE /v2/groups/{gid}/members/{openid}              |

鉴权头：Authorization: QQBot {access_token}（个别环境仍是 Bot {token}，401 时自动重试）。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from typing import Any, Dict, Optional, Tuple

import aiohttp

from config import settings

logger = logging.getLogger("msgguard.openapi")

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
BASE_URL_PROD = "https://api.sgroup.qq.com"
BASE_URL_SANDBOX = "https://sandbox.api.sgroup.qq.com"


class QQOpenAPI:
    """带 token 缓存与自动刷新能力的 QQ 开放接口客户端。"""

    def __init__(self, client=None, timeout: float = 10.0) -> None:
        self._client = client                # botpy.Client，用于复用其 token
        self._timeout = timeout
        self._token: str = ""
        self._token_expire_at: float = 0.0
        self._lock = asyncio.Lock()
        self._session: Optional[aiohttp.ClientSession] = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    @property
    def base_url(self) -> str:
        return BASE_URL_SANDBOX if settings.qq_is_sandbox else BASE_URL_PROD

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout)
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------------ #
    # AccessToken
    # ------------------------------------------------------------------ #
    async def get_access_token(self, force: bool = False) -> str:
        """获取 access_token：优先复用 botpy 内部 token，否则自行申请。"""
        if not force and self._token and time.time() < self._token_expire_at - 60:
            return self._token

        async with self._lock:
            if not force and self._token and time.time() < self._token_expire_at - 60:
                return self._token

            token = await self._token_from_botpy()
            if token:
                self._token = token
                self._token_expire_at = time.time() + 600   # botpy 内部会自行续期，这里短缓存即可
                return token

            if not settings.qq_appid or not settings.qq_secret:
                raise RuntimeError("未配置 QQ_APPID / QQ_SECRET，无法获取 access_token")

            session = await self._ensure_session()
            async with session.post(
                TOKEN_URL,
                json={"appId": settings.qq_appid, "clientSecret": settings.qq_secret},
            ) as resp:
                text = await resp.text()
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"获取 access_token 返回非 JSON：{text[:200]}") from exc

            if not data.get("access_token"):
                raise RuntimeError(f"获取 access_token 失败：{data}")
            self._token = data["access_token"]
            self._token_expire_at = time.time() + float(data.get("expires_in", 7200))
            logger.info("已获取 QQ access_token，有效期 %s 秒", data.get("expires_in", 7200))
            return self._token

    async def _token_from_botpy(self) -> str:
        """尝试复用 botpy 内部的 token，避免重复申请。"""
        manager = getattr(self._client, "token_manager", None)
        if manager is None:
            return ""
        getter = getattr(manager, "get_access_token", None)
        if getter is None:
            return ""
        try:
            value = getter()
            if inspect.isawaitable(value):
                value = await value
        except Exception as exc:
            logger.debug("从 botpy 获取 token 失败：%s", exc)
            return ""
        if isinstance(value, dict):
            return str(value.get("access_token", ""))
        return str(value or "")

    # ------------------------------------------------------------------ #
    # 通用请求
    # ------------------------------------------------------------------ #
    async def request(
        self, method: str, path: str, payload: Optional[Dict[str, Any]] = None
    ) -> Tuple[int, Any]:
        """发送一次 OpenAPI 请求，返回 (http_status, 解析后的 body)。

        遇到 401 会刷新 token 并用另一种鉴权前缀重试一次。
        """
        last_error: Optional[BaseException] = None

        for attempt in range(2):
            token = await self.get_access_token(force=attempt > 0)
            # 官方新版使用 QQBot 前缀，旧版是 Bot；两种都试，兼容不同网关版本
            prefix = "QQBot" if attempt == 0 else "Bot"
            headers = {
                "Authorization": f"{prefix} {token}",
                "Content-Type": "application/json",
            }
            try:
                session = await self._ensure_session()
                async with session.request(
                    method, self.base_url + path, headers=headers, json=payload
                ) as resp:
                    text = await resp.text()
                    try:
                        body = json.loads(text) if text else {}
                    except json.JSONDecodeError:
                        body = {"raw": text[:500]}
                    return resp.status, body
            except asyncio.TimeoutError as exc:
                last_error = RuntimeError(f"接口 {method} {path} 超时：{exc}")
            except Exception as exc:
                last_error = exc
            logger.warning("OpenAPI 请求失败(%s %s，第%s次)：%s", method, path, attempt + 1, last_error)
            await asyncio.sleep(0.5)

        raise RuntimeError(f"OpenAPI 请求失败：{method} {path} -> {last_error}")

    # ------------------------------------------------------------------ #
    # 具体能力
    # ------------------------------------------------------------------ #
    async def recall_message(self, message) -> bool:
        """撤回消息。优先 botpy 原生方法，失败/不存在则走 OpenAPI。"""
        native = getattr(message, "recall", None)
        if callable(native):
            try:
                await native()
                return True
            except Exception as exc:
                logger.warning("botpy 原生撤回失败，回退 OpenAPI：%s", exc)

        try:
            status, body = await self.request(
                "DELETE",
                f"/v2/groups/{message.group_openid}/messages/{message.id}",
            )
            if status in (200, 204):
                return True
            logger.error("撤回消息失败：HTTP %s %s", status, body)
        except Exception as exc:
            logger.error("撤回消息异常：%s", exc)
        return False

    async def send_group_message(
        self, group_openid: str, content: str, msg_type: int = 0, msg_seq: int = 1
    ) -> bool:
        """OpenAPI 方式发送群消息（botpy reply 失败时的兜底）。"""
        try:
            status, body = await self.request(
                "POST",
                f"/v2/groups/{group_openid}/messages",
                {"content": content, "msg_type": msg_type, "msg_seq": msg_seq},
            )
            if status == 200:
                return True
            logger.error("OpenAPI 发消息失败：HTTP %s %s", status, body)
        except Exception as exc:
            logger.error("OpenAPI 发消息异常：%s", exc)
        return False

    async def send_direct_message(self, user_openid: str, content: str) -> bool:
        """OpenAPI 方式发送单聊（C2C）消息，用于管理指令回复的兜底。

        官方接口：POST /v2/users/{user_openid}/messages
        """
        try:
            status, body = await self.request(
                "POST",
                f"/v2/users/{user_openid}/messages",
                {"content": content, "msg_type": 0, "msg_seq": 1},
            )
            if status == 200:
                return True
            logger.error("OpenAPI 发单聊消息失败：HTTP %s %s", status, body)
        except Exception as exc:
            logger.error("OpenAPI 发单聊消息异常：%s", exc)
        return False

    async def mute_member(self, group_openid: str, user_openid: str, duration: int) -> bool:
        """群成员禁言，duration 单位为**秒**（86400 = 1 天）。

        官方接口：POST /v2/groups/{group_openid}/members/{user_openid}/mute
        请求体：{"duration_seconds": duration}
        """
        try:
            status, body = await self.request(
                "POST",
                f"/v2/groups/{group_openid}/members/{user_openid}/mute",
                {"duration_seconds": int(duration)},
            )
            if status in (200, 204):
                logger.info("禁言成功：%s，时长 %s 秒", user_openid, duration)
                return True
            logger.error("禁言失败：HTTP %s %s", status, body)
        except Exception as exc:
            logger.error("禁言异常：%s", exc)
        return False

    async def kick_member(self, group_openid: str, user_openid: str) -> bool:
        """把成员移出群聊。

        官方接口：DELETE /v2/groups/{group_openid}/members/{user_openid}
        """
        try:
            status, body = await self.request(
                "DELETE", f"/v2/groups/{group_openid}/members/{user_openid}"
            )
            if status in (200, 204):
                logger.info("踢出群聊成功：%s", user_openid)
                return True
            logger.error("踢出群聊失败：HTTP %s %s", status, body)
        except Exception as exc:
            logger.error("踢出群聊异常：%s", exc)
        return False