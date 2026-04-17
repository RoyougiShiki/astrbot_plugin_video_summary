"""AstrBot 视频总结插件

支持无字幕视频的自动总结：
  1. FFmpeg 提取音频
  2. Cloudflare Workers AI Whisper 语音转文字
  3. LLM 总结转录文本

支持来源:
  - 本地视频/音频文件
  - Bilibili 视频链接 / BV号
  - QQ 小程序卡片消息（B站分享）

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
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register

from .bili_client import BiliClient

# ─── 常量 ────────────────────────────────────────────────

_CF_WHISPER_URL_TEMPLATE = (
    "https://api.cloudflare.com/client/v4/accounts/{account_id}"
    "/ai/run/@cf/openai/whisper"
)

# 支持的视频/音频格式
_VIDEO_EXTS = {".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".3gp", ".ts"}
_AUDIO_EXTS = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".opus", ".amr"}

# 临时文件最大保留时间（秒）
_TEMP_MAX_AGE = 86400  # 24 小时


def _find_ffmpeg() -> str:
    """查找系统中的 FFmpeg 可执行文件"""
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
    """清理过期的历史临时目录"""
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
                        logger.info(f"视频总结插件: 清理过期临时目录 {full_path}")
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
            [
                ffmpeg_path, "-y",
                "-i", input_path,
                "-vn",
                "-acodec", "pcm_s16le",
                "-ar", "16000",
                "-ac", "1",
                output_path,
            ],
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
    """使用 FFmpeg 的 segment muxer 分割音频"""
    try:
        result = subprocess.run(
            [
                ffmpeg_path, "-y",
                "-i", input_path,
                "-vn",
                "-acodec", "pcm_s16le",
                "-ar", "16000",
                "-ac", "1",
                "-f", "segment",
                "-segment_time", str(segment_seconds),
                os.path.join(output_dir, "seg_%03d.wav"),
            ],
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
    session: aiohttp.ClientSession,
    api_url: str,
    api_token: str,
    wav_path: str,
) -> str:
    with open(wav_path, "rb") as f:
        audio_bytes = f.read()
    payload = {"audio": list(audio_bytes)}
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }
    async with session.post(
        api_url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=120)
    ) as resp:
        result = await resp.json()
    if not result.get("success"):
        errors = result.get("errors", [])
        error_msg = errors[0].get("message", "Unknown error") if errors else "Unknown error"
        raise RuntimeError(f"Whisper API 错误: {error_msg}")
    return result.get("result", {}).get("text", "")


async def _transcribe_audio(
    api_url: str,
    api_token: str,
    wav_path: str,
    segment_seconds: int = 60,
) -> str:
    """转录音频文件，长音频自动分段"""
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("未找到 FFmpeg")

    # 如果音频本身很短，直接转录
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
            logger.info(f"转录分段 {i + 1}/{len(segments)}: {seg_path}")
            try:
                text = await _transcribe_segment(session, api_url, api_token, seg_path)
                if text:
                    texts.append(text)
            except Exception as e:
                logger.warning(f"分段 {i + 1} 转录失败: {e}")

    # 清理 FFmpeg 分段的临时目录（如果创建了）
    if len(segments) > 1 and segments:
        seg_dir = os.path.dirname(segments[0])
        if seg_dir and os.path.exists(seg_dir):
            shutil.rmtree(seg_dir, ignore_errors=True)

    return "\n".join(texts)


# ─── 插件主体 ─────────────────────────────────────────────


@register(
    "astrbot_plugin_video_summary",
    "RoyougiShiki",
    "视频总结插件 - 支持无字幕视频：FFmpeg提取音频 → Cloudflare Whisper语音转文字 → LLM总结",
    "0.2.0",
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
        self._bili_client = BiliClient()

    def _get_ffmpeg(self) -> str:
        if self._ffmpeg_path and os.path.isfile(self._ffmpeg_path):
            return self._ffmpeg_path
        found = _find_ffmpeg()
        if not found:
            raise RuntimeError("未找到 FFmpeg，请安装 FFmpeg 或在插件配置中指定路径")
        return found

    def _get_api_url(self) -> str:
        if not self._cf_account_id or not self._cf_api_token:
            raise RuntimeError("未配置 Cloudflare Account ID 或 API Token")
        return _CF_WHISPER_URL_TEMPLATE.format(account_id=self._cf_account_id)

    # ─── 命令入口 ─────────────────────────────────────────

    @filter.command("videosum")
    async def handle_videosum(self, event: AstrMessageEvent):
        msg = (event.message_str or "").strip()
        for prefix in ("/videosum ", "/videosum"):
            if msg.lower().startswith(prefix):
                remainder = msg[len(prefix):].strip()
                break
        else:
            remainder = ""

        if not remainder or remainder.lower() in ("help", "帮助"):
            yield event.plain_result(
                "🎬 视频总结插件\n\n"
                "用法:\n"
                "  /videosum <视频路径>    总结本地视频\n"
                "  /videosum <BV号>        如: /videosum BV1m5dhBzEgh\n"
                "  /videosum <B站链接>     如: /videosum https://b23.tv/xxx\n"
                "  /videosum help          显示帮助\n\n"
                "支持格式: mp4, avi, mkv, mov, wmv, flv, webm, mp3, wav 等\n"
                "也可以直接发送 B站 QQ 小程序卡片，我会自动解析并总结。"
            )
            return

        async for result in self._process_input(event, remainder.strip().strip('"').strip("'")):
            yield result

    # ─── 小程序卡片解析 ───────────────────────────────────

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def parse_miniapp(self, event: AstrMessageEvent):
        """解析 QQ 小程序卡片消息（B站分享）"""
        for msg_element in event.message_obj.message:
            if (
                hasattr(msg_element, "type")
                and msg_element.type == "Json"
                and hasattr(msg_element, "data")
            ):
                json_string = msg_element.data
                try:
                    if isinstance(json_string, dict):
                        parsed_data = json_string
                    else:
                        parsed_data = json.loads(json_string)
                except json.JSONDecodeError:
                    continue

                meta = parsed_data.get("meta", {})
                qqdocurl = None
                title = None

                # 新版小程序格式
                detail_1 = meta.get("detail_1", {})
                if detail_1.get("title") == "哔哩哔哩" and detail_1.get("qqdocurl"):
                    qqdocurl = detail_1.get("qqdocurl")
                    title = detail_1.get("desc", "")

                # 旧版小程序格式
                news = meta.get("news", {})
                if news.get("tag") == "哔哩哔哩" and news.get("jumpUrl"):
                    qqdocurl = news.get("jumpUrl")
                    title = news.get("title", "")

                if not qqdocurl:
                    continue

                # 解析短链
                if "b23.tv" in qqdocurl:
                    resolved = await self._bili_client.b23_to_bv(qqdocurl)
                    if resolved:
                        qqdocurl = resolved

                bvid = self._bili_client.extract_bvid_from_url(qqdocurl)
                if not bvid:
                    continue

                info = await self._bili_client.get_video_info(bvid)
                video_title = info.get("title", title or "未知标题") if info else (title or "未知标题")

                yield event.plain_result(f"🎬 检测到 B站视频分享: {video_title}\n正在总结中，请稍候...")
                async for result in self._process_bilibili(event, bvid):
                    yield result
                return

    # ─── 输入分发 ─────────────────────────────────────────

    async def _process_input(self, event: AstrMessageEvent, user_input: str):
        """根据用户输入类型分发处理"""
        if not user_input:
            yield event.plain_result("❌ 请输入视频路径、BV 号或 B站链接")
            return

        # 1. 本地文件
        if os.path.isfile(user_input):
            async for result in self._process_media(event, user_input):
                yield result
            return

        # 2. BV 号
        if re.match(r"^BV[a-zA-Z0-9]+$", user_input, re.IGNORECASE):
            bvid = user_input.upper()
            info = await self._bili_client.get_video_info(bvid)
            video_title = info.get("title", bvid) if info else bvid
            yield event.plain_result(f"🎬 正在总结 B站视频: {video_title}\n请稍候...")
            async for result in self._process_bilibili(event, bvid):
                yield result
            return

        # 3. B站链接
        if "bilibili.com" in user_input or "b23.tv" in user_input:
            if "b23.tv" in user_input:
                resolved = await self._bili_client.b23_to_bv(user_input)
                if resolved:
                    user_input = resolved
            bvid = self._bili_client.extract_bvid_from_url(user_input)
            if bvid:
                info = await self._bili_client.get_video_info(bvid)
                video_title = info.get("title", bvid) if info else bvid
                yield event.plain_result(f"🎬 正在总结 B站视频: {video_title}\n请稍候...")
                async for result in self._process_bilibili(event, bvid):
                    yield result
            else:
                yield event.plain_result("❌ 无法从链接中解析出 BV 号")
            return

        yield event.plain_result(f"❌ 无法识别输入: {user_input}\n请提供本地视频路径、BV 号或 B站链接")

    # ─── B站视频处理 ──────────────────────────────────────

    async def _process_bilibili(self, event: AstrMessageEvent, bvid: str):
        """下载 B站视频音频并总结"""
        temp_dir = tempfile.mkdtemp(prefix="videosum_")
        audio_path = None
        try:
            audio_path = await self._bili_client.download_audio(
                bvid, temp_dir, proxy=self._bili_proxy
            )
            if not audio_path:
                yield event.plain_result("❌ 下载 B站视频音频失败，请确认视频有效或 BV 号正确")
                return

            async for result in self._process_media(event, audio_path):
                yield result
        finally:
            self._safe_rmtree(temp_dir)

    # ─── 核心媒体处理 ─────────────────────────────────────

    async def _process_media(self, event: AstrMessageEvent, file_path: str):
        """处理视频/音频文件的完整流程"""
        if not os.path.isfile(file_path):
            yield event.plain_result(f"❌ 文件不存在: {file_path}")
            return

        ext = Path(file_path).suffix.lower()
        is_video = ext in _VIDEO_EXTS
        is_audio = ext in _AUDIO_EXTS

        if not is_video and not is_audio:
            yield event.plain_result(
                f"❌ 不支持的文件格式: {ext}\n"
                f"支持: {', '.join(sorted(_VIDEO_EXTS | _AUDIO_EXTS))}"
            )
            return

        try:
            ffmpeg = self._get_ffmpeg()
            api_url = self._get_api_url()
        except RuntimeError as e:
            yield event.plain_result(f"❌ {e}")
            return

        temp_dir = tempfile.mkdtemp(prefix="videosum_")
        try:
            # Step 1: 提取/转换音频
            if is_video:
                duration = _get_media_duration(ffmpeg, file_path)
                if duration <= 0:
                    yield event.plain_result("❌ 无法获取视频时长，文件可能已损坏")
                    return
                if duration > self._max_audio_minutes * 60:
                    yield event.plain_result(
                        f"❌ 视频时长 {duration / 60:.1f} 分钟，"
                        f"超过最大限制 {self._max_audio_minutes} 分钟"
                    )
                    return

                wav_path = os.path.join(temp_dir, "audio.wav")
                if not _extract_audio(ffmpeg, file_path, wav_path):
                    yield event.plain_result("❌ 提取音频失败，请确认视频文件有效")
                    return
            else:
                wav_path = os.path.join(temp_dir, "audio.wav")
                if not _extract_audio(ffmpeg, file_path, wav_path):
                    yield event.plain_result("❌ 音频格式转换失败")
                    return

            # Step 2: 语音转文字
            yield event.plain_result("🎤 正在语音转文字中，请稍候...")
            transcription = await _transcribe_audio(api_url, self._cf_api_token, wav_path)

            if not transcription.strip():
                yield event.plain_result("❌ 语音识别结果为空，视频可能没有语音内容")
                return

            # Step 3: LLM 总结
            yield event.plain_result("🧠 AI 正在总结中...")
            summary_request = (
                f"{self._summary_prompt}\n\n---\n视频转录文本：\n{transcription}"
            )
            result = await event.request_llm(prompt=summary_request)

            if result:
                output = (
                    f"🎬 视频总结\n\n"
                    f"📝 转录字数: {len(transcription)} 字\n"
                    f"{'─' * 30}\n"
                    f"{result}"
                )
                yield event.plain_result(output)
            else:
                output = (
                    f"🎬 视频转录\n\n"
                    f"（LLM 总结失败，以下为原始转录前 3000 字）\n\n"
                    f"{transcription[:3000]}"
                )
                if len(transcription) > 3000:
                    output += f"\n\n... (共 {len(transcription)} 字，已截断)"
                yield event.plain_result(output)

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
        """总结视频内容。用户要求总结视频时使用。支持本地视频、B站 BV 号或链接。

        Args:
            video_path(string): 视频文件的完整路径、BV 号或 B站链接
        """
        async for result in self._process_input(event, video_path):
            yield result

    # ─── 生命周期 ─────────────────────────────────────────

    async def initialize(self):
        if not self._cf_account_id or not self._cf_api_token:
            logger.warning("视频总结插件: 未配置 Cloudflare 凭证，插件将无法正常工作")
        else:
            logger.info("视频总结插件已加载")

        try:
            ffmpeg = self._get_ffmpeg()
            logger.info(f"视频总结插件: FFmpeg 路径: {ffmpeg}")
        except RuntimeError:
            logger.warning("视频总结插件: 未找到 FFmpeg，请安装")

        # 启动时清理历史临时目录
        _cleanup_old_temp_dirs()

    async def terminate(self):
        logger.info("视频总结插件已卸载")
