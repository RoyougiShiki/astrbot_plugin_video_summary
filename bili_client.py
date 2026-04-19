"""简化版 Bilibili 客户端

用于解析 B站链接、获取视频信息、下载音频
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Optional

import aiohttp

from astrbot.api import logger


class BiliClient:
    """Bilibili 客户端"""

    @staticmethod
    async def b23_to_bv(url: str) -> Optional[str]:
        """b23.tv 短链转换为原始链接"""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/91.0.4472.124 Safari/537.36"
            )
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, headers=headers, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status in (301, 302, 307, 308):
                        location_url = response.headers.get("Location")
                        if location_url:
                            return location_url.split("?", 1)[0]
        except Exception as e:
            logger.error(f"解析 b23 链接失败 ({url}): {e}")
        return url

    @staticmethod
    def extract_bvid_from_url(url: str) -> Optional[str]:
        """从 B站 URL 中提取 BV 号"""
        patterns = [
            r"BV([a-zA-Z0-9]+)",
            r"/video/([a-zA-Z0-9]+)",
        ]
        for pat in patterns:
            match = re.search(pat, url, re.IGNORECASE)
            if match:
                bvid = match.group(1)
                if bvid.upper().startswith("BV"):
                    return bvid
                return f"BV{bvid}"
        return None

    @staticmethod
    async def get_user_info(uid: int) -> Optional[dict]:
        """获取 B站用户基本信息
        
        优先使用搜索 API（不需要 wbi 签名），
        失败时回退到用户信息 API。
        """
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/91.0.4472.124 Safari/537.36"
            ),
            "Referer": "https://www.bilibili.com",
        }
        
        # 方法1: 用搜索 API 查找用户（不需要 wbi 签名）
        # 关键：用 "uid{数字}" 搜索才能在 bili_user 结果中找到
        try:
            search_url = f"https://api.bilibili.com/x/web-interface/search/all/v2?keyword=uid{uid}"
            async with aiohttp.ClientSession() as session:
                async with session.get(search_url, headers=headers, timeout=10) as resp:
                    data = await resp.json()
                    if data.get("code") == 0:
                        for group in data.get("data", {}).get("result", []):
                            if group.get("result_type") == "bili_user":
                                for item in group.get("data", []):
                                    if int(item.get("mid", 0)) == uid:
                                        return {
                                            "name": item.get("uname", ""),
                                            "mid": uid,
                                            "face": item.get("upic", ""),
                                        }
        except Exception as e:
            logger.debug(f"搜索 API 获取用户信息失败 ({uid}): {e}")
        
        # 方法2: 回退到用户信息 API
        try:
            api_url = f"https://api.bilibili.com/x/space/wbi/acc/info?mid={uid}"
            async with aiohttp.ClientSession() as session:
                async with session.get(api_url, headers=headers, timeout=10) as resp:
                    data = await resp.json()
                    if data.get("code") == 0:
                        return data.get("data", {})
        except Exception as e:
            logger.debug(f"用户信息 API 获取失败 ({uid}): {e}")
        
        # 方法3: 如果都失败了，返回仅含 UID 的基本信息
        return {"name": str(uid), "mid": uid}

    @staticmethod
    async def get_video_info(bvid: str) -> Optional[dict]:
        """获取 B站视频基本信息"""
        api_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/91.0.4472.124 Safari/537.36"
            ),
            "Referer": "https://www.bilibili.com",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(api_url, headers=headers, timeout=10) as resp:
                    data = await resp.json()
                    if data.get("code") == 0:
                        return data.get("data", {})
        except Exception as e:
            logger.error(f"获取视频信息失败 ({bvid}): {e}")
        return None

    @staticmethod
    async def download_audio(
        url_or_bvid: str,
        output_dir: str,
        proxy: str = "",
    ) -> Optional[str]:
        """使用 yt-dlp 下载 B站视频的音频

        Args:
            url_or_bvid: B站视频 URL 或 BV 号
            output_dir: 输出目录
            proxy: 可选代理

        Returns:
            下载的音频文件路径，失败返回 None
        """
        # 确保 yt-dlp 可用
        yt_dlp = shutil.which("yt-dlp")
        if not yt_dlp:
            # 尝试用 python -m yt_dlp
            try:
                subprocess.run(
                    ["python", "-m", "yt_dlp", "--version"],
                    capture_output=True,
                    check=True,
                    timeout=5,
                )
                yt_dlp = "python -m yt_dlp"
            except Exception:
                logger.error("未找到 yt-dlp，请安装: pip install yt-dlp")
                return None

        # 构建 URL
        if url_or_bvid.upper().startswith("BV"):
            video_url = f"https://www.bilibili.com/video/{url_or_bvid}"
        else:
            video_url = url_or_bvid

        output_path = os.path.join(output_dir, "audio")
        cmd = [
            *yt_dlp.split(),
            "-f", "bestaudio[ext=m4a]/bestaudio/best",
            "-o", output_path + ".%(ext)s",
            "--no-check-certificates",
            "--quiet",
            "--no-warnings",
            video_url,
        ]
        if proxy:
            cmd.extend(["--proxy", proxy])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                logger.error(f"yt-dlp 下载失败: {result.stderr}")
                return None

            # 找到下载的文件
            for ext in [".m4a", ".mp3", ".webm", ".opus", ".aac"]:
                candidate = output_path + ext
                if os.path.exists(candidate):
                    return candidate
        except subprocess.TimeoutExpired:
            logger.error("yt-dlp 下载超时")
        except Exception as e:
            logger.error(f"yt-dlp 下载异常: {e}")
        return None
