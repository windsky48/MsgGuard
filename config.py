# -*- coding: utf-8 -*-
"""
MsgGuard · 全局配置

设计原则：**所有可变项都从环境变量读取**，修改配置不需要改代码；
推荐在项目根目录放一个 .env 文件（见 .env.example），真实环境变量优先级更高。

环境变量优先级：真实环境变量  >  .env 文件  >  代码内默认值。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, TextIO

BASE_DIR = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# 控制台编码加固
# --------------------------------------------------------------------------- #
def force_utf8_stream(stream: Optional[TextIO] = None) -> bool:
    """把标准流强制为 UTF-8，避免中文日志乱码。

    为什么需要：
        Windows 控制台默认代码页是 cp936(GBK)。Python 仅在 stdout **直连控制台**时
        才使用 WindowsConsoleIO（按 UTF-16 写，中文正常）；一旦输出被重定向到
        文件或管道（`python bot.py > out.txt`、`| tee`、nohup、systemd、CI 日志），
        就会退回 ANSI 代码页，中文全部变成乱码。

    放在 config.py（本项目所有模块的导入源头）里，是为了让**模块导入阶段**的
    print 也能正确输出，而不只是 logging 的输出。

    注：Python 3.7+ 才有 reconfigure；用户显式设置了 PYTHONIOENCODING 时予以尊重。
    """
    for target in (sys.stdout, sys.stderr) if stream is None else (stream,):
        if target is None:
            continue
        if os.environ.get("PYTHONIOENCODING"):   # 用户显式指定则不覆盖
            return False
        reconfigure = getattr(target, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, AttributeError):
            continue
    return True


# --------------------------------------------------------------------------- #
# 环境变量读取工具
# --------------------------------------------------------------------------- #
def _load_dotenv(path: Path) -> None:
    """极简 .env 解析器，避免为了读配置再引入一个依赖。

    - 忽略空行与 # 开头的注释
    - 支持 KEY=VALUE / KEY="VALUE" / KEY='VALUE'
    - 使用 setdefault：已存在的真实环境变量不会被 .env 覆盖
    """
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_dotenv(BASE_DIR / ".env")


def _str_env(*names: str, default: str = "") -> str:
    """按优先级依次尝试多个环境变量名，返回第一个非空值。"""
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default


def _int_env(*names: str, default: int = 0) -> int:
    raw = _str_env(*names)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _float_env(*names: str, default: float = 0.0) -> float:
    raw = _str_env(*names)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _bool_env(*names: str, default: bool = False) -> bool:
    raw = _str_env(*names).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on")


def _list_env(name: str, default: List[str]) -> List[str]:
    raw = _str_env(name)
    if not raw:
        return list(default)
    return [item.strip() for item in raw.replace("，", ",").split(",") if item.strip()]


def _resolve_path(raw: str, default_name: str) -> Path:
    """相对路径按项目根目录解析，绝对路径原样使用。"""
    path = Path(raw or default_name).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


# --------------------------------------------------------------------------- #
# 配置实体
# --------------------------------------------------------------------------- #
@dataclass
class Settings:
    # ---------------- QQ 机器人 ----------------
    qq_appid: str = ""
    qq_secret: str = ""
    qq_port: int = 8080
    qq_is_sandbox: bool = False
    # 机器人自身的 user_openid（可选）：用于忽略自己发出的消息，避免自审触发处置
    bot_user_openid: str = ""

    # ---------------- 阿里云内容审核 ----------------
    # 注意：需求里写的是 A_CLOUD_ACCESS_KEY_ID / ALIBABA_CLOUD_ACCESS_KEY_SECRET，
    #      这里同时兼容两种拼写以及标准命名，任一存在即可。
    ak_id: str = ""
    ak_secret: str = ""
    region: str = "cn-beijing"                # 内容审核 API 的接入地域（绿网/内容安全）
    green_biz_type: str = "qq_group_guard"     # 审核业务类型，便于在控制台按业务统计
    scenes: List[str] = field(default_factory=lambda: ["porn", "terror"])
    violation_rate_threshold: float = 0.8      # 置信度阈值：rate >= 该值且 label != normal 即判违规
    green_body_mode: str = "auto"              # auto | tasks | flat（请求体结构，见 moderation.py）
    text_chunk_size: int = 1800                # TextScan 单任务 content 上限约 2000 字，超长自动分片

    # 审核接口调用稳定性
    green_max_retry: int = 1                   # 失败重试次数
    green_retry_delay: float = 1.0             # 重试间隔（秒）
    green_min_interval: float = 0.25           # 两次审核调用之间的最小间隔（简单限流）
    green_max_concurrency: int = 2             # 审核接口最大并发
    green_timeout: float = 10.0                # 单次审核 HTTP 超时（秒）

    # 图片审核：直连 URL 失败时，下载到本地再经 Extension.Uploader 换临时 URL
    image_fallback_upload: bool = True
    image_download_timeout: float = 10.0

    # ---------------- 处置策略 ----------------
    recall_limit_seconds: int = 120            # 官方撤回接口 2 分钟时限
    mute_seconds: int = 86400                  # 首次违规禁言 1 天
    kick_on_repeat: bool = True                # 二次及以上直接踢出
    repeat_threshold: int = 1                  # ViolationsNum >= 该值 -> 踢出

    # ---------------- 白名单 ----------------
    whitelist_enabled: bool = True              # 白名单总开关
    whitelist_file: Path = BASE_DIR / "Whitelist.json"
    # 允许执行「#加白 / #移白 / #清零 / #白名单」指令的管理员 user_openid（逗号分隔）。
    # 留空 = 禁用所有指令，只能手工编辑 Whitelist.json。
    whitelist_admins: List[str] = field(default_factory=list)
    # 加白时是否同时清零该成员的 ViolationsNum
    whitelist_reset_violations: bool = True

    # ---------------- 存储与日志 ----------------
    data_file: Path = BASE_DIR / "ViolationsNum.json"
    log_dir: Path = BASE_DIR / "logs"
    log_level: str = "INFO"
    log_retention_days: int = 30
    log_violation_to_console: bool = True      # 违规同时打一条普通日志，便于实时观察

    # ---------------- 其他 ----------------
    moderation_enabled: bool = True            # 总开关，false 时只记录不处置
    debug: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            # QQ
            qq_appid=_str_env("QQ_APPID", "APPID"),
            qq_secret=_str_env("QQ_SECRET", "SECRET"),
            qq_port=_int_env("QQ_PORT", default=8080),
            qq_is_sandbox=_bool_env("QQ_SANDBOX", default=False),
            bot_user_openid=_str_env("BOT_USER_OPENID"),
            # 阿里云
            ak_id=_str_env(
                "A_CLOUD_ACCESS_KEY_ID",
                "ALIBABA_CLOUD_ACCESS_KEY_ID",
                "ACCESS_KEY_ID",
            ),
            ak_secret=_str_env(
                "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
                "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
                "A_CLOUD_ACCESS_KEY_SECRET",
                "ACCESS_KEY_SECRET",
            ),
            # 注意：这里只决定**内容审核接口**的接入地域，与 Extension.Uploader
            # 上传用的 OSS 地域（固定在上海）无关，两者互不影响。
            region=_str_env("ALIBABA_CLOUD_REGION", "A_CLOUD_REGION", default="cn-beijing"),
            green_biz_type=_str_env("GREEN_BIZ_TYPE", default="qq_group_guard"),
            scenes=_list_env("GREEN_SCENES", ["porn", "terror"]),
            violation_rate_threshold=_float_env("VIOLATION_RATE_THRESHOLD", default=0.8),
            green_body_mode=_str_env("GREEN_BODY_MODE", default="auto").lower(),
            text_chunk_size=_int_env("TEXT_CHUNK_SIZE", default=1800),
            # 稳定性
            green_max_retry=_int_env("GREEN_API_MAX_RETRY", default=1),
            green_retry_delay=_float_env("GREEN_API_RETRY_DELAY", default=1.0),
            green_min_interval=_float_env("GREEN_API_MIN_INTERVAL", default=0.25),
            green_max_concurrency=_int_env("GREEN_API_MAX_CONCURRENCY", default=2),
            green_timeout=_float_env("GREEN_API_TIMEOUT", default=10.0),
            image_fallback_upload=_bool_env("IMAGE_FALLBACK_UPLOAD", default=True),
            image_download_timeout=_float_env("IMAGE_DOWNLOAD_TIMEOUT", default=10.0),
            # 处置
            recall_limit_seconds=_int_env("RECALL_LIMIT_SECONDS", default=120),
            mute_seconds=_int_env("MUTE_SECONDS", default=86400),
            kick_on_repeat=_bool_env("KICK_ON_REPEAT", default=True),
            repeat_threshold=_int_env("REPEAT_THRESHOLD", default=1),
            # 白名单
            whitelist_enabled=_bool_env("WHITELIST_ENABLED", default=True),
            whitelist_file=_resolve_path(_str_env("WHITELIST_FILE"), "Whitelist.json"),
            whitelist_admins=_list_env("WHITELIST_ADMINS", []),
            whitelist_reset_violations=_bool_env("WHITELIST_RESET_VIOLATIONS", default=True),
            # 存储与日志
            data_file=_resolve_path(_str_env("VIOLATIONS_FILE"), "ViolationsNum.json"),
            log_dir=_resolve_path(_str_env("LOG_DIR"), "logs"),
            log_level=_str_env("LOG_LEVEL", default="INFO").upper(),
            log_retention_days=_int_env("LOG_RETENTION_DAYS", default=30),
            log_violation_to_console=_bool_env("LOG_VIOLATION_TO_CONSOLE", default=True),
            # 其他
            moderation_enabled=_bool_env("MODERATION_ENABLED", default=True),
            debug=_bool_env("DEBUG", default=False),
        )

    def validate(self) -> None:
        """启动自检：只打印告警，不直接退出，方便先跑起来再排查。"""
        if not self.qq_appid or not self.qq_secret:
            logger_warning("未配置 QQ_APPID / QQ_SECRET，机器人无法连接 QQ 官方网关。")
        if not self.ak_id or not self.ak_secret:
            logger_warning("未配置 A_CLOUD_ACCESS_KEY_ID / ALIBABA_CLOUD_ACCESS_KEY_SECRET，"
                           "内容审核将不可用（bot 仍会正常收发消息）。")
        if self.green_body_mode not in ("auto", "tasks", "flat"):
            logger_warning(f"GREEN_BODY_MODE={self.green_body_mode} 非法，回退为 auto。")
            self.green_body_mode = "auto"
        if not 0 < self.violation_rate_threshold <= 1:
            logger_warning(f"VIOLATION_RATE_THRESHOLD={self.violation_rate_threshold} 不在 (0,1]，"
                           f"回退为 0.8。")
            self.violation_rate_threshold = 0.8


def logger_warning(message: str) -> None:
    print(f"[配置告警] {message}")


# 尽早锁定控制台编码（必须早于任何 print）
force_utf8_stream()

settings = Settings.from_env()
settings.validate()