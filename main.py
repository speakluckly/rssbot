import asyncio
import sys
import os

persistent_packages = "/AstrBot/data/python_packages"
if os.path.exists(persistent_packages) and persistent_packages not in sys.path:@filter
    sys.path.insert(0, persistent_packages)
import feedparser
import aiohttp
from datetime import datetime, timezone
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Plain, Image, At
from astrbot.api import logger

# 订阅数据存储结构：
# {
#   "user_subs": {
#     "qq_number": {
#         "origin": "unified_msg_origin",
#         "subscriptions": [
#             {"url": "http://...", "last_guid": "xxx", "last_title": "yyy"},
#             ...
#         ]
#     }
#   }
# }

@register("rss_subscriber", "你的名字", "一个简单的 RSS 订阅推送插件", "1.0.0")
class RssSubscriber(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.config = context.get_config()
        self.check_interval = 300  # 检查间隔，单位秒
        self._task = None

    async def _fetch_rss(self, url: str):
        """异步获取并解析 RSS feed，返回条目列表"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as resp:
                    if resp.status != 200:
                        logger.error(f"RSS 获取失败 {url}: HTTP {resp.status}")
                        return None
                    text = await resp.text()
        except Exception as e:
            logger.error(f"RSS 请求异常 {url}: {e}")
            return None

        loop = asyncio.get_event_loop()
        feed = await loop.run_in_executor(None, feedparser.parse, text)
        if feed.bozo:
            logger.warning(f"RSS 解析异常 {url}: {feed.bozo_exception}")
        return feed.entries

    def _get_latest_entry(self, entries):
        """从条目列表中找出最新的"""
        if not entries:
            return None
        sorted_entries = sorted(
            entries,
            key=lambda e: e.get('published_parsed') or e.get('updated_parsed') or (1970,1,1,0,0,0,0,0,0),
            reverse=True
        )
        return sorted_entries[0]

    async def _check_updates(self):
        """后台任务：检查所有订阅的更新"""
        while True:
            try:
                await asyncio.sleep(self.check_interval)
                logger.debug("开始检查 RSS 更新...")
                data = await self.get_kv_data("subscriptions") or {}
                user_subs = data.get("user_subs", {})
                if not user_subs:
                    continue

                # 收集所有唯一的 URL 及其订阅者
                url_to_users = {}
                for uid, info in user_subs.items():
                    origin = info.get("origin")
                    for sub in info.get("subscriptions", []):
                        url = sub["url"]
                        url_to_users.setdefault(url, []).append((uid, origin, sub))

                # 对每个 URL 检查更新
                for url, users in url_to_users.items():
                    entries = await self._fetch_rss(url)
                    if not entries:
                        continue
                    latest = self._get_latest_entry(entries)
                    if not latest:
                        continue
                    new_guid = latest.get('id') or latest.get('link')
                    new_title = latest.get('title', '无标题')
                    new_link = latest.get('link', '#')
                    new_published = latest.get('published', '')
                    if not new_guid:
                        continue

                    for uid, origin, sub_info in users:
                        last_guid = sub_info.get("last_guid")
                        if last_guid != new_guid:
                            try:
                                msg = f"📢 RSS 更新：{new_title}\n{new_link}\n{new_published}"
                                await self.context.send_message(origin, [Plain(msg)])
                                logger.info(f"已推送更新给用户 {uid} 来自 {url}")
                                sub_info["last_guid"] = new_guid
                                sub_info["last_title"] = new_title
                            except Exception as e:
                                logger.error(f"推送消息失败给 {uid}: {e}")

                await self.put_kv_data("subscriptions", data)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"RSS 检查任务异常: {e}")
                await asyncio.sleep(60)

    async def _start_background_task(self):
        self._task = asyncio.create_task(self._check_updates())

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        await self._start_background_task()
        logger.info("RSS 订阅插件已启动，后台检查间隔 {} 秒".format(self.check_interval))

    async def terminate(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ========== 用户指令（仅私聊） ==========
    @filter.command_group("rss")
    def rss(self):
        pass

    @rss.command("add")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def add_sub(self, event: AstrMessageEvent, url: str):
        """添加 RSS 订阅源"""
        entries = await self._fetch_rss(url)
        if entries is None:
            yield event.plain_result("❌ 无法访问或解析该 RSS 源，请检查 URL。")
            return

        latest = self._get_latest_entry(entries)
        if not latest:
            yield event.plain_result("❌ RSS 源无任何条目，无法订阅。")
            return

        data = await self.get_kv_data("subscriptions") or {}
        user_subs = data.setdefault("user_subs", {})

        # 使用 QQ 号作为用户标识
        uid = str(event.get_sender_id())
        origin = event.unified_msg_origin

        if uid not in user_subs:
            user_subs[uid] = {
                "origin": origin,
                "subscriptions": []
            }
        else:
            # 更新 origin（以防变化）
            user_subs[uid]["origin"] = origin

        for sub in user_subs[uid]["subscriptions"]:
            if sub["url"] == url:
                yield event.plain_result("⚠️ 你已经订阅过该 RSS 源。")
                return

        new_guid = latest.get('id') or latest.get('link')
        new_title = latest.get('title', '无标题')
        user_subs[uid]["subscriptions"].append({
            "url": url,
            "last_guid": new_guid,
            "last_title": new_title
        })

        await self.put_kv_data("subscriptions", data)
        yield event.plain_result(f"✅ 成功订阅 RSS 源：{url}\n最新条目：{new_title}")

    @rss.command("remove")
    @filter.private_message()
    async def remove_sub(self, event: AstrMessageEvent, url: str):
        """取消订阅 RSS 源"""
        uid = str(event.get_sender_id())
        data = await self.get_kv_data("subscriptions") or {}
        user_subs = data.get("user_subs", {})

        if uid not in user_subs:
            yield event.plain_result("❌ 你还没有任何订阅。")
            return

        subs = user_subs[uid]["subscriptions"]
        for i, sub in enumerate(subs):
            if sub["url"] == url:
                subs.pop(i)
                await self.put_kv_data("subscriptions", data)
                yield event.plain_result(f"✅ 已取消订阅：{url}")
                return

        yield event.plain_result("❌ 未找到该订阅 URL。")

    @rss.command("list")
    @filter.private_message()
    async def list_subs(self, event: AstrMessageEvent):
        """列出当前订阅的 RSS 源"""
        uid = str(event.get_sender_id())
        data = await self.get_kv_data("subscriptions") or {}
        user_subs = data.get("user_subs", {})

        if uid not in user_subs or not user_subs[uid]["subscriptions"]:
            yield event.plain_result("📭 你还没有任何 RSS 订阅。")
            return

        msg = "📋 你的 RSS 订阅列表：\n"
        for idx, sub in enumerate(user_subs[uid]["subscriptions"], 1):
            msg += f"{idx}. {sub['url']}\n   最新: {sub.get('last_title','')}\n"
        yield event.plain_result(msg)

    @rss.command("check")
    @filter.private_message()
    async def manual_check(self, event: AstrMessageEvent):
        """手动触发一次更新检查"""
        uid = str(event.get_sender_id())
        data = await self.get_kv_data("subscriptions") or {}
        user_subs = data.get("user_subs", {})

        if uid not in user_subs or not user_subs[uid]["subscriptions"]:
            yield event.plain_result("❌ 你还没有任何订阅。")
            return

        yield event.plain_result("🔄 开始为你手动检查更新...")
        updated = False
        for sub in user_subs[uid]["subscriptions"]:
            url = sub["url"]
            entries = await self._fetch_rss(url)
            if not entries:
                continue
            latest = self._get_latest_entry(entries)
            if not latest:
                continue
            new_guid = latest.get('id') or latest.get('link')
            new_title = latest.get('title', '无标题')
            new_link = latest.get('link', '#')
            new_published = latest.get('published', '')

            if sub.get("last_guid") != new_guid:
                msg = f"📢 发现新更新：{new_title}\n{new_link}\n{new_published}"
                yield event.plain_result(msg)
                sub["last_guid"] = new_guid
                sub["last_title"] = new_title
                updated = True

        if not updated:
            yield event.plain_result("✅ 暂无新更新。")
        else:
            await self.put_kv_data("subscriptions", data)
