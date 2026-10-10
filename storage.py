# -*- coding: utf-8 -*-
"""
违规次数持久化：ViolationsNum.json

- 结构：{"<user_openid>": 违规次数, ...}
- 机器人重启后不清零
- 写入采用「临时文件 + os.replace」原子替换，避免进程被杀导致文件损坏
- asyncio.Lock 保证同一进程内并发安全
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict

from config import settings

logger = logging.getLogger("msgguard.storage")


def atomic_write_json(path: Path, payload: Dict[str, Any], retries: int = 5) -> bool:
    """原子写 JSON：先写「临时文件」，再用 os.replace 覆盖目标。

    为什么不用直接 open(path, "w")：
        进程若在写到一半时被杀掉，文件会变成残缺 JSON，下次启动就无法恢复。

    为什么还要重试：
        Windows 上目标文件常被杀毒软件 / 索引服务 / 编辑器短暂占用，
        os.replace 会抛 WinError 5(拒绝访问)。这里做有限次退避重试，
        避免一次瞬时锁定就让违规记录没落盘。
    """
    path = Path(path)
    tmp_path = path.with_name(path.name + ".tmp")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.error("写临时文件失败(%s)：%s", tmp_path, exc)
        _silent_unlink(tmp_path)
        return False

    for attempt in range(retries):
        try:
            os.replace(tmp_path, path)   # Windows / POSIX 均为原子替换
            return True
        except PermissionError:
            # 文件被其它进程短暂占用，退避后重试
            if attempt < retries - 1:
                time.sleep(0.1 * (attempt + 1))
        except OSError as exc:
            logger.error("替换目标文件失败(%s)：%s", path, exc)
            break

    logger.error("写文件失败（已重试 %d 次）：%s", retries, path)
    _silent_unlink(tmp_path)
    return False


def _silent_unlink(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


class ViolationStore:
    """ViolationsNum 字典：key = user_openid，value = 违规次数。"""

    def __init__(self, file_path: Path | str | None = None) -> None:
        self._path = Path(file_path) if file_path else settings.data_file
        self._data: Dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._loaded = False

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #
    def load(self) -> None:
        """启动时调用一次；从磁盘恢复历史违规次数。"""
        if not self._path.exists():
            logger.info("违规记录文件不存在，将在首次处置时创建：%s", self._path)
            self._loaded = True
            return

        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = {str(k): int(v) for k, v in raw.items()}
                logger.info("已加载违规记录 %d 条 -> %s", len(self._data), self._path)
            else:
                raise ValueError("根节点不是 JSON 对象")
        except Exception as exc:
            # 文件损坏时不静默丢弃，先备份再从空字典继续，保证机器人可用
            backup = self._path.with_suffix(self._path.suffix + ".broken")
            try:
                os.replace(self._path, backup)
            except OSError:
                pass
            logger.error("违规记录文件解析失败（已备份到 %s），将以空记录启动：%s", backup, exc)

        self._loaded = True

    def get(self, user_openid: str) -> int:
        """取当前违规次数，未记录过返回 0。"""
        return int(self._data.get(user_openid, 0))

    def snapshot(self) -> Dict[str, int]:
        """返回一份浅拷贝，供管理命令/调试查看。"""
        return dict(self._data)

    def __len__(self) -> int:
        return len(self._data)

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #
    async def increment(self, user_openid: str) -> int:
        """违规次数 +1 并落盘，返回自增后的值。"""
        async with self._lock:
            new_value = self.get(user_openid) + 1
            self._data[user_openid] = new_value
            self._flush_locked()
            logger.info("违规次数自增：%s -> %d", user_openid, new_value)
            return new_value

    async def reset(self, user_openid: str) -> None:
        """人工干预时清零某人的违规次数。"""
        async with self._lock:
            self._data.pop(user_openid, None)
            self._flush_locked()

    async def flush(self) -> None:
        """停机前主动落盘。"""
        async with self._lock:
            self._flush_locked()

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _flush_locked(self) -> None:
        """同步写盘（原子替换 + 重试）。失败只记日志，不向上抛，避免影响处置流程。"""
        atomic_write_json(self._path, self._data)