"""B站 UP 主动态监听器

后台定时轮询已订阅的 UP 主，发现新视频时推送通知。
使用搜索 API 获取最新视频（其他 API 被 B站风控）。
"""

from __future__ import annotations

import asyncio
import re
import time
import traceback
from typing import Dict, List, Optional

import aiohttp

from astrbot.api import logger

from .bili_client import BiliClient
from .data_manager import BiliSubscription, SubscriptionDataManager


DEFAULT_INTERVAL = 300
DEFAULT_TASK_GAP = 20


class DynamicListener:
    """后台动态监听器"""

    def __init__(
        self,
        data_manager: SubscriptionDataManager,
        bili_client: BiliClient,
        interval: int = DEFAULT_INTERVAL,
    ):
        self.data_manager = data_manager
        self.bili_client = bili_client
        self.interval = max(60, interval)
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._context = None

    def set_context(self, context):
        self._context = context

    def start(self):
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def _poll_loop(self):
        logger.info(f"视频总结插件: 动态监听启动，轮询间隔 {self.interval}s")

        while self._running:
            try:
                ups = self.data_manager.get_ups_to_check(min_interval=self.interval)
                if not ups:
                    await asyncio.sleep(10)
                    continue

                for up in ups:
                    if not self._running:
                        break
                    try:
                        await self._check_up(up)
                    except Exception as e:
                        logger.error(f"检查 UP 主 {up.name}({up.uid}) 失败: {e}")
                    await asyncio.sleep(DEFAULT_TASK_GAP)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"动态监听异常: {e}\n{traceback.format_exc()}")
                await asyncio.sleep(30)

        logger.info("视频总结插件: 动态监听已停止")

    async def _check_up(self, up: BiliSubscription):
        """检查单个 UP 主是否有新视频"""
        videos = await self._get_latest_videos(up.uid, up.name)
        up.last_checked = time.time()

        if not videos:
            await self.data_manager.update_up(up)
            return

        # 第一次检查：只记录，不推送
        if not up.last_bvid:
            up.last_bvid = videos[0].get("bvid", "")
            # 更新昵称
            if videos[0].get("author"):
                up.name = videos[0]["author"]
            await self.data_manager.update_up(up)
            logger.info(f"视频总结插件: 初始化 UP 主 {up.name}({up.uid})，最新视频: {videos[0].get('title','')}")
            return

        # 查找新视频
        new_videos = []
        for v in videos:
            bvid = v.get("bvid", "")
            if bvid == up.last_bvid:
                break
            new_videos.append(v)

        # 更新最新视频 BV 号
        if videos:
            up.last_bvid = videos[0].get("bvid", up.last_bvid)
            if videos[0].get("author"):
                up.name = videos[0]["author"]

        await self.data_manager.update_up(up)

        # 推送新视频通知（从旧到新）
        for v in reversed(new_videos):
            await self._push_video_notification(up, v)

    async def _get_latest_videos(self, uid: int, name: str) -> List[dict]:
        """获取 UP 主最新视频列表
        
        优先用搜索 API（稳定可用），备用空间视频 API。
        """
        # 方法1: 搜索 API（最稳定）
        search_name = name if name and name != str(uid) else str(uid)
        results = await self._search_videos(search_name, uid)
        if results:
            return results

        # 方法2: 空间视频 API（可能被风控）
        results = await self._space_videos(uid)
        if results:
            return results

        return []

    async def _search_videos(self, keyword: str, target_uid: int) -> List[dict]:
        """通过搜索 API 获取 UP 主的最新视频"""
        api_url = "https://api.bilibili.com/x/web-interface/search/all/v2"
        params = {"keyword": keyword, "page": 1, "page_size": 5}
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Referer": "https://search.bilibili.com",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    api_url, params=params, headers=headers, timeout=15
                ) as resp:
                    data = await resp.json()
                    if data.get("code") != 0:
                        return []
                    
                    for group in data.get("data", {}).get("result", []):
                        if group.get("result_type") == "video":
                            videos = []
                            for item in group.get("data", []):
                                mid = item.get("mid", 0)
                                # 只匹配目标 UP 主
                                if mid and int(mid) == target_uid:
                                    videos.append({
                                        "bvid": item.get("bvid", ""),
                                        "title": item.get("title", "").replace('<em class="keyword">', "").replace("</em>", ""),
                                        "author": item.get("author", ""),
                                        "pic": item.get("pic", ""),
                                    })
                            return videos
        except Exception as e:
            logger.debug(f"搜索 API 获取失败 ({keyword}): {e}")
        return []

    async def _space_videos(self, uid: int) -> List[dict]:
        """通过空间视频 API 获取（备用，可能被风控）"""
        api_url = "https://api.bilibili.com/x/space/wbi/arc/search"
        params = {"mid": uid, "ps": 3, "pn": 1, "order": "pubdate"}
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Referer": f"https://space.bilibili.com/{uid}",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    api_url, params=params, headers=headers, timeout=15
                ) as resp:
                    data = await resp.json()
                    if data.get("code") == 0:
                        vlist = data.get("data", {}).get("list", {}).get("vlist", [])
                        return [
                            {
                                "bvid": v.get("bvid", ""),
                                "title": v.get("title", ""),
                                "author": v.get("author", ""),
                                "pic": v.get("pic", ""),
                            }
                            for v in vlist
                        ]
        except Exception as e:
            logger.debug(f"空间视频 API 获取失败 ({uid}): {e}")
        return []

    async def _push_video_notification(self, up: BiliSubscription, video: dict):
        """推送新视频通知"""
        chat_keys = self.data_manager.get_chat_keys_for_up(up.uid)
        if not chat_keys or not self._context:
            return

        bvid = video.get("bvid", "")
        title = video.get("title", "新视频")

        video_url = f"https://www.bilibili.com/video/{bvid}"
        text = (
            f"📢 UP主【{up.name}】发布了新视频！\n\n"
            f"🎬 {title}\n"
            f"🔗 {video_url}\n\n"
            f"💡 回复 /videosum {bvid} 即可总结此视频"
        )

        logger.info(f"视频总结插件: 推送新视频通知: {up.name} - {title[:30]}")

        for chat_key in chat_keys:
            try:
                await self._context.send_by_chat_key(chat_key, text)
            except Exception as e:
                logger.warning(f"推送通知到 {chat_key} 失败: {e}")
