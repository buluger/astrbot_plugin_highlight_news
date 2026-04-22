import os
import random
import time
import json
import uuid
import yaml
import aiohttp
import re
import textwrap
import shutil
from typing import List, Optional, Tuple
from astrbot import logger
from astrbot.core.message.components import Image, Reply, At, Plain
from astrbot.api.all import *

try:
    from PIL import Image as PILImage, ImageDraw, ImageFont
except ImportError:
    PILImage = None
    ImageDraw = None
    ImageFont = None


def _pick_cjk_font(size: int):
    if ImageFont is None:
        return None
    candidates = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "C:\\Windows\\Fonts\\msyh.ttc",
        "C:\\Windows\\Fonts\\simhei.ttf",
    ]
    for p in candidates:
        if os.path.isfile(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default()
    except OSError:
        return None


@register(
    "quote_collocter",
    "浅夏旧入梦",
    "发送「精华投稿」+文字或图片保存精华；「/精华」随机一条；「精华图」生成汇总长图。戳一戳随机一条。",
    "1.0",
)
class HighlightsPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data_root = os.path.join("data", "highlights_data")
        bot_config = context.get_config()
        admins = bot_config.get("admins_id", [])
        self.admins = [str(admin) for admin in admins] if admins else []
        if self.admins:
            logger.info(f"从 astrbot 配置中获取到管理员ID列表: {self.admins}")
        else:
            logger.warning("未找到任何管理员ID，某些需要管理员权限的命令可能无法使用")

    def create_main_folder(self):
        if not os.path.exists(self.data_root):
            os.makedirs(self.data_root)

    def create_group_folder(self, group_id):
        group_id = str(group_id)
        if not os.path.exists(self.data_root):
            self.create_main_folder()
        group_folder_path = os.path.join(self.data_root, group_id)
        if not os.path.exists(group_folder_path):
            os.makedirs(group_folder_path)

    def _highlights_json_path(self, group_id: str) -> str:
        return os.path.join(self.data_root, str(group_id), "highlights.json")

    def _load_highlights(self, group_id: str) -> list:
        path = self._highlights_json_path(group_id)
        if not os.path.isfile(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("entries", []) if isinstance(data, dict) else []
        except Exception as e:
            logger.error(f"读取精华列表失败: {e}")
            return []

    def _save_highlights(self, group_id: str, entries: list):
        path = self._highlights_json_path(group_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"entries": entries}, f, ensure_ascii=False, indent=2)

    def _random_highlight(self, group_id: str):
        entries = self._load_highlights(group_id)
        if not entries:
            return None
        return random.choice(entries)

    def _append_highlight(self, group_id: str, entry: dict):
        entries = self._load_highlights(group_id)
        entries.append(entry)
        self._save_highlights(group_id, entries)

    def _parse_copy_group_command(self, msg: str) -> Optional[str]:
        stripped = msg.strip()
        normalized = stripped.replace(" ", "")
        if not (stripped.startswith("精华复制") or stripped.startswith("/精华复制")):
            return None
        m = re.match(r"^/?精华复制(?:\s+|\+|＋)?(\d+)$", stripped)
        if not m:
            # 兼容旧格式：去空格后再匹配一次
            m = re.match(r"^/?精华复制(?:\+|＋)?(\d+)$", normalized)
        if not m:
            return ""
        return m.group(1)

    def _copy_highlights_from_group(self, source_group_id: str, target_group_id: str) -> Tuple[int, int]:
        source_entries = self._load_highlights(source_group_id)
        if not source_entries:
            return 0, 0

        self.create_group_folder(target_group_id)
        target_entries = self._load_highlights(target_group_id)
        copied_images = 0
        copied_entries = []

        for entry in source_entries:
            new_entry = dict(entry)
            new_entry["id"] = str(uuid.uuid4())

            if new_entry.get("type") == "image" and new_entry.get("path"):
                src_path = os.path.join(self.data_root, str(source_group_id), new_entry["path"])
                if not os.path.isfile(src_path):
                    continue
                ext = os.path.splitext(new_entry["path"])[1] or ".jpg"
                new_name = f"image_copy_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}{ext}"
                dst_path = os.path.join(self.data_root, str(target_group_id), new_name)
                try:
                    shutil.copy2(src_path, dst_path)
                except Exception as e:
                    logger.warning(f"复制图片失败 {src_path} -> {dst_path}: {e}")
                    continue
                new_entry["path"] = new_name
                copied_images += 1

            copied_entries.append(new_entry)

        if copied_entries:
            target_entries.extend(copied_entries)
            self._save_highlights(target_group_id, target_entries)
        return len(copied_entries), copied_images

    def _parse_delete_one_command(self, msg: str) -> Optional[int]:
        stripped = msg.strip()
        normalized = stripped.replace(" ", "")
        if not (stripped.startswith("删除精华") or stripped.startswith("/删除精华")):
            return None
        m = re.match(r"^/?删除精华(?:\s+|\+|＋)?(\d+)$", stripped)
        if not m:
            # 兼容旧格式：去空格后再匹配一次
            m = re.match(r"^/?删除精华(?:\+|＋)?(\d+)$", normalized)
        if not m:
            return -1
        return int(m.group(1))

    def _delete_highlight_by_index(self, group_id: str, index: int) -> Tuple[bool, str]:
        entries = self._load_highlights(group_id)
        total = len(entries)
        if total <= 0:
            return False, "⭐本群还没有精华可删除。"
        if index < 1 or index > total:
            return False, f"⭐编号超出范围，请输入 1 到 {total} 之间的数字。"

        target = entries[index - 1]
        removed_image = 0
        if target.get("type") == "image" and target.get("path"):
            img_path = os.path.join(self.data_root, str(group_id), target["path"])
            if os.path.isfile(img_path):
                try:
                    os.remove(img_path)
                    removed_image = 1
                except Exception as e:
                    logger.warning(f"删除精华图片失败 {img_path}: {e}")

        entries.pop(index - 1)
        self._save_highlights(group_id, entries)
        if removed_image:
            return True, f"⭐已删除精华 #{index}（并清理对应图片文件）。"
        return True, f"⭐已删除精华 #{index}。"

    def _clear_group_highlights(self, group_id: str) -> Tuple[int, int]:
        entries = self._load_highlights(group_id)
        count = len(entries)
        group_dir = os.path.join(self.data_root, str(group_id))
        removed_files = 0
        if os.path.isdir(group_dir):
            for name in os.listdir(group_dir):
                if name == "admin_settings.yml":
                    continue
                path = os.path.join(group_dir, name)
                if os.path.isfile(path):
                    try:
                        os.remove(path)
                        removed_files += 1
                    except Exception as e:
                        logger.warning(f"删除文件失败 {path}: {e}")
        self._save_highlights(group_id, [])
        return count, removed_files

    def is_admin(self, user_id):
        return str(user_id) in self.admins

    def _create_admin_settings_file(self):
        try:
            default_data = {"mode": 0}
            with open(self.admin_settings_path, "w", encoding="utf-8") as f:
                yaml.dump(default_data, f)
        except Exception as e:
            self.context.logger.error(f"创建模式文件失败: {str(e)}")

    def _load_admin_settings(self):
        try:
            with open(self.admin_settings_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return data
        except Exception as e:
            self.context.logger.error(f"加载模式数据失败: {str(e)}")
            return {}

    def _save_admin_settings(self):
        try:
            with open(self.admin_settings_path, "w", encoding="utf-8") as f:
                yaml.dump(self.admin_settings, f, allow_unicode=True)
        except Exception as e:
            self.context.logger.error(f"保存模式数据失败: {str(e)}")

    def gain_mode(self, event):
        value = None
        msg = event.message_str.strip()
        if msg:
            match = re.search(r"[-+]?\d*\.?\d+", msg)
            if match:
                value = match.group()
        return value

    def _extract_sender_name_from_get_msg(self, reply_msg: dict) -> str:
        if not isinstance(reply_msg, dict):
            return ""
        sender = reply_msg.get("sender", {})
        if not isinstance(sender, dict):
            return ""
        return (
            sender.get("card")
            or sender.get("nickname")
            or sender.get("title")
            or sender.get("name")
            or ""
        )

    def _extract_sender_name_from_event(self, event: AstrMessageEvent) -> str:
        sender = getattr(event.message_obj, "sender", None)
        if isinstance(sender, dict):
            return (
                sender.get("card")
                or sender.get("nickname")
                or sender.get("title")
                or sender.get("name")
                or str(event.get_sender_id())
            )
        return str(event.get_sender_id())

    async def _resolve_sender_name(
        self, event: AstrMessageEvent, group_id: str, user_id: str
    ) -> str:
        # 先走事件对象内已有字段（跨平台最通用）
        name = self._extract_sender_name_from_event(event).strip()
        if name and name != str(user_id):
            return name

        # 再尝试从原始消息结构提取
        raw_message = getattr(event.message_obj, "raw_message", {})
        if isinstance(raw_message, dict):
            sender = raw_message.get("sender", {})
            if isinstance(sender, dict):
                name = (
                    sender.get("card")
                    or sender.get("nickname")
                    or sender.get("title")
                    or sender.get("name")
                    or ""
                ).strip()
                if name and name != str(user_id):
                    return name

        # QQ 适配器兜底：查群成员资料，拿群名片/昵称
        try:
            member = await event.bot.api.call_action(
                "get_group_member_info",
                group_id=int(group_id) if str(group_id).isdigit() else group_id,
                user_id=int(user_id) if str(user_id).isdigit() else user_id,
                no_cache=True,
            )
            if isinstance(member, dict):
                name = (member.get("card") or member.get("nickname") or "").strip()
                if name:
                    return name
        except Exception as e:
            logger.debug(f"获取群成员名称失败，回退 user_id: {e}")

        return str(user_id)

    def _extract_sender_avatar_url(self, sender: dict, user_id: str = "") -> str:
        if not isinstance(sender, dict):
            sender = {}
        avatar_url = (
            sender.get("avatar")
            or sender.get("avatar_url")
            or sender.get("face")
            or sender.get("head")
            or sender.get("img_url")
            or ""
        )
        if isinstance(avatar_url, str) and avatar_url.strip():
            return avatar_url.strip()
        if str(user_id).isdigit():
            return f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100"
        return ""

    async def _save_avatar_from_url(
        self, group_id: str, user_id: str, avatar_url: str
    ) -> Optional[str]:
        if not avatar_url:
            return None
        try:
            out_dir = os.path.join(self.data_root, str(group_id))
            os.makedirs(out_dir, exist_ok=True)
            ext = ".jpg"
            low = avatar_url.lower()
            if ".png" in low:
                ext = ".png"
            elif ".webp" in low:
                ext = ".webp"
            out_name = f"avatar_{user_id}_{int(time.time() * 1000)}{ext}"
            out_path = os.path.join(out_dir, out_name)
            async with aiohttp.ClientSession() as session:
                async with session.get(avatar_url, timeout=10) as response:
                    if response.status != 200:
                        return None
                    data = await response.read()
            with open(out_path, "wb") as f:
                f.write(data)
            return out_name
        except Exception:
            return None

    def _parse_paged_command(self, msg: str, aliases: Tuple[str, ...]) -> Optional[int]:
        stripped = msg.strip()
        normalized = stripped.replace(" ", "")
        for alias in aliases:
            alias_clean = alias.strip()
            alias_normalized = alias_clean.replace(" ", "")
            if normalized == alias_normalized:
                return 1
            # 推荐新格式：/精华图 2
            m = re.match(rf"^{re.escape(alias_clean)}\s+(\d+)$", stripped)
            if m:
                return max(1, int(m.group(1)))
            # 兼容旧格式：/精华图2
            if normalized.startswith(alias_normalized):
                tail = normalized[len(alias_normalized) :]
                if tail.isdigit():
                    return max(1, int(tail))
        return None

    async def _save_bytes_as_image(self, group_id: str, data: bytes) -> str:
        filename = f"image_{int(time.time() * 1000)}.jpg"
        file_path = os.path.join(self.data_root, group_id, filename)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "wb") as f:
            f.write(data)
        logger.info(f"图片已保存到 {file_path}")
        return file_path

    async def download_image(
        self,
        event: AstrMessageEvent,
        file_id: Optional[str],
        group_id: str,
        image_comp: Optional[Image] = None,
    ) -> Optional[str]:
        try:
            client = event.bot
            payloads = {"file_id": file_id} if file_id else {}
            download_by_api_failed = 0
            download_by_file_failed = 0
            message_obj = event.message_obj
            image_obj = image_comp
            if image_obj is None:
                for i in message_obj.message:
                    if isinstance(i, Image):
                        image_obj = i
                        break
            result = {}
            if image_obj:
                file_path = await image_obj.convert_to_file_path()
                if file_path:
                    logger.info(f"尝试从本地缓存{file_path}读取图片")
                    try:
                        with open(file_path, "rb") as f:
                            data = f.read()
                        return await self._save_bytes_as_image(group_id, data)
                    except Exception as e:
                        download_by_file_failed = 1
                        logger.error(f"在读取本地缓存时遇到问题: {str(e)}")
                else:
                    download_by_file_failed = 1
            else:
                download_by_file_failed = 1

            if download_by_file_failed == 1 and payloads:
                try:
                    result = await client.api.call_action("get_image", **payloads)
                except Exception as e:
                    logger.warning(f"调用 get_image 失败，可能是非 QQ 平台: {e}")
                    result = {}
                file_path = result.get("file") if isinstance(result, dict) else None
                if file_path and os.path.exists(file_path):
                    logger.info(f"尝试从协议端api返回的路径{file_path}读取图片")
                    try:
                        with open(file_path, "rb") as f:
                            data = f.read()
                        return await self._save_bytes_as_image(group_id, data)
                    except Exception as e:
                        download_by_api_failed = 1
                        logger.error(f"在通过api下载图片时遇到问题: {str(e)}")
                else:
                    download_by_api_failed = 1

            if download_by_api_failed == 1 and download_by_file_failed == 1:
                url = result.get("url") if isinstance(result, dict) else None
                if not url and image_obj:
                    # 兼容非 QQ 适配器：优先尝试组件上的 URL 字段
                    url = getattr(image_obj, "url", None)
                if url:
                    logger.info(f"尝试从URL下载图片: {url}")
                    try:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(url) as response:
                                if response.status == 200:
                                    data = await response.read()
                                    return await self._save_bytes_as_image(group_id, data)
                                logger.error(f"从URL下载图片失败: HTTP {response.status}")
                    except Exception as e:
                        logger.error(f"从URL下载出错: {str(e)}")
                else:
                    logger.error("API返回结果中没有URL，无法下载")
        except Exception as e:
            raise Exception(f"{str(e)}")
        return None

    def _parse_cq_plain_text(self, chain) -> str:
        """从 get_msg 返回的 message 链中提取纯文本。"""
        if not chain:
            return ""
        parts = []
        if isinstance(chain, str):
            s = re.sub(r"\[CQ:[^\]]+\]", "", chain)
            return s.strip()
        if isinstance(chain, list):
            for part in chain:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(part.get("data", {}).get("text", ""))
        return "".join(parts).strip()

    async def _get_reply_text_and_image(self, event: AstrMessageEvent, reply_comp: Reply):
        file_id = None
        plain = ""
        sender_name = ""
        sender_id = ""
        sender_avatar = None
        try:
            reply_id = int(reply_comp.id) if str(reply_comp.id).isdigit() else reply_comp.id
            reply_msg = await event.bot.api.call_action("get_msg", message_id=reply_id)
            if reply_msg and "message" in reply_msg:
                sender_name = self._extract_sender_name_from_get_msg(reply_msg)
                sender = reply_msg.get("sender", {}) if isinstance(reply_msg, dict) else {}
                if isinstance(sender, dict):
                    sender_id = str(
                        sender.get("user_id")
                        or sender.get("uid")
                        or sender.get("id")
                        or ""
                    )
                    avatar_url = self._extract_sender_avatar_url(sender, sender_id)
                    sender_avatar = await self._save_avatar_from_url(
                        str(event.message_obj.group_id), sender_id or "unknown", avatar_url
                    )
                chain = reply_msg["message"]
                if isinstance(chain, list):
                    for part in chain:
                        if isinstance(part, dict):
                            if part.get("type") == "image":
                                file_id = part.get("data", {}).get("file")
                            elif part.get("type") == "text":
                                plain += part.get("data", {}).get("text", "")
                elif isinstance(chain, str):
                    plain = self._parse_cq_plain_text(chain)
                    m = re.search(r"\[CQ:image,[^\]]*file=([^,\]]+)", chain)
                    if m:
                        file_id = m.group(1)
        except Exception as e:
            logger.error(f"获取引用消息失败: {e}")
        return plain.strip(), file_id, sender_name, sender_id, sender_avatar

    def _paginate_entries(self, entries: List[dict], page: int, page_size: int = 10):
        page = max(1, page)
        latest_first = list(reversed(entries))
        total = len(latest_first)
        total_pages = max(1, (total + page_size - 1) // page_size)
        if page > total_pages:
            page = total_pages
        start = (page - 1) * page_size
        end = start + page_size
        return latest_first[start:end], total_pages, page

    def _build_highlights_image(self, group_id: str, page: int = 1) -> Optional[str]:
        if PILImage is None or ImageDraw is None:
            return None
        entries = self._load_highlights(group_id)
        if not entries:
            return None

        page_entries, total_pages, current_page = self._paginate_entries(entries, page, 10)
        margin = 24
        card_gap = 18
        inner = 18
        img_w = 1080
        avatar_size = 54
        title_size = 34
        sender_size = 22
        body_size = 24
        meta_size = 18
        font_body = _pick_cjk_font(body_size)
        if font_body is None:
            return None
        font_title = _pick_cjk_font(title_size) or font_body
        font_sender = _pick_cjk_font(sender_size) or font_body
        font_meta = _pick_cjk_font(meta_size) or font_body

        dummy = ImageDraw.Draw(PILImage.new("RGB", (10, 10), (255, 255, 255)))

        def text_h(text: str, font) -> int:
            bbox = dummy.textbbox((0, 0), text if text else " ", font=font)
            return bbox[3] - bbox[1]

        content_w = img_w - margin * 2
        body_w = content_w - inner * 2
        max_text_chars = 36
        max_img_h = 380
        min_card_h = 230

        card_plan = []
        for idx, e in enumerate(page_entries, 1):
            sender_name = (
                e.get("origin_sender_name")
                or e.get("sender_name")
                or str(e.get("origin_sender_id") or e.get("user_id") or "未知用户")
            )
            submitter = e.get("submitter_name") or sender_name
            submit_time = e.get("submit_time")
            if isinstance(submit_time, (int, float)):
                submit_time_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(submit_time))
            else:
                submit_time_str = "未知时间"
            footer = f"投稿人：{submitter}  ·  {submit_time_str}"
            global_idx = len(entries) - ((current_page - 1) * 10 + idx) + 1
            header_name = f"#{global_idx} {sender_name}"

            if e.get("type") == "image" and e.get("path"):
                img_path = os.path.join(self.data_root, str(group_id), e["path"])
                show_h = text_h("[图片加载失败]", font_body)
                if os.path.isfile(img_path):
                    try:
                        with PILImage.open(img_path) as src:
                            sw, sh = src.size
                        ratio = min(body_w / max(1, sw), max_img_h / max(1, sh), 1.0)
                        show_h = int(sh * ratio)
                    except Exception:
                        pass
                card_h = max(
                    min_card_h,
                    inner * 2 + avatar_size + inner + show_h + inner + text_h(footer, font_meta),
                )
                card_plan.append(("image", e, header_name, footer, card_h))
            else:
                raw = (e.get("text") or "").replace("\r\n", "\n").strip() or "[空文本]"
                lines = []
                for line in raw.split("\n"):
                    lines.extend(
                        textwrap.wrap(line, width=max_text_chars, break_long_words=True, break_on_hyphens=False) or [""]
                    )
                line_h = text_h("测试", font_body) + 8
                text_total_h = max(1, len(lines)) * line_h
                card_h = max(
                    min_card_h,
                    inner * 2 + avatar_size + inner + text_total_h + inner + text_h(footer, font_meta),
                )
                card_plan.append(("text", e, header_name, footer, card_h, lines, line_h))

        head_h = text_h("本群精华汇总", font_title) + text_h("meta", font_meta) + 14
        cards_h = sum(c[4] for c in card_plan) + card_gap * max(0, len(card_plan) - 1)
        footer_h = text_h("tips", font_meta) + 8
        img_h = max(500, margin * 2 + head_h + cards_h + footer_h)

        img = PILImage.new("RGB", (img_w, img_h), (245, 247, 252))
        draw = ImageDraw.Draw(img)
        y = margin
        draw.text((margin, y), "本群精华汇总", fill=(25, 30, 40), font=font_title)
        y += text_h("本群精华汇总", font_title) + 6
        draw.text(
            (margin, y),
            f"共 {len(entries)} 条  |  第 {current_page}/{total_pages} 页  |  每页 10 条（最新在前）",
            fill=(90, 96, 108),
            font=font_meta,
        )
        y += text_h("meta", font_meta) + 12

        for item in card_plan:
            card_type = item[0]
            e = item[1]
            header_name = item[2]
            footer = item[3]
            card_h = item[4]
            x1, y1 = margin, y
            x2, y2 = margin + content_w, y + card_h
            draw.rounded_rectangle([x1, y1, x2, y2], radius=14, fill=(255, 255, 255), outline=(225, 229, 238), width=2)

            top_x = x1 + inner
            top_y = y1 + inner
            avatar_path = e.get("origin_sender_avatar") or ""
            avatar_full = os.path.join(self.data_root, str(group_id), avatar_path) if avatar_path else ""
            avatar_drawn = False
            if avatar_full and os.path.isfile(avatar_full):
                try:
                    with PILImage.open(avatar_full) as av:
                        av = av.convert("RGB").resize((avatar_size, avatar_size))
                        mask = PILImage.new("L", (avatar_size, avatar_size), 0)
                        ImageDraw.Draw(mask).ellipse((0, 0, avatar_size, avatar_size), fill=255)
                        img.paste(av, (top_x, top_y), mask)
                        avatar_drawn = True
                except Exception:
                    avatar_drawn = False
            if not avatar_drawn:
                draw.ellipse((top_x, top_y, top_x + avatar_size, top_y + avatar_size), fill=(222, 230, 245))
                draw.text((top_x + 16, top_y + 13), "?", fill=(80, 92, 120), font=font_sender)
            draw.text((top_x + avatar_size + 12, top_y + 12), header_name, fill=(66, 72, 84), font=font_sender)

            body_top = top_y + avatar_size + inner
            body_bottom = y2 - inner - text_h(footer, font_meta)
            body_h = max(20, body_bottom - body_top)

            if card_type == "image":
                img_path = os.path.join(self.data_root, str(group_id), e.get("path", ""))
                if os.path.isfile(img_path):
                    try:
                        with PILImage.open(img_path) as src:
                            src = src.convert("RGB")
                            ratio = min(body_w / max(1, src.width), body_h / max(1, src.height), 1.0)
                            nw = max(1, int(src.width * ratio))
                            nh = max(1, int(src.height * ratio))
                            resized = src.resize((nw, nh))
                            px = x1 + inner + (body_w - nw) // 2
                            py = body_top + (body_h - nh) // 2
                            img.paste(resized, (px, py))
                    except Exception:
                        draw.text((x1 + inner, body_top), "[图片加载失败]", fill=(42, 46, 54), font=font_body)
                else:
                    draw.text((x1 + inner, body_top), "[图片文件已丢失]", fill=(42, 46, 54), font=font_body)
            else:
                lines = item[5]
                line_h = item[6]
                total_h = max(1, len(lines)) * line_h
                ty = body_top + max(0, (body_h - total_h) // 2)
                for line in lines:
                    draw.text((x1 + inner, ty), line, fill=(42, 46, 54), font=font_body)
                    ty += line_h

            foot_w = draw.textlength(footer, font=font_meta)
            draw.text((x2 - inner - foot_w, y2 - inner - text_h(footer, font_meta)), footer, fill=(100, 106, 118), font=font_meta)
            y += card_h + card_gap

        draw.text((margin, y), "翻页示例：精华图 2  或  /精华图 3", fill=(110, 118, 130), font=font_meta)

        out_dir = os.path.join(self.data_root, str(group_id))
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"highlights_summary_{int(time.time())}.png")
        img.save(out_path, format="PNG")
        return out_path

    def _can_submit(self, user_id: str) -> bool:
        current_mode = self.admin_settings.get("mode", 0)
        if current_mode == 0:
            return False
        if current_mode == 1:
            return self.is_admin(user_id)
        return True

    @event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        message_obj = event.message_obj
        raw_message = (
            message_obj.raw_message
            if isinstance(message_obj.raw_message, dict)
            else {}
        )
        msg = event.message_str.strip()
        group_folder_path = os.path.join(self.data_root, group_id)

        if not os.path.exists(group_folder_path):
            self.create_group_folder(group_id)
        self.admin_settings_path = os.path.join(group_folder_path, "admin_settings.yml")
        if not os.path.exists(self.admin_settings_path):
            self._create_admin_settings_file()
        self.admin_settings = self._load_admin_settings()

        page = self._parse_paged_command(msg, ("精华图", "/精华图", "精华列表", "/精华列表"))
        help_text = (
            "⭐精华插件指令一览\n"
            "1. /精华 或 精华：随机发送一条精华\n"
            "2. /精华图 或 精华图：查看精华汇总图（默认第1页）\n"
            "3. /精华图 2（数字可改）：查看精华汇总图指定页\n"
            "4. /精华列表 或 精华列表：与精华图相同\n"
            "5. 精华投稿 + 文字/图片：投稿精华\n"
            "6. 精华权限+模式数字：设置投稿权限（管理员）\n"
            "7. 戳戳冷却+秒数：设置戳一戳冷却（管理员）\n"
            "8. /删除精华 编号：删除指定编号的精华（管理员）\n"
            "9. /删除全部精华：清空当前群全部精华（管理员）\n"
            "10. /精华复制 群号：复制指定群的精华到当前群（管理员）"
        )

        copy_from_group = self._parse_copy_group_command(msg)
        delete_one_idx = self._parse_delete_one_command(msg)
        if msg in ("/精华帮助", "精华帮助"):
            yield event.plain_result(help_text)

        elif delete_one_idx is not None:
            if not self.is_admin(user_id):
                yield event.plain_result("权限不足，仅可由bot管理员执行")
                return
            if delete_one_idx <= 0:
                yield event.plain_result("⭐格式错误，请使用：/删除精华 编号")
                return
            ok, text = self._delete_highlight_by_index(group_id, delete_one_idx)
            yield event.plain_result(text)
            if not ok:
                return

        elif copy_from_group is not None:
            if not self.is_admin(user_id):
                yield event.plain_result("权限不足，仅可由bot管理员执行")
                return
            if copy_from_group == "":
                yield event.plain_result("⭐格式错误，请使用：/精华复制 群号")
                return
            if copy_from_group == group_id:
                yield event.plain_result("⭐不能从当前群复制到当前群，请填写其他群号。")
                return
            copied_entries, copied_images = self._copy_highlights_from_group(copy_from_group, group_id)
            if copied_entries <= 0:
                yield event.plain_result(f"⭐群 {copy_from_group} 没有可复制的精华，或复制失败。")
                return
            yield event.plain_result(
                f"⭐复制完成：从群 {copy_from_group} 导入 {copied_entries} 条精华到当前群，其中图片 {copied_images} 条。"
            )

        elif msg.startswith("精华权限"):
            if not self.is_admin(user_id):
                yield event.plain_result("权限不足，仅可由bot管理员设置")
                return
            set_mode = self.gain_mode(event)
            if not set_mode:
                yield event.plain_result(
                    f"⭐请输入「精华权限+数字」来设置\n  0：关闭投稿系统\n  1：仅管理员可投稿\n  2：全体成员均可投稿\n当前群聊权限设置为：{self.admin_settings.get('mode', 0)}"
                )
            else:
                if set_mode not in ["0", "1", "2"]:
                    yield event.plain_result(
                        "⭐模式数字范围出错！请输入正确的模式\n  0：关闭投稿系统\n  1：仅管理员可投稿\n  2：全体成员均可投稿"
                    )
                    return
                self.admin_settings["mode"] = int(set_mode)
                self._save_admin_settings()
                texts = "⭐精华权限设置成功，当前状态为："
                if self.admin_settings["mode"] == 0:
                    texts += "\n  0：关闭投稿系统"
                elif self.admin_settings["mode"] == 1:
                    texts += "\n  1：仅管理员可投稿"
                elif self.admin_settings["mode"] == 2:
                    texts += "\n  2：全体成员均可投稿"
                yield event.plain_result(texts)

        elif msg.startswith("戳戳冷却"):
            if not self.is_admin(user_id):
                yield event.plain_result("权限不足，仅可由bot管理员设置")
                return
            set_coldown = self.gain_mode(event)
            if not set_coldown:
                yield event.plain_result("⭐请输入「戳戳冷却+数字」来设置，单位为秒\n")
                return
            self.admin_settings["coldown"] = int(set_coldown)
            self._save_admin_settings()
            yield event.plain_result(f"⭐戳戳冷却设置成功，当前值为：{self.admin_settings['coldown']}秒")

        elif msg in ("删除全部精华", "/删除全部精华"):
            if not self.is_admin(user_id):
                yield event.plain_result("权限不足，仅可由bot管理员执行")
                return
            deleted_entries, deleted_files = self._clear_group_highlights(group_id)
            yield event.plain_result(
                f"⭐已清空本群全部精华，共删除 {deleted_entries} 条记录，清理 {deleted_files} 个文件。"
            )

        elif msg in ("/精华", "精华"):
            picked = self._random_highlight(group_id)
            if not picked:
                yield event.plain_result(
                    "⭐本群还没有精华哦~\n请发送「精华投稿」添加文字或图片精华！"
                )
                return
            if picked.get("type") == "image" and picked.get("path"):
                full = os.path.join(self.data_root, group_id, picked["path"])
                if os.path.isfile(full):
                    yield event.image_result(full)
                else:
                    yield event.plain_result("⭐该条图片文件已丢失，可删除后重新投稿。")
            elif picked.get("type") == "text" and picked.get("text"):
                yield event.plain_result(picked["text"])
            else:
                yield event.plain_result("⭐数据异常，请重新投稿。")

        elif page is not None:
            if PILImage is None:
                yield event.plain_result("⭐服务器未安装 Pillow，无法生成长图。请 pip install Pillow")
                return
            out = self._build_highlights_image(group_id, page=page)
            if not out:
                yield event.plain_result("⭐本群还没有精华，无法生成汇总图。")
            else:
                yield event.image_result(out)

        elif msg.startswith("精华投稿"):
            if not self._can_submit(user_id):
                current_mode = self.admin_settings.get("mode", 0)
                if current_mode == 0:
                    yield event.plain_result("⭐投稿系统未开启，请联系bot管理员发送「精华权限」来设置")
                else:
                    yield event.plain_result(
                        "⭐权限不足，当前为「仅管理员可投稿」\n可由管理员发送「精华权限」调整"
                    )
                return

            rest = msg[len("精华投稿") :].strip()
            current_sender_name = await self._resolve_sender_name(event, group_id, user_id)
            event_sender = getattr(event.message_obj, "sender", {})
            current_sender_avatar_url = self._extract_sender_avatar_url(event_sender, user_id)
            current_sender_avatar = await self._save_avatar_from_url(
                group_id, user_id, current_sender_avatar_url
            )

            messages = event.message_obj.message
            image_comp = next((m for m in messages if isinstance(m, Image)), None)
            reply_comp = next((m for m in messages if isinstance(m, Reply)), None)

            file_id = image_comp.file if image_comp else None
            reply_text = ""
            reply_sender_name = ""
            reply_sender_id = ""
            reply_sender_avatar = None
            if reply_comp:
                rt, rf, rs, ruid, rav = await self._get_reply_text_and_image(event, reply_comp)
                reply_text = rt
                reply_sender_name = rs
                reply_sender_id = ruid
                reply_sender_avatar = rav
                if not file_id and rf:
                    file_id = rf

            origin_sender_name = reply_sender_name or current_sender_name
            origin_sender_id = reply_sender_id or user_id
            origin_sender_avatar = reply_sender_avatar or current_sender_avatar
            submitter_name = current_sender_name
            text_to_save = rest
            if not text_to_save and reply_text:
                text_to_save = reply_text

            if file_id:
                try:
                    self.create_group_folder(group_id)
                    file_path = await self.download_image(event, file_id, group_id, image_comp)
                    msg_id = str(event.message_obj.message_id)
                    if file_path and os.path.exists(file_path):
                        rel = os.path.basename(file_path)
                        self._append_highlight(
                            group_id,
                            {
                                "id": str(uuid.uuid4()),
                                "type": "image",
                                "path": rel,
                                "text": None,
                                "user_id": user_id,
                                "sender_name": origin_sender_name,
                                "origin_sender_name": origin_sender_name,
                                "origin_sender_id": origin_sender_id,
                                "origin_sender_avatar": origin_sender_avatar,
                                "submitter_name": submitter_name,
                                "submitter_id": user_id,
                                "submit_time": int(time.time()),
                            },
                        )
                        chain = [Reply(id=msg_id), Plain(text="⭐精华投稿成功！（图片）")]
                        yield event.chain_result(chain)
                    else:
                        yield event.plain_result("⭐精华投稿失败，图片下载失败")
                except Exception as e:
                    logger.error(f"投稿过程出错: {e}")
                    yield event.plain_result(f"⭐投稿失败: {str(e)}")
                return

            if text_to_save:
                self.create_group_folder(group_id)
                self._append_highlight(
                    group_id,
                    {
                        "id": str(uuid.uuid4()),
                        "type": "text",
                        "path": None,
                        "text": text_to_save,
                        "user_id": user_id,
                        "sender_name": origin_sender_name,
                        "origin_sender_name": origin_sender_name,
                        "origin_sender_id": origin_sender_id,
                        "origin_sender_avatar": origin_sender_avatar,
                        "submitter_name": submitter_name,
                        "submitter_id": user_id,
                        "submit_time": int(time.time()),
                    },
                )
                msg_id = str(event.message_obj.message_id)
                chain = [Reply(id=msg_id), Plain(text="⭐精华投稿成功！（文字）")]
                yield event.chain_result(chain)
                return

            chain = [
                At(qq=user_id),
                Plain(
                    text="\n请发送「精华投稿」+文字，或带图发送，或引用消息发送「精华投稿」"
                ),
            ]
            yield event.chain_result(chain)

        if raw_message.get("post_type") == "notice" and raw_message.get("notice_type") == "notify" and raw_message.get("sub_type") == "poke":
            bot_id = raw_message.get("self_id")
            sender_id = raw_message.get("user_id")
            target_id = raw_message.get("target_id")
            if bot_id and sender_id and target_id:
                if not os.path.exists(group_folder_path):
                    self.create_group_folder(group_id)
                self.admin_settings_path = os.path.join(group_folder_path, "admin_settings.yml")
                if not os.path.exists(self.admin_settings_path):
                    self._create_admin_settings_file()
                self.admin_settings = self._load_admin_settings()
                cold_time = self.admin_settings.setdefault("coldown", 10)
                last_poke = self.admin_settings.setdefault("last_poke", 0)
                self._save_admin_settings()

                if time.time() - last_poke > cold_time:
                    self.admin_settings["last_poke"] = time.time()
                    self._save_admin_settings()
                    if str(target_id) == str(bot_id):
                        if random.random() < 0.85:
                            picked = self._random_highlight(group_id)
                            if not picked:
                                return
                            if picked.get("type") == "image" and picked.get("path"):
                                full = os.path.join(self.data_root, group_id, picked["path"])
                                if os.path.isfile(full):
                                    yield event.image_result(full)
                            elif picked.get("type") == "text" and picked.get("text"):
                                yield event.plain_result(picked["text"])
                        else:
                            texts = [
                                "\n再戳的话......说不定下一条就是你的！",
                                "\n我会一直一直看着你👀",
                                "\n给我出列！",
                            ]
                            selected_text = random.choice(texts)
                            chain = [At(qq=sender_id), Plain(text=selected_text)]
                            yield event.chain_result(chain)
                else:
                    remaining = cold_time - (time.time() - last_poke)
                    logger.info(f"精华功能冷却中，剩余{remaining:.0f}秒")
