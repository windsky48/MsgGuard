# -*- coding: utf-8 -*-
"""
Extension.Uploader 适配层

背景：阿里云内容审核的**图片审核接口需要一个可被公网访问的图片 URL**，
      而 QQ 机器人收到的图片往往是 CDN 链接或本地临时文件。

官方提供的扩展工具类（下载地址见 README / requirements 说明）：
    https://aligreen-shanghai-share.oss-cn-shanghai.aliyuncs.com/aliyun-python-sdk-green-extension.zip
    解压后得到 Extension/Uploader.py，其中类 Uploader 提供：
        auto_file_upload(file_path: str) -> str      # 上传本地文件，返回临时 URL
        auto_upload(image_url: str) -> str            # 转存公网图片，返回临时 URL

本模块只做三件事：
    1. 延迟导入（缺少扩展包不影响文本审核与机器人主流程）
    2. 统一入口 upload(local_path) / upload_from_url(url)
    3. 异常兜底 + 日志
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger("msgguard.uploader")


def _is_http_url(value: str) -> bool:
    try:
        scheme = urlparse(value).scheme.lower()
    except (ValueError, AttributeError):
        return False
    return scheme in ("http", "https")


class ImageUploader:
    """Extension.Uploader 的安全封装。"""

    def __init__(self, uploader_instance=None) -> None:
        self._uploader = uploader_instance
        self._import_failed = False

    # ------------------------------------------------------------------ #
    # 内部：懒加载 Uploader 实例
    # ------------------------------------------------------------------ #
    def _ensure_uploader(self):
        if self._uploader is not None:
            return self._uploader
        if self._import_failed:
            return None

        try:
            from Extension.Uploader import Uploader  # type: ignore
        except ImportError as exc:
            self._import_failed = True
            logger.error(
                "未找到 Extension.Uploader（%s）。请下载并解压官方扩展包到项目根目录："
                "https://aligreen-shanghai-share.oss-cn-shanghai.aliyuncs.com/"
                "aliyun-python-sdk-green-extension.zip；图片将只支持直接审核公网 URL。", exc
            )
            return None

        try:
            self._uploader = Uploader()
            logger.info("Extension.Uploader 初始化成功。")
        except Exception as exc:
            self._import_failed = True
            logger.error("Extension.Uploader 初始化失败，图片转存能力不可用：%s", exc)
            self._uploader = None
        return self._uploader

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def upload(self, file_path: str | Path) -> Optional[str]:
        """上传**本地文件**，返回可公网访问的临时 URL；失败返回 None。"""
        uploader = self._ensure_uploader()
        if uploader is None:
            return None

        path = Path(file_path)
        if not path.is_file():
            logger.error("待上传图片不存在：%s", path)
            return None

        try:
            url = uploader.auto_file_upload(str(path))
        except Exception as exc:
            logger.error("Extension.Uploader 上传本地图片失败(%s)：%s", path.name, exc)
            return None

        if not url or not _is_http_url(str(url)):
            logger.error("Extension.Uploader 返回的地址非法：%r", url)
            return None

        logger.info("图片上传成功：%s -> %s", path.name, url)
        return str(url)

    def upload_from_url(self, url: str) -> Optional[str]:
        """把**公网图片**转存到 OSS，返回临时 URL；失败返回 None。"""
        uploader = self._ensure_uploader()
        if uploader is None:
            return None
        try:
            result = uploader.auto_upload(url)
        except Exception as exc:
            logger.error("Extension.Uploader 转存公网图片失败：%s", exc)
            return None

        if not result or not _is_http_url(str(result)):
            logger.error("Extension.Uploader 返回的地址非法：%r", result)
            return None
        return str(result)

    @staticmethod
    def download_to_temp(url: str, suffix: str = ".jpg", timeout: float = 10.0) -> Optional[str]:
        """把 CDN 图片下载到本地临时文件，作为「直连审核失败后的兜底」。

        使用 urllib 标准库，避免为了一次下载再引第三方依赖。
        """
        import urllib.request

        suffix = suffix if suffix.startswith(".") else f".{suffix}"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, prefix="msgguard_")
        tmp.close()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 MsgGuard"})
            with urllib.request.urlopen(req, timeout=timeout) as resp, \
                    open(tmp.name, "wb") as out:
                out.write(resp.read())
            return tmp.name
        except Exception as exc:
            logger.error("下载图片失败(%s)：%s", url, exc)
            try:
                Path(tmp.name).unlink(missing_ok=True)
            except OSError:
                pass
            return None