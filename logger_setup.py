# -*- coding: utf-8 -*-
"""日志初始化：控制台 + 按天切分的文件日志，违规事件额外写 JSONL 审计文件。"""

from __future__ import annotations

import json
import logging
import sys
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any, Dict

from config import force_utf8_stream, settings


def setup_logging() -> logging.Logger:
    """配置根 logger，返回本项目统一使用的 logger（名称 msgguard）。"""
    log_dir: Path = settings.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    # 兜底再锁一次（config 在导入时已锁过，这里覆盖重复调用 setup_logging 的情况）
    force_utf8_stream()

    root = logging.getLogger()
    root.setLevel(getattr(logging, settings.log_level, logging.INFO))
    # 防止重复调用导致 handler 叠加
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)-18s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台：显式用 stdout，这样 print() 与日志走同一个流，编码策略一致
    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    # 业务日志：logs/msgguard_YYYY-MM-DD.log，保留 N 天
    file_handler = TimedRotatingFileHandler(
        filename=str(log_dir / "msgguard.log"),
        when="midnight",
        interval=1,
        backupCount=settings.log_retention_days,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # 第三方库降噪
    for noisy in ("aiohttp.access", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logger = logging.getLogger("msgguard")
    logger.info("日志系统就绪，目录：%s", log_dir)
    return logger


def violation_log_path() -> Path:
    """违规审计文件：logs/violations_YYYY-MM-DD.jsonl（按天切分，结构化便于检索）。"""
    stamp = time.strftime("%Y-%m-%d")
    return settings.log_dir / f"violations_{stamp}.jsonl"


def write_violation_record(record: Dict[str, Any]) -> None:
    """把一条违规记录写入审计文件；写失败只记 error 日志，绝不影响主流程。"""
    path = violation_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # pragma: no cover - 防御性代码
        logging.getLogger("msgguard.audit").error("写违规审计文件失败：%s", exc)