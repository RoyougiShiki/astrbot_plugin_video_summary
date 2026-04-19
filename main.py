"""AstrBot 视频总结插件

支持无字幕视频的自动总结：
  1. FFmpeg 提取音频
  2. Cloudflare Workers AI Whisper 语音转文字
  3. LLM 总结转录文本

支持来源:
  - 本地视频/音频文件
  - Bilibili 视频链接 / BV号
  - QQ 小程序卡片消息（B站分享）

订阅功能:
  - /vsub <UP主UID或链接>      订阅 UP 主新视频通知
  - /vsub_list                  查看订阅列表
  - /vsub_del <UID>             取消订阅
  - /videosum <BV号>            总结视频（收到通知后手动触发）

命令:
  /videosum <视频路径、URL 或 BV号>   总结视频内容
  /videosum help                        显示帮助
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register

from .bili_client import BiliClient
from .data_manager import SubscriptionDataManager
from .listener import DynamicListener

# ─── 常量 ────────────────────────────────────────────────

_CF_WHISPER_URL_TEMPLATE = (
    "https://api.cloudflare.com/client/v4/accounts/{account_id}"
    "/ai/run/@cf/openai/whisper"
)

_VIDEO_EXTS = {".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".3gp", ".ts"}
_AUDIO_EXTS = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".opus", ".amr"}

_TEMP_MAX_AGE = 86400


def _find_ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    common_paths = [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
    ]
    for p in common_paths:
        if os.path.isfile(p):
            return p
    return ""


def _cleanup_old_temp_dirs(base_temp_dir: str | None = None) -> None:
    if base_temp_dir is None:
        base_temp_dir = tempfile.gettempdir()
    now = time.time()
    try:
        for name in os.listdir(base_temp_dir):
            if name.startswith("videosum_"):
                full_path = os.path.join(base_temp_dir, name)
                try:
                    mtime = os.path.getmtime(full_path)
                    if now - mtime > _TEMP_MAX_AGE:
                        shutil.rmtree(full_path, ignore_errors=True)
                except Exception:
                    pass
    except Exception:
        pass


def _get_media_duration(ffmpeg_path: str, file_path: str) -> float:
    try:
        result = subprocess.run(
            [ffmpeg_path, "-i", file_path, "-hide_banner"],
            capture_output=True, text=True, timeout=30
        )
        for line in result.stderr.split("\n"):
            if "Duration:" in line:
                time_str = line.split("Duration:")[1].split(",")[0].strip()
                parts = time_str.split(":")
                if len(parts) == 3:
                    h, m, s = parts
                    return float(h) * 3600 + float(m) * 60 + float(s)
    except Exception:
        pass
    return 0.0


def _extract_audio(ffmpeg_path: str, input_path: str, output_path: str) -> bool:
    try:
        result = subprocess.run(
            [ffmpeg_path, "-y", "-i", input_path, "-vn", "-acodec", "pcm_s16le",
             "-ar", "16000", "-ac", "1", output_path],
            capture_output=True, text=True, timeout=300
        )
        return result.returncode == 0 and os.path.exists(output_path)
    except subprocess.TimeoutExpired:
        logger.error("FFmpeg 提取音频超时")
        return False
    except Exception as e:
        logger.error(f"FFmpeg 提取音频失败: {e}")
        return False


def _split_audio_with_ffmpeg(
    ffmpeg_path: str, input_path: str, output_dir: str, segment_seconds: int = 60
) -> list[str]:
    try:
        result = subprocess.run(
            [ffmpeg_path, "-y", "-i", input_path, "-vn", "-acodec", "pcm_s16le",
             "-ar", "16000", "-ac", "1", "-f", "segment",
             "-segment_time", str(segment_seconds),
             os.path.join(output_dir, "seg_%03d.wav")],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            logger.error(f"FFmpeg 分段失败: {result.stderr}")
            return []
        return sorted(
            [os.path.join(output_dir, f) for f in os.listdir(output_dir) if f.endswith(".wav")]
        )
    except Exception as e:
        logger.error(f"FFmpeg 分段异常: {e}")
        return []


async def _transcribe_segment(
    session: aiohttp.ClientSession, api_url: str, api_token: str, wav_path: str,
) -> str:
    with open(wav_path, "rb") as f:
        audio_bytes = f.read()
    payload = {"audio": list(audio_bytes)}
    headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
    async with session.post(api_url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=120)) as resp:
        result = await resp.json()
    if not result.get("success"):
        errors = result.get("errors", [])
        error_msg = errors[0].get("message", "Unknown error") if errors else "Unknown error"
        raise RuntimeError(f"Whisper API 错误: {error_msg}")
    return result.get("result", {}).get("text", "")


async def _transcribe_audio(api_url: str, api_token: str, wav_path: str, segment_seconds: int = 60) -> str:
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("未找到 FFmpeg")

    duration = _get_media_duration(ffmpeg, wav_path)
    if duration <= segment_seconds:
        segments = [wav_path]
    else:
        temp_dir = tempfile.mkdtemp(prefix="videosum_split_")
        try:
            segments = _split_audio_with_ffmpeg(ffmpeg, wav_path, temp_dir, segment_seconds)
            if not segments:
                raise RuntimeError("音频分段失败")
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

    async with aiohttp.ClientSession() as session:
        texts = []
        for i, seg_path in enumerate(segments):
            logger.info(f"转录分段 {i + 1}/{len(segments)}")
            try:
                text = await _transcribe_segment(session, api_url, api_token, seg_path)
                if text:
                    texts.append(text)
            except Exception as e:
                logger.warning(f"分段 {i + 1} 转录失败: {e}")

    if len(segments) > 1 and segments:
        seg_dir = os.path.dirname(segments[0])
        if seg_dir and os.path.exists(seg_dir):
            shutil.rmtree(seg_dir, ignore_errors=True)

    return "\n".join(texts)


# ─── 插件主体 ─────────────────────────────────────────────


@register(
    "astrbot_plugin_video_summary",
    "RoyougiShiki",
    "视频总结插件 - FFmpeg提取音频 → Cloudflare Whisper语音转文字 → LLM总结 + B站UP主订阅通知",
    "0.3.0",
)
class VideoSummaryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._cf_account_id: str = config.get("cf_account_id", "")
        self._cf_api_token: str = config.get("cf_api_token", "")
        self._ffmpeg_path: str = config.get("ffmpeg_path", "")
        self._max_audio_minutes: int = config.get("max_audio_minutes", 30)
        self._summary_prompt: str = config.get(
            "summary_prompt",
            "请对以下视频转录文本进行总结。要求：\n1. 用中文总结\n2. 提取核心观点和关键信息\n3. 分点列出要点\n4. 如果有具体数据请保留\n5. 最后给出简短的一句话总结"
        )
        self._bili_proxy: str = config.get("bili_proxy", "")
        self._listen_interval: int = max(60, int(config.get("listen_interval", 300)))

        self._bili_client = BiliClient()
        self._data_manager = SubscriptionDataManager()
        self._listener = DynamicListener(
            self._data_manager, self._bili_client, self._listen_interval
        )

    def _get_ffmpeg(self) -> str:
        if self._ffmpeg_path and os.path.isfile(self._ffmpeg_path):
            return self._ffmpeg_path
        found = _find_ffmpeg()
        if not found:
            raise RuntimeError("未找到 FFmpeg")
        return found

    def _get_api_url(self) -> str:
        if not self._cf_account_id or not self._cf_api_token:
            raise RuntimeError("未配置 Cloudflare Account ID 或 API Token")
        return _CF_WHISPER_URL_TEMPLATE.format(account_id=self._cf_account_id)

    # ─── 视频总结命令 ─────────────────────────────────────

    @filter.command("videosum")
    async def handle_videosum(self, event: AstrMessageEvent):
        msg = (event.message_str or "").strip()
        for prefix in ("videosum ", "videosum", "/videosum ", "/videosum"):
            if msg.lower().startswith(prefix):
                remainder = msg[len(prefix):].strip()
                break
        else:
            remainder = msg

        if not remainder or remainder.lower() in ("help", "帮助"):
            yield event.plain_result(
                "🎬 视频总结插件\n\n"
                "用法:\n"
                "  /videosum <视频路径>    总结本地视频\n"
                "  /videosum <BV号>        如: /videosum BV1m5dhBzEgh\n"
                "  /videosum <B站链接>     如: /videosum https://b23.tv/xxx\n\n"
                "订阅命令:\n"
                "  /vsub <UID或链接>       订阅UP主新视频通知\n"
                "  /vsub_list              查看订阅列表\n"
                "  /vsub_del <UID>         取消订阅\n\n"
                "也可以直接发送 B站 QQ 小程序卡片，我会自动解析并总结。"
            )
            return

        async for result in self._process_input(event, remainder.strip().strip('"').strip("'")):
            yield result

    # ─── 订阅命令 ─────────────────────────────────────────

    @filter.command("vsub")
    async def handle_subscribe(self, event: AstrMessageEvent):
        """订阅 UP 主新视频通知"""
        msg = (event.message_str or "").strip()
        logger.info(f"视频总结插件: vsub raw message_str='{event.message_str}'")
        
        # AstrBot @filter.command 行为因版本/平台而异：
        # 有时自动剥离前缀，有时保留完整消息
        for prefix in ("vsub ", "vsub", "/vsub ", "/vsub"):
            if msg.lower().startswith(prefix):
                remainder = msg[len(prefix):].strip()
                break
        else:
            remainder = msg

        logger.info(f"视频总结插件: vsub parsed remainder='{remainder}'")

        if not remainder:
            yield event.plain_result(
                "📌 订阅UP主新视频通知\n\n"
                "用法:\n"
                "  /vsub <UP主UID>         如: /vsub 123456\n"
                "  /vsub <B站主页链接>     如: /vsub https://space.bilibili.com/123456\n"
                "  /vsub_list              查看订阅列表\n"
                "  /vsub_del <UID>         取消订阅"
            )
            return

        # 解析 UID
        uid = await self._resolve_uid(remainder)
        if not uid:
            yield event.plain_result("❌ 无法解析 UP 主 UID，请提供数字 UID 或 B站主页链接")
            return

        # 获取 UP 主信息
        info = await self._bili_client.get_user_info(uid)
        name = info.get("name", str(uid)) if info else str(uid)
        chat_key = event.unified_msg_origin
        logger.info(f"视频总结插件: vsub final - chat_key='{chat_key}', uid={uid}, name='{name}'")

        added = await self._data_manager.add_subscription(chat_key, uid, name)
        if added:
            yield event.plain_result(
                f"✅ 已订阅 UP主【{name}】(UID: {uid})\n"
                f"有新视频时会通知你，回复 /videosum <BV号> 即可总结。"
            )
        else:
            yield event.plain_result(f"⚠️ 你已经订阅过 UP主【{name}】了")

    @filter.command("vsub_list")
    async def handle_sub_list(self, event: AstrMessageEvent):
        """查看订阅列表"""
        chat_key = event.unified_msg_origin
        subs = self._data_manager.get_subscriptions(chat_key)

        if not subs:
            yield event.plain_result("📋 当前没有订阅任何 UP 主")
            return

        lines = ["📋 订阅列表\n"]
        for i, sub in enumerate(subs, 1):
            lines.append(f"  {i}. 【{sub.name}】UID: {sub.uid}")

        yield event.plain_result("\n".join(lines))

    @filter.command("vsub_del")
    async def handle_sub_del(self, event: AstrMessageEvent):
        """取消订阅"""
        msg = (event.message_str or "").strip()
        for prefix in ("vsub_del ", "vsub_del", "/vsub_del ", "/vsub_del"):
            if msg.lower().startswith(prefix):
                uid_str = msg[len(prefix):].strip()
                break
        else:
            uid_str = msg

        if not uid_str or not uid_str.isdigit():
            yield event.plain_result("❌ 请提供 UP 主 UID，如: /vsub_del 123456")
            return

        uid = int(uid_str)
        chat_key = event.unified_msg_origin
        removed = await self._data_manager.remove_subscription(chat_key, uid)

        if removed:
            up = self._data_manager.get_up(uid)
            name = up.name if up else str(uid)
            yield event.plain_result(f"✅ 已取消订阅 UP主【{name}】")
        else:
            yield event.plain_result("❌ 未找到该订阅")

    async def _resolve_uid(self, text: str) -> int | None:
        """从用户输入解析 UID"""
        text = text.strip()

        # 纯数字 UID
        if text.isdigit():
            return int(text)

        # B站主页链接: https://space.bilibili.com/123456
        match = re.search(r"space\.bilibili\.com/(\d+)", text)
        if match:
            return int(match.group(1))

        return None

    # ─── 小程序卡片解析 ───────────────────────────────────

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def parse_miniapp(self, event: AstrMessageEvent):
        for msg_element in event.message_obj.message:
            if not (hasattr(msg_element, "type") and msg_element.type == "Json" and hasattr(msg_element, "data")):
                continue

            json_string = msg_element.data
            try:
                parsed_data = json_string if isinstance(json_string, dict) else json.loads(json_string)
            except json.JSONDecodeError:
                continue

            meta = parsed_data.get("meta", {})
            qqdocurl = None
            title = None

            detail_1 = meta.get("detail_1", {})
            if detail_1.get("title") == "哔哩哔哩" and detail_1.get("qqdocurl"):
                qqdocurl = detail_1.get("qqdocurl")
                title = detail_1.get("desc", "")

            news = meta.get("news", {})
            if news.get("tag") == "哔哩哔哩" and news.get("jumpUrl"):
                qqdocurl = news.get("jumpUrl")
                title = news.get("title", "")

            if not qqdocurl:
                continue

            if "b23.tv" in qqdocurl:
                resolved = await self._bili_client.b23_to_bv(qqdocurl)
                if resolved:
                    qqdocurl = resolved

            bvid = self._bili_client.extract_bvid_from_url(qqdocurl)
            if not bvid:
                continue

            info = await self._bili_client.get_video_info(bvid)
            video_title = info.get("title", title or "未知标题") if info else (title or "未知标题")

            yield event.plain_result(f"🎬 检测到 B站视频: {video_title}\n正在总结中...")
            async for result in self._process_bilibili(event, bvid):
                yield result
            return

    # ─── 输入分发 ─────────────────────────────────────────

    async def _process_input(self, event: AstrMessageEvent, user_input: str):
        if not user_input:
            yield event.plain_result("❌ 请输入视频路径、BV 号或 B站链接")
            return

        if os.path.isfile(user_input):
            async for result in self._process_media(event, user_input):
                yield result
            return

        if re.match(r"^BV[a-zA-Z0-9]+$", user_input, re.IGNORECASE):
            bvid = user_input.upper()
            info = await self._bili_client.get_video_info(bvid)
            video_title = info.get("title", bvid) if info else bvid
            yield event.plain_result(f"🎬 正在总结: {video_title}\n请稍候...")
            async for result in self._process_bilibili(event, bvid):
                yield result
            return

        if "bilibili.com" in user_input or "b23.tv" in user_input:
            if "b23.tv" in user_input:
                resolved = await self._bili_client.b23_to_bv(user_input)
                if resolved:
                    user_input = resolved
            bvid = self._bili_client.extract_bvid_from_url(user_input)
            if bvid:
                info = await self._bili_client.get_video_info(bvid)
                video_title = info.get("title", bvid) if info else bvid
                yield event.plain_result(f"🎬 正在总结: {video_title}\n请稍候...")
                async for result in self._process_bilibili(event, bvid):
                    yield result
            else:
                yield event.plain_result("❌ 无法从链接中解析出 BV 号")
            return

        yield event.plain_result(f"❌ 无法识别输入: {user_input}")

    # ─── B站视频处理 ──────────────────────────────────────

    async def _process_bilibili(self, event: AstrMessageEvent, bvid: str):
        temp_dir = tempfile.mkdtemp(prefix="videosum_")
        try:
            audio_path = await self._bili_client.download_audio(bvid, temp_dir, proxy=self._bili_proxy)
            if not audio_path:
                yield event.plain_result("❌ 下载 B站视频音频失败")
                return
            async for result in self._process_media(event, audio_path):
                yield result
        finally:
            self._safe_rmtree(temp_dir)

    # ─── 核心媒体处理 ─────────────────────────────────────

    async def _process_media(self, event: AstrMessageEvent, file_path: str):
        if not os.path.isfile(file_path):
            yield event.plain_result(f"❌ 文件不存在: {file_path}")
            return

        ext = Path(file_path).suffix.lower()
        is_video = ext in _VIDEO_EXTS
        is_audio = ext in _AUDIO_EXTS

        if not is_video and not is_audio:
            yield event.plain_result(f"❌ 不支持的格式: {ext}")
            return

        try:
            ffmpeg = self._get_ffmpeg()
            api_url = self._get_api_url()
        except RuntimeError as e:
            yield event.plain_result(f"❌ {e}")
            return

        temp_dir = tempfile.mkdtemp(prefix="videosum_")
        try:
            if is_video:
                duration = _get_media_duration(ffmpeg, file_path)
                if duration <= 0:
                    yield event.plain_result("❌ 无法获取视频时长")
                    return
                if duration > self._max_audio_minutes * 60:
                    yield event.plain_result(f"❌ 视频 {duration / 60:.1f} 分钟，超过限制 {self._max_audio_minutes} 分钟")
                    return

            wav_path = os.path.join(temp_dir, "audio.wav")
            if not _extract_audio(ffmpeg, file_path, wav_path):
                yield event.plain_result("❌ 提取音频失败")
                return

            yield event.plain_result("🎤 语音转文字中...")
            transcription = await _transcribe_audio(api_url, self._cf_api_token, wav_path)

            if not transcription.strip():
                yield event.plain_result("❌ 语音识别结果为空")
                return

            yield event.plain_result("🧠 AI 总结中...")
            summary_request = f"{self._summary_prompt}\n\n---\n视频转录文本：\n{transcription}"
            result = await event.request_llm(prompt=summary_request)

            if result:
                output = f"🎬 视频总结\n\n📝 转录字数: {len(transcription)} 字\n{'─' * 30}\n{result}"
                yield event.plain_result(output)
            else:
                yield event.plain_result(f"🎬 视频转录（总结失败）\n\n{transcription[:3000]}")

        except Exception as e:
            logger.error(f"视频处理失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 处理失败: {e}")
        finally:
            self._safe_rmtree(temp_dir)

    @staticmethod
    def _safe_rmtree(path: str) -> None:
        try:
            if path and os.path.exists(path):
                shutil.rmtree(path, ignore_errors=True)
        except Exception as e:
            logger.warning(f"清理临时目录失败 {path}: {e}")

    # ─── LLM 工具 ─────────────────────────────────────────

    @filter.llm_tool(name="video_summary")
    async def tool_video_summary(self, event: AstrMessageEvent, video_path: str):
        """总结视频内容。支持本地视频、B站 BV 号或链接。

        Args:
            video_path(string): 视频文件的完整路径、BV 号或 B站链接
        """
        async for result in self._process_input(event, video_path):
            yield result

    # ─── 生命周期 ─────────────────────────────────────────

    async def initialize(self):
        if not self._cf_account_id or not self._cf_api_token:
            logger.warning("视频总结插件: 未配置 Cloudflare 凭证")
        else:
            logger.info("视频总结插件已加载")

        try:
            ffmpeg = self._get_ffmpeg()
            logger.info(f"视频总结插件: FFmpeg: {ffmpeg}")
        except RuntimeError:
            logger.warning("视频总结插件: 未找到 FFmpeg")

        _cleanup_old_temp_dirs()

        # 启动监听器
        self._listener.set_context(self.context)
        self._listener.start()
        logger.info("视频总结插件: B站动态监听已启动")

    async def terminate(self):
        self._listener.stop()
        logger.info("视频总结插件已卸载")
