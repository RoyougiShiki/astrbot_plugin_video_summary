"""订阅数据持久化管理

管理 B站 UP 主订阅列表，存储到 JSON 文件。
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from astrbot.api import logger
from astrbot.api.star import StarTools


@dataclass
class BiliSubscription:
    """单条订阅记录"""
    uid: int                       # UP 主 UID
    name: str = ""                 # UP 主 昵称
    last_bvid: str = ""            # 最新视频 BV 号（用于判断是否有更新）
    last_checked: float = 0.0      # 上次检查时间戳

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "BiliSubscription":
        return cls(
            uid=int(raw.get("uid", 0)),
            name=str(raw.get("name", "")),
            last_bvid=str(raw.get("last_bvid", "")),
            last_checked=float(raw.get("last_checked", 0)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "uid": self.uid,
            "name": self.name,
            "last_bvid": self.last_bvid,
            "last_checked": self.last_checked,
        }


@dataclass
class SubscriberInfo:
    """订阅者（群/私聊）"""
    chat_key: str              # 会话标识，如 "aiocqhttp:GroupMessage:123456"
    subscriptions: List[int] = field(default_factory=list)  # 订阅的 UP 主 UID 列表

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "SubscriberInfo":
        return cls(
            chat_key=str(raw.get("chat_key", "")),
            subscriptions=[int(x) for x in raw.get("subscriptions", []) if str(x).isdigit()],
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chat_key": self.chat_key,
            "subscriptions": list(self.subscriptions),
        }


class SubscriptionDataManager:
    """订阅数据管理器"""

    def __init__(self):
        self._path = os.path.join(
            StarTools.get_data_dir("astrbot_plugin_video_summary"),
            "subscriptions.json",
        )
        self._up_map: Dict[int, BiliSubscription] = {}      # uid -> subscription
        self._subscribers: Dict[str, SubscriberInfo] = {}    # chat_key -> info
        self._load()

    def _load(self):
        if not os.path.exists(self._path):
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            self._save_sync()
            return

        try:
            with open(self._path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"加载订阅数据失败: {e}")
            data = {}

        for raw in data.get("up_list", []):
            try:
                sub = BiliSubscription.from_dict(raw)
                self._up_map[sub.uid] = sub
            except Exception:
                pass

        for raw in data.get("subscribers", []):
            try:
                info = SubscriberInfo.from_dict(raw)
                self._subscribers[info.chat_key] = info
            except Exception:
                pass

    def _save_sync(self):
        payload = {
            "up_list": [s.to_dict() for s in self._up_map.values()],
            "subscribers": [s.to_dict() for s in self._subscribers.values()],
        }
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    async def save(self):
        await asyncio.to_thread(self._save_sync)

    # ─── UP 主管理 ────────────────────────────────────────

    def get_up(self, uid: int) -> Optional[BiliSubscription]:
        return self._up_map.get(uid)

    def get_all_ups(self) -> List[BiliSubscription]:
        return list(self._up_map.values())

    def get_ups_to_check(self, min_interval: float = 60.0) -> List[BiliSubscription]:
        """获取需要检查的 UP 主列表（排除刚检查过的）"""
        import time
        now = time.time()
        return [
            up for up in self._up_map.values()
            if now - up.last_checked >= min_interval
        ]

    async def update_up(self, sub: BiliSubscription):
        self._up_map[sub.uid] = sub
        await self.save()

    async def remove_up(self, uid: int):
        self._up_map.pop(uid, None)
        # 同时从所有订阅者中移除
        for info in self._subscribers.values():
            if uid in info.subscriptions:
                info.subscriptions.remove(uid)
        await self.save()

    # ─── 订阅者管理 ───────────────────────────────────────

    def get_subscriptions(self, chat_key: str) -> List[BiliSubscription]:
        """获取某个会话订阅的所有 UP 主"""
        info = self._subscribers.get(chat_key)
        if not info:
            return []
        return [self._up_map[uid] for uid in info.subscriptions if uid in self._up_map]

    def get_chat_keys_for_up(self, uid: int) -> List[str]:
        """获取订阅了某个 UP 主的所有会话"""
        return [
            ck for ck, info in self._subscribers.items()
            if uid in info.subscriptions
        ]

    async def add_subscription(self, chat_key: str, uid: int, name: str = "") -> bool:
        """添加订阅。返回 True 表示新增，False 表示已存在"""
        logger.info(f"视频总结插件: add_subscription chat_key='{chat_key}', uid={uid}, name='{name}'")
        
        if not chat_key:
            logger.error("视频总结插件: chat_key 为空，无法订阅！")
            return False
            
        # 确保 UP 主记录存在
        if uid not in self._up_map:
            self._up_map[uid] = BiliSubscription(uid=uid, name=name)
        elif name and not self._up_map[uid].name:
            self._up_map[uid].name = name

        # 确保订阅者记录存在
        if chat_key not in self._subscribers:
            self._subscribers[chat_key] = SubscriberInfo(chat_key=chat_key)

        info = self._subscribers[chat_key]
        if uid in info.subscriptions:
            return False  # 已订阅

        info.subscriptions.append(uid)
        await self.save()
        return True

    async def remove_subscription(self, chat_key: str, uid: int) -> bool:
        """移除订阅。返回 True 表示成功移除"""
        info = self._subscribers.get(chat_key)
        if not info or uid not in info.subscriptions:
            return False

        info.subscriptions.remove(uid)
        # 如果该 UP 主没有任何人订阅了，清理
        if not self.get_chat_keys_for_up(uid):
            self._up_map.pop(uid, None)
        await self.save()
        return True

    async def remove_all_subscriptions(self, chat_key: str) -> int:
        """移除某个会话的所有订阅，返回移除数量"""
        info = self._subscribers.pop(chat_key, None)
        if not info:
            return 0
        count = len(info.subscriptions)
        # 清理无人订阅的 UP 主
        for uid in info.subscriptions:
            if not self.get_chat_keys_for_up(uid):
                self._up_map.pop(uid, None)
        await self.save()
        return count
