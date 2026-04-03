import os
import random
import time
import json
import uuid
import yaml
import aiohttp
import re
import textwrap
from typing import List, Optional, Tuple
from astrbot import logger
from astrbot.core.message.components import Image, Reply, At, Plain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
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

    async def download_image(self, event: AstrMessageEvent, file_id: str, group_id) -> Optional[str]:
        try:
            assert isinstance(event, AiocqhttpMessageEvent)
            client = event.bot
            payloads = {"file_id": file_id}
            download_by_api_failed = 0
            download_by_file_failed = 0
            message_obj = event.message_obj
            image_obj = None
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
                        filename = f"image_{int(time.time() * 1000)}.jpg"
                        file_path = os.path.join(self.data_root, group_id, filename)
                        os.makedirs(os.path.dirname(file_path), exist_ok=True)
                        with open(file_path, "wb") as f:
                            f.write(data)
                        logger.info(f"图片已保存到 {file_path}")
                        return file_path
                    except Exception as e:
                        download_by_file_failed = 1
                        logger.error(f"在读取本地缓存时遇到问题: {str(e)}")
                else:
                    download_by_file_failed = 1
            else:
                download_by_file_failed = 1

            if download_by_file_failed == 1:
                result = await client.api.call_action("get_image", **payloads)
                file_path = result.get("file")
                if file_path and os.path.exists(file_path):
                    logger.info(f"尝试从协议端api返回的路径{file_path}读取图片")
                    try:
                        with open(file_path, "rb") as f:
                            data = f.read()
                        filename = f"image_{int(time.time() * 1000)}.jpg"
                        save_path = os.path.join(self.data_root, group_id, filename)
                        os.makedirs(os.path.dirname(save_path), exist_ok=True)
                        with open(save_path, "wb") as f:
                            f.write(data)
                        logger.info(f"图片已保存到 {save_path}")
                        return save_path
                    except Exception as e:
                        download_by_api_failed = 1
                        logger.error(f"在通过api下载图片时遇到问题: {str(e)}")
                else:
                    download_by_api_failed = 1

            if download_by_api_failed == 1 and download_by_file_failed == 1:
                url = result.get("url")
                if url:
                    logger.info(f"尝试从URL下载图片: {url}")
                    try:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(url) as response:
                                if response.status == 200:
                                    data = await response.read()
                                    filename = f"image_{int(time.time() * 1000)}.jpg"
                                    file_path = os.path.join(
                                        self.data_root, group_id, filename
                                    )
                                    os.makedirs(os.path.dirname(file_path), exist_ok=True)
                                    with open(file_path, "wb") as f:
                                        f.write(data)
                                    logger.info(f"图片已保存到 {file_path}")
                                    return file_path
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
        try:
            reply_id = int(reply_comp.id) if str(reply_comp.id).isdigit() else reply_comp.id
            reply_msg = await event.bot.api.call_action("get_msg", message_id=reply_id)
            if reply_msg and "message" in reply_msg:
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
        return plain.strip(), file_id

    def _build_highlights_image(self, group_id: str) -> Optional[str]:
        if PILImage is None or ImageDraw is None:
            return None
        entries = self._load_highlights(group_id)
        if not entries:
            return None

        margin = 24
        line_gap = 10
        title_size = 28
        body_size = 22
        img_w = 920
        font_body = _pick_cjk_font(body_size)
        if font_body is None:
            return None
        font_title = _pick_cjk_font(title_size) or font_body

        display_rows: List[Tuple[str, str]] = []
        display_rows.append(("title", "本群精华汇总"))
        display_rows.append(("body", f"共 {len(entries)} 条"))
        display_rows.append(("body", ""))

        for idx, e in enumerate(entries, 1):
            if e.get("type") == "text" and e.get("text"):
                raw = e["text"].replace("\r\n", "\n").strip()
                wrapped = textwrap.wrap(
                    raw,
                    width=38,
                    break_long_words=True,
                    break_on_hyphens=False,
                )
                if not wrapped:
                    wrapped = [""]
                for i, wline in enumerate(wrapped):
                    prefix = f"[{idx}] " if i == 0 else "      "
                    display_rows.append(("body", prefix + wline))
            else:
                display_rows.append(("body", f"[{idx}] [图片]"))
            display_rows.append(("body", ""))

        dummy = ImageDraw.Draw(PILImage.new("RGB", (10, 10), (255, 255, 255)))
        heights = []
        for kind, text in display_rows:
            use_font = font_title if kind == "title" else font_body
            t = text if text else " "
            bbox = dummy.textbbox((0, 0), t, font=use_font)
            heights.append(bbox[3] - bbox[1])

        total_h = margin * 2 + sum(heights) + line_gap * max(0, len(display_rows) - 1)
        img_h = max(320, total_h)
        img = PILImage.new("RGB", (img_w, img_h), (250, 250, 252))
        draw = ImageDraw.Draw(img)
        y = margin
        for i, (kind, text) in enumerate(display_rows):
            use_font = font_title if kind == "title" else font_body
            draw.text((margin, y), text, fill=(30, 30, 35), font=use_font)
            y += heights[i] + line_gap

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
        raw_message = message_obj.raw_message
        msg = event.message_str.strip()
        group_folder_path = os.path.join(self.data_root, group_id)

        if not os.path.exists(group_folder_path):
            self.create_group_folder(group_id)
        self.admin_settings_path = os.path.join(group_folder_path, "admin_settings.yml")
        if not os.path.exists(self.admin_settings_path):
            self._create_admin_settings_file()
        self.admin_settings = self._load_admin_settings()

        if msg.startswith("投稿权限"):
            if not self.is_admin(user_id):
                yield event.plain_result("权限不足，仅可由bot管理员设置")
                return
            set_mode = self.gain_mode(event)
            if not set_mode:
                yield event.plain_result(
                    f"⭐请输入「投稿权限+数字」来设置\n  0：关闭投稿系统\n  1：仅管理员可投稿\n  2：全体成员均可投稿\n当前群聊权限设置为：{self.admin_settings.get('mode', 0)}"
                )
            else:
                if set_mode not in ["0", "1", "2"]:
                    yield event.plain_result(
                        "⭐模式数字范围出错！请输入正确的模式\n  0：关闭投稿系统\n  1：仅管理员可投稿\n  2：全体成员均可投稿"
                    )
                    return
                self.admin_settings["mode"] = int(set_mode)
                self._save_admin_settings()
                texts = "⭐投稿权限设置成功，当前状态为："
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

        elif msg in ("精华图", "/精华图", "精华列表", "/精华列表"):
            if PILImage is None:
                yield event.plain_result("⭐服务器未安装 Pillow，无法生成长图。请 pip install Pillow")
                return
            out = self._build_highlights_image(group_id)
            if not out:
                yield event.plain_result("⭐本群还没有精华，无法生成汇总图。")
            else:
                yield event.image_result(out)

        elif msg.startswith("精华投稿"):
            if not self._can_submit(user_id):
                current_mode = self.admin_settings.get("mode", 0)
                if current_mode == 0:
                    yield event.plain_result("⭐投稿系统未开启，请联系bot管理员发送「投稿权限」来设置")
                else:
                    yield event.plain_result(
                        "⭐权限不足，当前为「仅管理员可投稿」\n可由管理员发送「投稿权限」调整"
                    )
                return

            rest = msg[len("精华投稿") :].strip()

            messages = event.message_obj.message
            image_comp = next((m for m in messages if isinstance(m, Image)), None)
            reply_comp = next((m for m in messages if isinstance(m, Reply)), None)

            file_id = image_comp.file if image_comp else None
            reply_text = ""
            if reply_comp:
                rt, rf = await self._get_reply_text_and_image(event, reply_comp)
                reply_text = rt
                if not file_id and rf:
                    file_id = rf

            text_to_save = rest
            if not text_to_save and reply_text:
                text_to_save = reply_text

            if file_id:
                try:
                    self.create_group_folder(group_id)
                    file_path = await self.download_image(event, file_id, group_id)
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
