# -*- coding: utf-8 -*-
"""
白名单（Whitelist）

作用：让特定成员**完全跳过审核**，避免运营号、官方账号、被误伤的正常用户
      被撤回/禁言/踢出，同时节省审核接口调用额度。

特性
----
- 持久化到 Whitelist.json，格式：{"users": {"<openid>": {"note": "...", "added_at": "..."}}}
- 兼容简单列表格式 ["openid1", "openid2"]，方便手工编辑
- 写入采用「临时文件 + os.replace」原子替换；文件损坏时自动备份并以空名单启动
- 管理员名单来自环境变量 WHITELIST_ADMINS（逗号分隔），**默认为空即禁用所有指令**
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from config import settings
from storage import atomic_write_json

logger = logging.getLogger("msgguard.whitelist")


class WhitelistManager:
    """白名单管理器。"""

    def __init__(self, file_path: Optional[Path] = None) -> None:
        self._path = Path(file_path) if file_path else settings.whitelist_file
        self._users: Dict[str, Dict[str, str]] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #
    def load(self) -> None:
        """启动时调用，从磁盘恢复白名单。"""
        if not self._path.exists():
            logger.info("白名单文件不存在，视为空名单：%s", self._path)
            return

        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            users: Dict[str, Dict[str, str]] = {}

            if isinstance(raw, dict) and isinstance(raw.get("users"), dict):
                for openid, meta in raw["users"].items():
                    meta = meta if isinstance(meta, dict) else {"note": str(meta)}
                    users[str(openid)] = {
                        "note": str(meta.get("note", "")),
                        "added_at": str(meta.get("added_at", "")),
                    }
            elif isinstance(raw, list):
                # 简单列表格式：["openid1", "openid2"]
                for item in raw:
                    users[str(item)] = {"note": "", "added_at": ""}
            elif isinstance(raw, dict):
                # 扁平格式：{"openid1": "备注"}
                for openid, meta in raw.items():
                    users[str(openid)] = {
                        "note": "" if meta is None else str(meta),
                        "added_at": "",
                    }
            else:
                raise ValueError("不支持的白名单文件格式")

            self._users = users
            logger.info("已加载白名单 %d 人 -> %s", len(self._users), self._path)
        except Exception as exc:
            backup = self._path.with_suffix(self._path.suffix + ".broken")
            try:
                os.replace(self._path, backup)
            except OSError:
                pass
            logger.error("白名单文件解析失败（已备份到 %s），以空名单启动：%s", backup, exc)

    def is_exempt(self, user_openid: str) -> bool:
        """该成员是否豁免审核。"""
        if not settings.whitelist_enabled:
            return False
        return str(user_openid) in self._users

    def entries(self) -> Dict[str, Dict[str, str]]:
        return dict(self._users)

    def describe(self) -> str:
        """生成给管理员看的白名单摘要（可直接回复到聊天）。"""
        if not self._users:
            return "当前白名单为空。"
        lines = [f"白名单（共 {len(self._users)} 人）："]
        for index, (openid, meta) in enumerate(self._users.items(), start=1):
            note = meta.get("note") or "无备注"
            added = meta.get("added_at") or "未知"
            lines.append(f"{index}. {openid}｜备注：{note}｜加入：{added}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 管理员判定
    # ------------------------------------------------------------------ #
    @staticmethod
    def is_admin(user_openid: str) -> bool:
        """是否为白名单管理员。

        默认关闭：WHITELIST_ADMINS 未配置时任何人都不能执行加白/移白指令，
        避免普通成员把自己加进白名单绕过审核。
        """
        if not settings.whitelist_admins:
            return False
        return str(user_openid) in settings.whitelist_admins

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #
    async def add(self, user_openid: str, note: str = "") -> bool:
        """加入白名单，返回 True 表示本次新增（原本已在白名单里则返回 False）。"""
        async with self._lock:
            if str(user_openid) in self._users:
                return False
            self._users[str(user_openid)] = {
                "note": note or "",
                "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            self._flush_locked()
            logger.info("已加入白名单：%s（备注：%s）", user_openid, note or "无")
            return True

    async def remove(self, user_openid: str) -> bool:
        """移出白名单，返回 True 表示确实移除了。"""
        async with self._lock:
            if str(user_openid) not in self._users:
                return False
            self._users.pop(str(user_openid), None)
            self._flush_locked()
            logger.info("已移出白名单：%s", user_openid)
            return True

    async def flush(self) -> None:
        """停机前主动落盘。"""
        async with self._lock:
            self._flush_locked()

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _flush_locked(self) -> None:
        """原子写盘（复用 storage 的公共实现，含 Windows 文件占用重试）。"""
        atomic_write_json(self._path, {"users": self._users})