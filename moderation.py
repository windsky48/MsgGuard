# -*- coding: utf-8 -*-
"""
审核核心：阿里云内容审核（文本 + 图片）

技术要点
--------
1. 鉴权：AccessKey ID + AccessKey Secret，通过 `aliyunsdkcore.client.AcsClient` 初始化，
   **不是** Bearer Token 方式。
2. 请求类：文本 `TextScanRequest`，图片 `ImageSyncScanRequest`（包路径
   aliyunsdkgreen.request.v20180509），请求体为 JSON。
3. 请求体：{"tasks": [{"dataId": ..., "content": ... | "url": ..., "scenes": [...]}]}
   另提供一个兼容模式（GREEN_BODY_MODE=flat）使用不带 tasks 包装的扁平请求体，
   若某个 SDK/接口版本不接受 tasks 结构会自动回退并记住可用模式。
4. 场景：porn（色情）、terror（暴恐涉政），文本与图片一致。
5. 判定：label != "normal" 且 rate >= 阈值（默认 0.8）即视为违规。
   注意阿里云不同版本 rate 可能是 0~1 小数或 0~100 百分制，这里统一归一化到 0~1。
6. 稳定性：所有调用同步 SDK（do_action_with_exception）放到线程池执行，
   外层加「限流 + 失败重试 1 次」，任何异常都不会抛到事件处理主流程之外。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from config import settings
from uploader import ImageUploader

logger = logging.getLogger("msgguard.moderation")

# 单个任务的 content 上限（官方约 2000 字符），留一点余量
_TEXT_CHUNK_SIZE = 1800


@dataclass
class ModerationResult:
    """一次审核调用的结果。"""

    source: str = ""                       # text / image
    preview: str = ""                      # 被审核内容的摘要（写日志用）
    is_violation: bool = False
    hits: List[Dict[str, Any]] = field(default_factory=list)   # 命中项：scene/label/rate
    reasons: List[str] = field(default_factory=list)           # 人类可读原因
    raw: Dict[str, Any] = field(default_factory=dict)         # 接口原始返回
    data_id: str = ""
    error: str = ""                        # 非空表示调用失败（此时 is_violation 恒为 False）

    def to_log_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "preview": self.preview,
            "is_violation": self.is_violation,
            "hits": self.hits,
            "reasons": self.reasons,
            "data_id": self.data_id,
            "error": self.error,
            "audit_result": self.raw,
        }


class AsyncRateLimiter:
    """极简异步限流器：并发信号量 + 调用最小间隔。

    用于避免短时间大量群消息同时打到审核接口而被限流/超时。
    """

    def __init__(self, min_interval: float = 0.25, max_concurrency: int = 2) -> None:
        self._min_interval = max(0.0, min_interval)
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._lock = asyncio.Lock()
        self._last_call_at = 0.0

    @asynccontextmanager
    async def slot(self):
        await self._semaphore.acquire()
        try:
            async with self._lock:
                wait = self._last_call_at + self._min_interval - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_call_at = time.monotonic()
            yield
        finally:
            self._semaphore.release()


class ContentModerator:
    """阿里云内容审核客户端封装。"""

    def __init__(self) -> None:
        self._client = None
        self._limiter = AsyncRateLimiter(
            min_interval=settings.green_min_interval,
            max_concurrency=settings.green_max_concurrency,
        )
        self._uploader = ImageUploader()
        # 请求体模式：auto = 先试 tasks，再试 flat，成功后记住
        self._body_mode = settings.green_body_mode
        self._disabled_reason = ""

    # ------------------------------------------------------------------ #
    # 客户端初始化
    # ------------------------------------------------------------------ #
    def _ensure_client(self):
        """懒加载 AcsClient（AccessKey 鉴权）。"""
        if self._client is not None:
            return self._client
        if self._disabled_reason:
            raise RuntimeError(self._disabled_reason)

        if not settings.ak_id or not settings.ak_secret:
            self._disabled_reason = (
                "未配置 AccessKey（环境变量 A_CLOUD_ACCESS_KEY_ID / ALIBABA_CLOUD_ACCESS_KEY_SECRET）"
            )
            raise RuntimeError(self._disabled_reason)

        try:
            from aliyunsdkcore.client import AcsClient  # type: ignore
        except ImportError as exc:
            self._disabled_reason = f"未安装 aliyun-python-sdk-core-v3：{exc}"
            raise RuntimeError(self._disabled_reason) from exc

        try:
            # AccessKey ID + AccessKey Secret + region，三者缺一不可
            self._client = AcsClient(settings.ak_id, settings.ak_secret, settings.region)
            logger.info(
                "阿里云内容审核客户端初始化成功：region=%s scenes=%s threshold=%.2f",
                settings.region, ",".join(settings.scenes), settings.violation_rate_threshold,
            )
        except Exception as exc:
            self._disabled_reason = f"AcsClient 初始化失败：{exc}"
            raise RuntimeError(self._disabled_reason) from exc
        return self._client

    @property
    def available(self) -> bool:
        """是否能正常调用审核接口（供启动自检使用）。"""
        try:
            self._ensure_client()
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # 请求体拼装
    # ------------------------------------------------------------------ #
    def _build_body(self, mode: str, tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
        """mode=tasks：{"tasks": [...]}；mode=flat：把第一个任务展开到顶层。"""
        if mode == "flat":
            body = dict(tasks[0])
        else:
            body = {"tasks": tasks}
        if settings.green_biz_type:
            body.setdefault("bizType", settings.green_biz_type)
        return body

    def _modes(self) -> List[str]:
        """本次调用需要尝试的请求体模式列表（失败会顺延到下一个）。"""
        if self._body_mode == "tasks":
            return ["tasks"]
        if self._body_mode == "flat":
            return ["flat"]
        return ["tasks", "flat"]

    # ------------------------------------------------------------------ #
    # 底层调用：限流 + 重试 + 模式回退
    # ------------------------------------------------------------------ #
    def _do_request(self, request_cls, body: Dict[str, Any]) -> Dict[str, Any]:
        """同步执行一次 SDK 调用（会在线程池里跑，不阻塞事件循环）。"""
        request = request_cls()
        request.set_accept_format("json")
        request.set_content_type("application/json")
        request.set_Method("POST")
        request.set_RequestBody(json.dumps(body, ensure_ascii=False))

        response = self._client.do_action_with_exception(request)
        if isinstance(response, (bytes, bytearray)):
            response = response.decode("utf-8", errors="replace")
        if isinstance(response, str):
            return json.loads(response)
        return dict(response or {})

    async def _invoke(self, request_cls, tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
        """执行审核请求，返回原始 JSON。

        - 每个 body 模式最多尝试 settings.green_max_retry + 1 次
        - 全部失败则抛异常，由调用方转成 error 字段（不影响 bot 主流程）
        """
        self._ensure_client()   # 保证 AcsClient 已就绪（缺 AccessKey 时给出清晰错误）
        last_error: Optional[BaseException] = None

        for mode in self._modes():
            body = self._build_body(mode, tasks)
            for attempt in range(settings.green_max_retry + 1):
                try:
                    async with self._limiter.slot():
                        raw = await asyncio.wait_for(
                            asyncio.to_thread(self._do_request, request_cls, body),
                            timeout=settings.green_timeout,
                        )
                    code = int(raw.get("code", 0) or 0)
                    if code != 200:
                        # 业务级错误（参数非法 / 无权限等）：换 body 模式不如直接重试一次
                        raise RuntimeError(
                            f"审核接口返回 code={code} message={raw.get('message')} data={raw.get('data')}"
                        )
                    self._body_mode = mode   # 记住成功的模式，下次少一次试错
                    return raw
                except Exception as exc:  # 超时、网络异常、业务错误统一处理
                    last_error = exc
                    logger.warning(
                        "内容审核调用失败(mode=%s, 第%s次)：%s", mode, attempt + 1, exc
                    )
                    if attempt < settings.green_max_retry:
                        await asyncio.sleep(settings.green_retry_delay)

        raise RuntimeError(f"内容审核调用失败：{last_error}")

    # ------------------------------------------------------------------ #
    # 结果解析
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize_rate(rate: Any) -> float:
        """把 rate 归一化到 0~1。

        阿里云部分版本返回百分制（如 99.9），部分返回小数（如 0.999），
        直接与阈值 0.8 比较会误判，这里统一处理。
        """
        try:
            value = float(rate)
        except (TypeError, ValueError):
            return 0.0
        if value < 0:
            return 0.0
        return value / 100.0 if value > 1.0 else value

    def _parse(self, raw: Dict[str, Any]) -> Tuple[bool, List[Dict[str, Any]], List[str]]:
        """按规则判定：label != normal 且 rate >= 阈值。"""
        hits: List[Dict[str, Any]] = []
        reasons: List[str] = []
        for item in raw.get("results") or []:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label", "")).strip()
            scene = str(item.get("scene", "")).strip()
            rate = self._normalize_rate(item.get("rate"))
            if not label or label.lower() == "normal":
                continue
            if rate >= settings.violation_rate_threshold:
                hits.append({"scene": scene, "label": label, "rate": round(rate, 4)})
                reasons.append(f"{scene or 'unknown'}->{label}(置信度{rate:.2f})")
        return bool(hits), hits, reasons

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    async def review_text(self, text: str) -> ModerationResult:
        """文本审核；超过单次长度上限时自动分片，任一片段违规即整体违规。"""
        content = (text or "").strip()
        if not content:
            return ModerationResult(source="text", preview="")

        data_id = uuid.uuid4().hex
        try:
            from aliyunsdkgreen.request.v20180509.TextScanRequest import (  # type: ignore
                TextScanRequest,
            )
        except ImportError as exc:
            logger.error("未安装 aliyun-python-sdk-green：%s", exc)
            return ModerationResult(source="text", preview=_preview(content), error=str(exc))

        # 分片
        size = max(200, settings.text_chunk_size or _TEXT_CHUNK_SIZE)
        chunks = [content[i:i + size] for i in range(0, len(content), size)]

        all_hits: List[Dict[str, Any]] = []
        all_reasons: List[str] = []
        raw_acc: Dict[str, Any] = {"code": 200, "results": []}
        errors: List[str] = []

        for index, chunk in enumerate(chunks):
            task = {
                "dataId": f"{data_id}_{index}",
                "content": chunk,
                "scenes": list(settings.scenes),
            }
            try:
                raw = await self._invoke(TextScanRequest, [task])
            except Exception as exc:
                # 单片失败不影响其它片段；全部片段失败才判定为审核不可用
                errors.append(str(exc))
                continue

            _, hits, reasons = self._parse(raw)
            all_hits.extend(hits)
            all_reasons.extend(reasons)
            raw_acc["results"].extend(raw.get("results") or [])

        # 全部片段都失败 -> 视为审核不可用（调用方会跳过该条内容）；
        # 仅部分失败时保留已有结论，不阻断处置。
        error_text = "；".join(errors[:3]) if len(errors) == len(chunks) else ""
        if errors and not error_text:
            logger.warning("部分文本分片审核失败（共 %d 片，成功 %d 片）", len(chunks), len(chunks) - len(errors))

        return ModerationResult(
            source="text",
            preview=_preview(content),
            is_violation=bool(all_hits),
            hits=all_hits,
            reasons=all_reasons,
            raw=raw_acc,
            data_id=data_id,
            error=error_text,
        )

    async def review_image_url(self, url: str) -> ModerationResult:
        """图片审核（公网 URL 直送）。

        若阿里云拉取该链接失败（QQ CDN 常见防盗链），且开启了兜底开关，
        则自动「下载到本地 -> Extension.Uploader 换临时 URL -> 再次送审」。
        """
        if not url:
            return ModerationResult(source="image", preview="")

        data_id = uuid.uuid4().hex
        try:
            from aliyunsdkgreen.request.v20180509.ImageSyncScanRequest import (  # type: ignore
                ImageSyncScanRequest,
            )
        except ImportError as exc:
            logger.error("未安装 aliyun-python-sdk-green：%s", exc)
            return ModerationResult(source="image", preview=_preview(url), error=str(exc))

        result = await self._review_image_once(ImageSyncScanRequest, url, data_id)
        if not result.error or not settings.image_fallback_upload:
            return result

        # ---- 兜底：下载 + 上传换临时 URL 再审 ----
        logger.info("图片直连审核未成功，尝试「下载 -> Extension.Uploader 上传」兜底：%s", url)
        local_path = ImageUploader.download_to_temp(
            url, suffix=".jpg", timeout=settings.image_download_timeout
        )
        if not local_path:
            return result

        try:
            temp_url = self._uploader.upload(local_path)
            if temp_url:
                return await self._review_image_once(ImageSyncScanRequest, temp_url, uuid.uuid4().hex)
            return result
        finally:
            try:
                from pathlib import Path
                Path(local_path).unlink(missing_ok=True)
            except OSError:
                pass

    async def _review_image_once(self, request_cls, url: str, data_id: str) -> ModerationResult:
        task = {
            "dataId": data_id,
            "url": url,
            "scenes": list(settings.scenes),
        }
        try:
            self._ensure_client()   # 提前初始化，缺配置时给出清晰错误
            raw = await self._invoke(request_cls, [task])
        except Exception as exc:
            logger.error("图片审核失败：%s", exc)
            return ModerationResult(source="image", preview=_preview(url), error=str(exc))

        is_violation, hits, reasons = self._parse(raw)
        return ModerationResult(
            source="image",
            preview=_preview(url),
            is_violation=is_violation,
            hits=hits,
            reasons=reasons,
            raw=raw,
            data_id=data_id,
        )

    async def review(self, kind: str, value: str) -> ModerationResult:
        """统一入口：kind = 'text' 或 'image'。"""
        try:
            self._ensure_client()
        except Exception as exc:
            return ModerationResult(source=kind, preview=_preview(value), error=str(exc))

        if kind == "text":
            return await self.review_text(value)
        return await self.review_image_url(value)

    async def review_image_file(self, file_path: str) -> ModerationResult:
        """本地图片审核：先经 Extension.Uploader 上传换临时 URL。"""
        temp_url = self._uploader.upload(file_path)
        if not temp_url:
            return ModerationResult(source="image", preview=file_path, error="Extension.Uploader 上传失败")
        return await self.review_image_url(temp_url)


def _preview(text: str, limit: int = 300) -> str:
    """日志/审计用的内容摘要，避免把超长文本整篇写进日志。"""
    text = (text or "").replace("\r", " ").replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + f"...(共{len(text)}字)"