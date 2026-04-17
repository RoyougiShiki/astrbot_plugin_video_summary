"""AstrBot 视频总结插件

支持无字幕视频的自动总结：
  1. FFmpeg 提取音频
  2. Cloudflare Workers AI Whisper 语音转文字
  3. LLM 总结转录文本

命令:
  /videosum <视频路径或URL>   总结视频内容
  /videosum help              显示帮助
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import struct
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

# ─── 常量 ────────────────────────────────────────────────

_CF_WHISPER_URL_TEMPLATE = (
    "https://api.cloudflare.com/client/v4/accounts/{account_id}"
    "/ai/run/@cf/openai/whisper"
)
_CF_NEURONS_PER_MINUTE = 41  # 大约每分钟音频消耗的 Neurons

# 支持的视频/音频格式
_VIDEO_EXTS = {".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".3gp", ".ts"}
_AUDIO_EXTS = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".opus", ".amr"}

# Whisper 支持的音频格式
_WHISPER_INPUT_EXTS = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a"}


def _find_ffmpeg() -> str:
    """查找系统中的 FFmpeg 可执行文件"""
    # 1. 直接在 PATH 中找
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg

    # 2. 尝试 imageio-ffmpeg
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass

    # 3. 常见路径
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


def _get_media_duration(ffmpeg_path: str, file_path: str) -> float:
    """获取媒体文件时长（秒）"""
    try:
        result = subprocess.run(
            [ffmpeg_path, "-i", file_path, "-hide_banner"],
            capture_output=True, text=True, timeout=30
        )
        # 从 stderr 解析时长
        output = result.stderr
        for line in output.split("\n"):
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
    """使用 FFmpeg 从视频中提取音频为 16kHz 单声道 WAV"""
    try:
        result = subprocess.run(
            [
                ffmpeg_path, "-y",
                "-i", input_path,
                "-vn",                  # 去掉视频流
                "-acodec", "pcm_s16le", # 16-bit PCM
                "-ar", "16000",         # 16kHz 采样率
                "-ac", "1",             # 单声道
                output_path
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


def _split_wav(wav_path: str, segment_seconds: int = 300) -> list[str]:
    """将 WAV 文件按指定秒数分段，返回分段文件路径列表"""
    segments = []

    with open(wav_path, "rb") as f:
        header = f.read(44)  # WAV header
        data = f.read()

    sample_rate = struct.unpack_from("<I", header, 24)[0]
    bytes_per_sample = struct.unpack_from("<H", header, 34)[0] // 8
    bytes_per_second = sample_rate * bytes_per_sample
    segment_bytes = bytes_per_second * segment_seconds

    total_data = len(data)
    if total_data <= segment_bytes:
        return [wav_path]

    temp_dir = tempfile.mkdtemp(prefix="videosum_")
    offset = 0
    idx = 0

    while offset < total_data:
        chunk = data[offset:offset + segment_bytes]
        seg_path = os.path.join(temp_dir, f"segment_{idx:03d}.wav")

        # 构建新的 WAV 头
        data_size = len(chunk)
        new_header = bytearray(header)
        struct.pack_into("<I", new_header, 4, 36 + data_size)  # RIFF size
        struct.pack_into("<I", new_header, 40, data_size)       # data size

        with open(seg_path, "wb") as f:
            f.write(bytes(new_header))
            f.write(chunk)

        segments.append(seg_path)
        offset += segment_bytes
        idx += 1

    return segments


async def _transcribe_segment(
    session: aiohttp.ClientSession,
    api_url: str,
    api_token: str,
    wav_path: str,
) -> dict[str, Any]:
    """调用 Cloudflare Whisper API 转录单个音频段"""
    with open(wav_path, "rb") as f:
        audio_bytes = f.read()

    audio_array = list(audio_bytes)

    payload = {"audio": audio_array}
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    async with session.post(api_url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=120)) as resp:
        result = await resp.json()

    if not result.get("success"):
        errors = result.get("errors", [])
        error_msg = errors[0].get("message", "Unknown error") if errors else "Unknown error"
        raise RuntimeError(f"Whisper API 错误: {error_msg}")

    return result.get("result", {})


async def _transcribe_audio(
    api_url: str,
    api_token: str,
    wav_path: str,
    segment_seconds: int = 300,
) -> str:
    """转录音频文件，长音频自动分段"""
    segments = _split_wav(wav_path, segment_seconds)

    async with aiohttp.ClientSession() as session:
        texts = []
        for i, seg_path in enumerate(segments):
            logger.info(f"转录分段 {i + 1}/{len(segments)}: {seg_path}")
            try:
                result = await _transcribe_segment(session, api_url, api_token, seg_path)
                text = result.get("text", "")
                if text:
                    texts.append(text)
            except Exception as e:
                logger.warning(f"分段 {i + 1} 转录失败: {e}")

    return "\n".join(texts)


# ─── 插件主体 ─────────────────────────────────────────────


@register(
    "astrbot_plugin_video_summary",
    "RoyougiShiki",
    "视频总结插件 - 支持无字幕视频：FFmpeg提取音频 → Cloudflare Whisper语音转文字 → LLM总结",
    "0.1.0",
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
        """视频总结命令"""
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
                "  /videosum <视频路径>   总结本地视频\n"
                "  /videosum help         显示帮助\n\n"
                "支持格式: mp4, avi, mkv, mov, wmv, flv, webm, mp3, wav 等\n\n"
                "也可以直接发送视频文件，我会自动总结。"
            )
            return

        # 处理文件路径
        file_path = remainder.strip().strip('"').strip("'")

        async for result in self._process_media(event, file_path):
            yield result

    # ─── 处理媒体文件 ─────────────────────────────────────

    async def _process_media(self, event: AstrMessageEvent, file_path: str):
        """处理视频/音频文件的完整流程"""

        # 验证文件
        if not os.path.isfile(file_path):
            yield event.plain_result(f"❌ 文件不存在: {file_path}")
            return

        ext = Path(file_path).suffix.lower()
        is_video = ext in _VIDEO_EXTS
        is_audio = ext in _AUDIO_EXTS

        if not is_video and not is_audio:
            yield event.plain_result(f"❌ 不支持的文件格式: {ext}\n支持: {', '.join(sorted(_VIDEO_EXTS | _AUDIO_EXTS))}")
            return

        try:
            ffmpeg = self._get_ffmpeg()
        except RuntimeError as e:
            yield event.plain_result(f"❌ {e}")
            return

        try:
            api_url = self._get_api_url()
        except RuntimeError as e:
            yield event.plain_result(f"❌ {e}")
            return

        yield event.plain_result("🎬 正在处理视频，请稍候...\n1/3 提取音频中")

        # Step 1: 如果是视频，提取音频
        temp_dir = tempfile.mkdtemp(prefix="videosum_")
        try:
            if is_video:
                # 检查时长
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
                success = _extract_audio(ffmpeg, file_path, wav_path)
                if not success:
                    yield event.plain_result("❌ 提取音频失败，请确认视频文件有效")
                    return

                logger.info(f"音频提取成功: {wav_path}, 时长 {duration:.1f}s")
            else:
                # 音频文件：如果需要转格式
                if ext in _WHISPER_INPUT_EXTS and ext == ".wav":
                    wav_path = file_path
                else:
                    wav_path = os.path.join(temp_dir, "audio.wav")
                    success = _extract_audio(ffmpeg, file_path, wav_path)
                    if not success:
                        yield event.plain_result("❌ 音频格式转换失败")
                        return

            # Step 2: 语音转文字
            yield event.plain_result("🎬 正在处理视频，请稍候...\n2/3 语音转文字中（可能需要几分钟）")

            transcription = await _transcribe_audio(
                api_url, self._cf_api_token, wav_path
            )

            if not transcription.strip():
                yield event.plain_result("❌ 语音识别结果为空，视频可能没有语音内容")
                return

            logger.info(f"转录完成，文本长度: {len(transcription)} 字符")

            # Step 3: LLM 总结
            yield event.plain_result("🎬 正在处理视频，请稍候...\n3/3 AI 总结中")

            # 使用 LLM 总结
            from astrbot.core.provider.entities import ProviderRequest
            from astrbot.core.message.message_event_result import MessageChain

            # 通过 event 的 LLM 请求来总结
            summary_request = f"{self._summary_prompt}\n\n---\n视频转录文本：\n{transcription}"

            # 直接让 LLM 处理
            result = await event.request_llm(
                prompt=summary_request,
            )

            if result:
                # 构建最终输出
                output = f"🎬 视频总结\n\n"
                output += f"📝 转录字数: {len(transcription)} 字\n\n"
                output += f"{'─' * 30}\n"
                output += result

                yield event.plain_result(output)
            else:
                # LLM 总结失败，直接返回转录文本
                output = f"🎬 视频转录\n\n"
                output += f"（LLM 总结失败，以下为原始转录）\n\n"
                output += transcription[:3000]
                if len(transcription) > 3000:
                    output += f"\n\n... (共 {len(transcription)} 字，已截断)"

                yield event.plain_result(output)

        except Exception as e:
            logger.error(f"视频处理失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 处理失败: {e}")
        finally:
            # 清理临时文件
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass

    # ─── LLM 工具 ─────────────────────────────────────────

    @filter.llm_tool(name="video_summary")
    async def tool_video_summary(self, event: AstrMessageEvent, video_path: str):
        """总结视频内容。用户要求总结视频时使用。会先提取音频，再语音转文字，最后用AI总结。

        Args:
            video_path(string): 视频文件的完整路径
        """
        async for result in self._process_media(event, video_path):
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

    async def terminate(self):
        logger.info("视频总结插件已卸载")
