#!/usr/bin/env python3
# Copyright (C) 2026 Soenchin
# SPDX-License-Identifier: AGPL-3.0
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
"""
QQ relay — dice + AI chat
Connects to OneBot v11 WebSocket, handles all group messages.
"""

import asyncio
import hashlib
import html
import io
import json
import os
import random
import re
import subprocess
import sys
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote
import time

# 手动加载 .env（免外部依赖）
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

import httpx

import websockets

import plugins

# ============ 配 置 ============
WS_URL = os.getenv("NAPCAT_WS_URL", "ws://127.0.0.1:3001")
TOKEN = os.getenv("NAPCAT_TOKEN", "")

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic")
DEEPSEEK_MODEL = "deepseek-v4-flash"

BOT_NAME = os.getenv("BOT_NAME", "QQ Bot")

MASTER_QQ = int(os.getenv("MASTER_QQ", "0"))

# 群模式分流 — 通过环境变量 GROUP_MODE 配置 JSON，如：
#   {"123456789":"direct","987654321":"pipe"}
GROUP_MODE_RAW = os.getenv("GROUP_MODE", "")
if GROUP_MODE_RAW:
    GROUP_MODE = {int(k): v for k, v in json.loads(GROUP_MODE_RAW).items()}
else:
    GROUP_MODE = {}
FALLBACK_MODE = os.getenv("FALLBACK_MODE", "direct")
PIPE_GROUPS = [gid for gid, mode in GROUP_MODE.items() if mode == "pipe"]

# 管道群 session UUID（用群号生成固定 UUID，每个群独立）
def pipe_session_id(gid: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"qq-group-{gid}"))

# 管道进程配置
CLAUDE_CMD = os.getenv("CLAUDE_CMD", "claude")
PIPE_ADD_DIR = Path(os.getenv("PIPE_ADD_DIR", str(Path(__file__).parent / "memory")))
PIPE_WORK_DIR = PIPE_ADD_DIR  # 跟 --add-dir 一致，物理隔离
PIPE_ALLOWED_TOOLS = "WebSearch,Read,Glob"
PIPE_ALLOWED_TOOLS_ADMIN = "WebSearch,Read,Edit,Write,Bash,Grep,Glob"

MEM_DIR = Path(os.getenv("MEM_DIR", str(Path(__file__).parent / "memory")))
MAX_HISTORY = 50

# 人设 + 知识库
PERSONA_FILE = MEM_DIR / "persona.md"
KNOWLEDGE_DIR = MEM_DIR / "knowledge"
MEMORY_DIR = MEM_DIR / "memory"

# 表情包
MEME_DIR = MEM_DIR / "memes"
MEME_ARCHIVE_DIR = MEME_DIR / "archive"
MEME_UNSORTED_DIR = MEME_DIR / "unsorted"
MEME_SERVER_PORT = int(os.getenv("MEME_SERVER_PORT", "8801"))
MEME_PROMPT_EXTRA = (
    "\n\n你有表情包可以用。规则："
    "先 Read memes/archive/index.md 筛图，"
    "觉得合适直接在回复末尾带一张 CQ 码：[CQ:image,file=http://host.docker.internal:8801/archive/文件名]。"
    "**不要聊图本身，不要解释，自然带过去就行**。"
    "每次最多一张图。"
)

# 管道读图（默认关；GROUP_VISION 为开启读图的群号 JSON 数组，如 [732123758]）
GROUP_VISION_RAW = os.getenv("GROUP_VISION", "")
if GROUP_VISION_RAW:
    try:
        GROUP_VISION = {int(x) for x in json.loads(GROUP_VISION_RAW)}
    except Exception:
        GROUP_VISION = set()
        print(f"[中繼] GROUP_VISION 解析失败，已忽略: {GROUP_VISION_RAW[:120]}")
else:
    GROUP_VISION = set()

INBOX_DIR = PIPE_ADD_DIR / "inbox"
INBOX_MAX_PER_GROUP = 5
INBOX_TTL_SEC = 30 * 60
VISION_READ_MAX = 3
IMAGE_COMPRESS_BYTES = int(1.5 * 1024 * 1024)
IMAGE_MAX_SIDE = 1280
CQ_IMAGE_RE = re.compile(r"\[CQ:image,([^\]]*)\]", re.IGNORECASE)
# 中继侧 vision 描述后塞进窗口；管道只吃文字，不再要求 CLI Read 图片
VISION_CAPTION_MAX = 80


def _normalize_media_url(url: str) -> str:
    """CQ/日志里常见 &amp; 未反解，直接请求会 400。"""
    if not url:
        return ""
    u = html.unescape(url.strip())
    # 再解一轮，防止双重实体
    u = html.unescape(u)
    try:
        from urllib.parse import unquote
        # 只解 %XX，不动已是明文的 &
        if "%" in u:
            u = unquote(u)
    except Exception:
        pass
    return u


def _parse_cq_params(param_str: str) -> dict:
    out = {}
    for part in param_str.split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        key = k.strip().lower()
        val = v.strip()
        if key in ("url", "file"):
            val = _normalize_media_url(val)
        out[key] = val
    return out


def extract_image_refs(text: str) -> list[dict]:
    """从 raw_message 抽图片引用：优先 url，其次 file 名（给 get_image 兜底）。"""
    refs = []
    seen = set()
    for m in CQ_IMAGE_RE.finditer(text or ""):
        params = _parse_cq_params(m.group(1))
        url = params.get("url") or ""
        file_id = params.get("file") or ""
        if url and not (url.startswith("http://") or url.startswith("https://")):
            url = ""
        key = url or file_id
        if not key or key in seen:
            continue
        seen.add(key)
        refs.append({"url": url, "file": file_id})
    return refs


def extract_image_urls(text: str) -> list[str]:
    """兼容旧调用：只返回 http(s) url。"""
    return [r["url"] for r in extract_image_refs(text) if r.get("url")]


def strip_cq_images(text: str) -> str:
    cleaned = CQ_IMAGE_RE.sub(" ", text or "")
    return re.sub(r"\s+", " ", cleaned).strip()


def _guess_ext(content_type: str, url: str = "") -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/bmp": ".bmp",
    }
    if ct in mapping:
        return mapping[ct]
    from urllib.parse import urlparse
    path = urlparse(url or "").path.lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"):
        if path.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return ".jpg"


def _process_image_bytes(data: bytes, content_type: str) -> tuple[bytes, str]:
    """GIF 取首帧；过大则压到长边 1280，并继续降质/缩边直到 <=1.5MB 或触底。

    不会死循环：quality 与缩边次数都有硬上限。压不下去也返回当前最小结果，不卡死。
    """
    try:
        from PIL import Image
    except ImportError:
        print(f"[读图] 无 Pillow，跳过压缩 raw={len(data)} bytes")
        return data, _guess_ext(content_type)

    try:
        im = Image.open(io.BytesIO(data))
        im.load()  # 强制解码，避免后续 lazy 读踩坑
        src_fmt = (im.format or "").upper()
        is_gif = src_fmt == "GIF" or "gif" in (content_type or "").lower()
        if getattr(im, "n_frames", 1) and getattr(im, "n_frames", 1) > 1:
            # 动图只取首帧，避免整段 GIF 巨大
            im.seek(0)
            is_gif = True

        # 统一转可存格式；带透明 PNG 若过大最终也会转 JPEG
        if is_gif:
            im = im.convert("RGB")
            prefer_png = False
        elif im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            # 先保留 alpha 走 PNG；若超限再摊成白底 JPEG
            im = im.convert("RGBA")
            prefer_png = True
        else:
            im = im.convert("RGB")
            prefer_png = False

        def _resize_long_side(image, max_side: int):
            w, h = image.size
            m = max(w, h)
            if m <= max_side:
                return image
            scale = max_side / float(m)
            nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
            return image.resize((nw, nh), Image.Resampling.LANCZOS)

        def _jpeg_bytes(image, quality: int) -> bytes:
            work = image
            if work.mode == "RGBA":
                bg = Image.new("RGB", work.size, (255, 255, 255))
                bg.paste(work, mask=work.split()[-1])
                work = bg
            elif work.mode not in ("RGB", "L"):
                work = work.convert("RGB")
            buf = io.BytesIO()
            # optimize 大图很慢；超大时关掉，避免事件循环体感卡死
            optimize = len(data) < 8 * 1024 * 1024
            work.save(buf, format="JPEG", quality=quality, optimize=optimize)
            return buf.getvalue()

        def _png_bytes(image) -> bytes:
            buf = io.BytesIO()
            image.save(buf, format="PNG", optimize=True)
            return buf.getvalue()

        # 1) 先压到长边上限
        im = _resize_long_side(im, IMAGE_MAX_SIDE)
        original_len = len(data)
        need_compress = original_len > IMAGE_COMPRESS_BYTES or is_gif
        if not need_compress and not prefer_png:
            return data, _guess_ext(content_type)

        # 2) 先尝试目标格式
        if prefer_png:
            out = _png_bytes(im)
            if len(out) <= IMAGE_COMPRESS_BYTES:
                print(f"[读图] 压缩完成 png {original_len}->{len(out)} bytes size={im.size}")
                return out, ".png"
            # PNG 仍超限：转 JPEG 继续压
            print(f"[读图] PNG 仍超限 {len(out)} bytes，改 JPEG 继续压")
            prefer_png = False

        quality = 85
        out = _jpeg_bytes(im, quality)
        # quality 循环：85 -> 75 -> ... -> 35，最多约 6 次，必退出
        while len(out) > IMAGE_COMPRESS_BYTES and quality > 35:
            quality -= 10
            out = _jpeg_bytes(im, quality)

        # 3) 质量触底仍超限：继续缩边（0.8x），最多 8 次，最小长边 320
        shrink_guard = 0
        max_side = max(im.size)
        while len(out) > IMAGE_COMPRESS_BYTES and max_side > 320 and shrink_guard < 8:
            shrink_guard += 1
            max_side = max(320, int(max_side * 0.8))
            im = _resize_long_side(im, max_side)
            out = _jpeg_bytes(im, max(quality, 40))
            print(f"[读图] 二次缩边#{shrink_guard} max_side={max_side} -> {len(out)} bytes")

        if len(out) > IMAGE_COMPRESS_BYTES:
            print(
                f"[读图] 警告: 压后仍超限 {len(out)}>{IMAGE_COMPRESS_BYTES} "
                f"(raw={original_len}, size={im.size}, q={quality})，返回当前结果不阻塞"
            )
        else:
            print(f"[读图] 压缩完成 jpg {original_len}->{len(out)} bytes size={im.size} q={quality}")
        return out, ".jpg"
    except Exception as e:
        print(f"[读图] 图片处理失败，使用原文件: {e}")
        return data, _guess_ext(content_type)


def list_image_markers(text: str) -> list[str]:
    return re.findall(r"\[图片:\s*([^\]]+)\]", text or "")


def collect_recent_image_paths(recent_lines, limit: int = VISION_READ_MAX) -> list[str]:
    paths = []
    for line in reversed(list(recent_lines)):
        for p in list_image_markers(line):
            p = p.strip()
            if p and p not in paths:
                paths.append(p)
            if len(paths) >= limit:
                return list(reversed(paths))
    return list(reversed(paths))


def load_persona() -> str:
    parts = []
    if PERSONA_FILE.exists():
        text = PERSONA_FILE.read_text(encoding="utf-8").strip()
        if text.startswith("---"):
            p2 = text.split("---", 2)
            if len(p2) >= 3:
                text = p2[2].strip()
        if text:
            parts.append(text)
    if not parts:
        parts.append(f"你是 {BOT_NAME}，绿发兽耳少年。性格大大咧咧，直率但不失礼貌。用中文回复。")
    if KNOWLEDGE_DIR.exists():
        kb = []
        for f in sorted(KNOWLEDGE_DIR.glob("*.md")):
            c = f.read_text(encoding="utf-8").strip()
            if c:
                kb.append(f"## {f.stem}\n{c}")
        if kb:
            parts.append("--- 知识库 ---\n" + "\n\n".join(kb))
    if MEMORY_DIR.exists():
        mb = []
        for f in sorted(MEMORY_DIR.glob("*.md")):
            c = f.read_text(encoding="utf-8").strip()
            if c:
                mb.append(f"## {f.stem}\n{c}")
        if mb:
            parts.append("--- 记忆 ---\n" + "\n\n".join(mb))
    return "\n\n".join(parts)


class RelayBot:
    def __init__(self, eventbus=None):
        self.ws = None
        self.bot_qq = None
        self.http = httpx.AsyncClient(
            timeout=60.0,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            },
            follow_redirects=True,
        )
        self._api_futures = {}  # echo -> Future，OneBot 同步调用
        self.eventbus = eventbus
        self._start_time = time.time()
        self.persona = load_persona()
        self.pipe_persona = (PERSONA_FILE.read_text(encoding="utf-8").strip()
                             if PERSONA_FILE.exists() else f"你是 {BOT_NAME}，绿发兽耳少年。性格大大咧咧，直率但不失礼貌。用中文回复。")
        # 去重：记录最近处理过的 message_id
        self._seen_ids = set()
        self._seen_queue = deque()
        self._seen_max = 500
        (MEM_DIR / "conv").mkdir(parents=True, exist_ok=True)
        # 群组配置（从模块常量复制到实例，供 webui 读取）
        self.GROUP_MODE = dict(GROUP_MODE)
        self.PIPE_GROUPS = list(PIPE_GROUPS)
        self.FALLBACK_MODE = FALLBACK_MODE
        self.GROUP_VISION = set(GROUP_VISION)  # 开启管道读图的群
        # 主动发言计数器（仅管道群）
        self._pipe_counters = {}   # {gid: 当前计数}
        self._pipe_thresholds = {} # {gid: 触发阈值}
        self._pipe_locks = {}      # {gid: asyncio.Lock} 串行排队
        self._pipe_recent = {}     # {gid: deque(maxlen=30)} 最近消息滑动窗口（字符串）
        # 与 _pipe_recent 平行：每条窗口消息对应的 inbox 相对路径列表（用于出窗删除）
        self._pipe_recent_imgs = {}  # {gid: deque(maxlen=30) of list[str]}
        self._init_pipe_threshold = lambda: random.randint(4, 8)
        INBOX_DIR.mkdir(parents=True, exist_ok=True)
        for gid in self.PIPE_GROUPS:
            self._pipe_counters[gid] = 0
            self._pipe_thresholds[gid] = self._init_pipe_threshold()
            self._pipe_locks[gid] = asyncio.Lock()
            self._pipe_recent[gid] = deque(maxlen=30)
            self._pipe_recent_imgs[gid] = deque(maxlen=30)
            (INBOX_DIR / str(gid)).mkdir(parents=True, exist_ok=True)

        # 插件系统
        self.plugins = plugins.get_all()
        self.plugin_config = plugins.load_config()
        msg_ps = self.plugins
        notice_ps = plugins.get_all_notice()
        request_ps = plugins.get_all_request()
        if msg_ps or notice_ps or request_ps:
            print(f"[中繼] 已加载 {len(msg_ps) + len(notice_ps) + len(request_ps)} 个插件:")
            for p in msg_ps:
                print(f"       [消息] {p.name} — {p.desc}")
            for p in notice_ps:
                print(f"       [通知] {p.name} — {p.desc}")
            for p in request_ps:
                print(f"       [请求] {p.name} — {p.desc}")
        else:
            print(f"[中繼] 暂无激活插件（在 plugins.py 中注册即可）")

        print(f"[中繼] 初始化完成（persona: {PERSONA_FILE.name if PERSONA_FILE.exists() else '默认'}）")
        print(f"[中繼] 管道群: {PIPE_GROUPS}, 其他群走 {FALLBACK_MODE}")
        print(f"[中繼] 读图群: {sorted(self.GROUP_VISION) if self.GROUP_VISION else '（无，默认关）'}")
        print(f"[中繼] 主动发言阈值: {self._pipe_thresholds}")

    async def publish_event(self, event: dict):
        """推事件到 WebUI（如有 EventBus）"""
        if self.eventbus:
            await self.eventbus.publish(event)

    # ------- 去重 -------
    def _dedup(self, mid) -> bool:
        """返回 True = 已见过（跳过），False = 新消息"""
        if not mid:
            return False
        if mid in self._seen_ids:
            return True
        self._seen_ids.add(mid)
        self._seen_queue.append(mid)
        if len(self._seen_queue) > self._seen_max:
            old = self._seen_queue.popleft()
            self._seen_ids.remove(old)
        return False

    # ------- 对话历史 -------
    def _conv_path(self, gid):
        return MEM_DIR / "conv" / f"{gid}.json"

    def _load_conv(self, gid):
        p = self._conv_path(gid)
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return []
        return []

    def _save_conv(self, gid, conv):
        self._conv_path(gid).write_text(
            json.dumps(conv, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _push_conv(self, gid, entry):
        conv = self._load_conv(gid)
        conv.append(entry)
        if len(conv) > MAX_HISTORY:
            conv = conv[-MAX_HISTORY:]
        self._save_conv(gid, conv)

    # ------- WebSocket -------
    async def connect(self):
        headers = {"Authorization": f"Bearer {TOKEN}"}
        self.ws = await websockets.connect(WS_URL, additional_headers=headers)
        first = json.loads(await self.ws.recv())
        self.bot_qq = first.get("self_id", "?")
        print(f"[中繼] 已登录 QQ: {self.bot_qq}")

    async def run(self):
        await self.connect()
        print("[中繼] 开始监听...")
        async for msg in self.ws:
            try:
                data = json.loads(msg)
                # OneBot API 回包（带 echo / status）
                if "echo" in data and ("status" in data or "retcode" in data or "data" in data):
                    fut = self._api_futures.pop(data.get("echo"), None)
                    if fut and not fut.done():
                        fut.set_result(data)
                    continue
                asyncio.create_task(self.on_message(data))
            except json.JSONDecodeError:
                pass

    async def on_message(self, data: dict):
        post_type = data.get("post_type")

        if post_type == "message":
            # 去重
            if self._dedup(data.get("message_id")):
                return

            mt = data.get("message_type")
            uid = data.get("user_id")

            if mt == "group":
                gid = data.get("group_id")
                text = data.get("raw_message", "").strip()
                sender = data.get("sender", {})
                nick = sender.get("card") or sender.get("nickname") or str(uid)
                print(f"[群 {gid}] {nick}({uid}): {text}")
                await self.on_group_msg(gid, uid, nick, text, event=data)
            elif mt == "private":
                # 私聊由 cc-connect 处理
                pass

        elif post_type == "notice":
            gid = data.get("group_id", "?")
            ntype = data.get("notice_type", "?")
            print(f"[通知] 群 {gid} | {ntype}")
            await plugins.run_notice_plugins(self, data)

        elif post_type == "request":
            gid = data.get("group_id", "?")
            rtype = data.get("request_type", "?")
            sub = data.get("sub_type", "?")
            print(f"[请求] 群 {gid} | {rtype}/{sub}")
            await plugins.run_request_plugins(self, data)

    # ------- 管道调用 -------
    async def _call_pipe(self, gid: int, message: str, allowed_tools: str | None = None) -> str:
        """先尝试 --resume 续接已有会话，失败则 --session-id 创建新会话"""
        sid = pipe_session_id(gid)
        tools = allowed_tools or PIPE_ALLOWED_TOOLS
        base = [
            CLAUDE_CMD, "--bare", "-p",
            "--add-dir", str(PIPE_ADD_DIR),
            "--allowedTools", tools,
            "--system-prompt", self.pipe_persona,
        ]
        # 子进程环境变量：注入 DeepSeek API 认证
        pipe_env = {**os.environ,
            "ANTHROPIC_AUTH_TOKEN": DEEPSEEK_API_KEY,
            "ANTHROPIC_BASE_URL": DEEPSEEK_BASE,
            "ANTHROPIC_MODEL": "deepseek-v4-flash",
        }
        # 先试 resume
        try:
            proc = await asyncio.create_subprocess_exec(
                *base, "--resume", sid,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(PIPE_WORK_DIR),
                env=pipe_env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=message.encode("utf-8")), timeout=120
            )
            if proc.returncode == 0:
                return stdout.decode("utf-8", errors="replace").strip()
            # resume 失败，可能是会话不存在，尝试 session-id 创建
            err_text = stderr.decode("utf-8", errors="replace")
            if "does not exist" not in err_text and "not found" not in err_text.lower():
                out_text = stdout.decode("utf-8", errors="replace").strip()
                if not err_text.strip() and not out_text:
                    err_text = "(无输出)"
                print(f"[管道] resume 错误 (exit {proc.returncode}): stderr={err_text.strip()[:300]}")
                if out_text:
                    print(f"[管道] stdout: {out_text[:300]}")
                return f"（{BOT_NAME}挂机中....）"
        except FileNotFoundError:
            print("[管道] claude 命令未找到")
            return "（CLI 还没配好）"
        except (asyncio.TimeoutError, Exception) as e:
            print(f"[管道] resume 异常: {e}")
            return "（正在走神，过会再来）"

        # 回退：用 session-id 创建新会话
        try:
            proc = await asyncio.create_subprocess_exec(
                *base, "--session-id", sid,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(PIPE_WORK_DIR),
                env=pipe_env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=message.encode("utf-8")), timeout=120
            )
            if proc.returncode != 0:
                err = stderr.decode("utf-8", errors="replace").strip()
                out = stdout.decode("utf-8", errors="replace").strip()
                if not err and not out:
                    err = "(无输出)"
                print(f"[管道] claude 错误 (exit {proc.returncode}): stderr={err[:300]}")
                if out:
                    print(f"[管道] stdout: {out[:300]}")
                return "（出了点问题，等下再试试）"
            return stdout.decode("utf-8", errors="replace").strip()
        except asyncio.TimeoutError:
            print(f"[管道] claude 超时 (gid={gid})")
            return "（正在走神，过会再来）"
        except Exception as e:
            print(f"[管道] 异常: {e}")
            return ""


    async def call_api(self, action: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        """通过 OneBot WS 调 action，等待 echo 回包。"""
        if not self.ws or self.ws.close_code is not None:
            raise RuntimeError("ws not connected")
        echo = f"api-{uuid.uuid4().hex}"
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._api_futures[echo] = fut
        payload = {
            "action": action,
            "params": params or {},
            "echo": echo,
        }
        try:
            await self.ws.send(json.dumps(payload))
            return await asyncio.wait_for(fut, timeout=timeout)
        except Exception:
            self._api_futures.pop(echo, None)
            raise

    def vision_enabled(self, gid: int) -> bool:
        return gid in self.GROUP_VISION

    def _ensure_pipe_state(self, gid: int):
        if gid not in self._pipe_recent:
            self._pipe_recent[gid] = deque(maxlen=30)
        if gid not in self._pipe_recent_imgs:
            self._pipe_recent_imgs[gid] = deque(maxlen=30)
        if gid not in self._pipe_counters:
            self._pipe_counters[gid] = 0
        if gid not in self._pipe_thresholds:
            self._pipe_thresholds[gid] = self._init_pipe_threshold()
        if gid not in self._pipe_locks:
            self._pipe_locks[gid] = asyncio.Lock()

    def _delete_inbox_paths(self, rel_paths: list[str]):
        for rel in rel_paths or []:
            try:
                p = PIPE_ADD_DIR / rel
                if "inbox" not in Path(rel).parts:
                    continue
                if p.is_file():
                    p.unlink()
                    print(f"[读图] 已删除过期/出窗图片: {rel}")
                side = p.with_suffix(".txt")
                if side.is_file():
                    side.unlink()
            except Exception as e:
                print(f"[读图] 删除失败 {rel}: {e}")

    def _append_pipe_recent(self, gid: int, line: str, image_rels: list[str] | None = None):
        """写入滑动窗口；若顶掉最旧一条，顺手删其 inbox 图。"""
        self._ensure_pipe_state(gid)
        imgs = list(image_rels or [])
        recent = self._pipe_recent[gid]
        recent_imgs = self._pipe_recent_imgs[gid]
        if len(recent) == recent.maxlen:
            old_imgs = recent_imgs[0] if recent_imgs else []
            self._delete_inbox_paths(list(old_imgs))
        recent.append(line)
        recent_imgs.append(imgs)

    def _cleanup_inbox_group(self, gid: int):
        """每群最多 5 张图 + 30 分钟过期；同名 .txt 描述随图删除。"""
        gdir = INBOX_DIR / str(gid)
        if not gdir.exists():
            return
        now = time.time()
        img_ext = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

        def _unlink_with_side(p: Path, reason: str):
            rel = f"inbox/{gid}/{p.name}"
            try:
                if p.is_file():
                    p.unlink()
                    print(f"[读图] {reason}: {rel}")
                side = p.with_suffix(".txt") if p.suffix.lower() != ".txt" else None
                if side and side.is_file():
                    side.unlink()
            except Exception as e:
                print(f"[读图] 清理失败 {p}: {e}")

        files = [p for p in gdir.iterdir() if p.is_file()]
        for p in files:
            try:
                age = now - p.stat().st_mtime
                if age > INBOX_TTL_SEC:
                    _unlink_with_side(p, "TTL 过期删除")
            except Exception as e:
                print(f"[读图] TTL 清理失败 {p}: {e}")

        imgs = [p for p in gdir.iterdir() if p.is_file() and p.suffix.lower() in img_ext]
        if len(imgs) <= INBOX_MAX_PER_GROUP:
            for p in gdir.iterdir():
                if p.is_file() and p.suffix.lower() == ".txt":
                    if not any(p.with_suffix(ext).is_file() for ext in img_ext):
                        try:
                            p.unlink()
                        except Exception:
                            pass
            return
        imgs.sort(key=lambda p: p.stat().st_mtime)
        for p in imgs[: max(0, len(imgs) - INBOX_MAX_PER_GROUP)]:
            _unlink_with_side(p, "超出每群上限删除")

    async def _download_pipe_images(self, gid: int, text: str, event: dict | None = None) -> list[str]:
        """下载图片到 inbox。优先 CQ url（反解 &amp;），失败再 get_image。"""
        refs = extract_image_refs(text)
        # 结构化 message 段补一层（有时 raw_message 和 message 不一致）
        if event and isinstance(event.get("message"), list):
            for seg in event["message"]:
                if not isinstance(seg, dict) or seg.get("type") != "image":
                    continue
                data = seg.get("data") or {}
                url = _normalize_media_url(str(data.get("url") or ""))
                file_id = str(data.get("file") or data.get("file_id") or "")
                if url and not (url.startswith("http://") or url.startswith("https://")):
                    url = ""
                key = url or file_id
                if not key:
                    continue
                if any((r.get("url") == url and url) or (r.get("file") == file_id and file_id) for r in refs):
                    continue
                refs.append({"url": url, "file": file_id})

        if not refs:
            return []
        (INBOX_DIR / str(gid)).mkdir(parents=True, exist_ok=True)
        saved = []

        for ref in refs:
            data = b""
            ct = ""
            source = ""
            url = ref.get("url") or ""
            file_id = ref.get("file") or ""

            # 1) 直链下载
            if url:
                try:
                    headers = {
                        "Referer": "https://web.qpic.cn/",
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    }
                    r = await self.http.get(url, timeout=20.0, headers=headers)
                    r.raise_for_status()
                    data = r.content
                    ct = r.headers.get("content-type", "")
                    source = "url"
                except Exception as e:
                    print(f"[读图] url 下载失败，尝试 get_image: {e}")
                    data = b""

            # 2) OneBot get_image 兜底（SnowLuma/NapCat 本地缓存）
            if not data and file_id:
                try:
                    resp = await self.call_api("get_image", {"file": file_id})
                    body = resp.get("data") or {}
                    # 常见字段：file / path / url
                    local_path = body.get("file") or body.get("path") or body.get("file_path")
                    api_url = _normalize_media_url(str(body.get("url") or ""))
                    if local_path and Path(local_path).is_file():
                        data = Path(local_path).read_bytes()
                        source = f"get_image-file:{local_path}"
                    elif api_url.startswith("http"):
                        r = await self.http.get(api_url, timeout=20.0)
                        r.raise_for_status()
                        data = r.content
                        ct = r.headers.get("content-type", "")
                        source = "get_image-url"
                    else:
                        print(f"[读图] get_image 无可用数据: {str(resp)[:200]}")
                except Exception as e:
                    print(f"[读图] get_image 失败: {e}")

            if not data:
                print(f"[读图] 跳过（无数据） file={file_id[:40] if file_id else '-'} url={url[:80] if url else '-'}")
                continue

            try:
                data, ext = _process_image_bytes(data, ct)
                key = url or file_id or str(time.time())
                digest = hashlib.sha1(key.encode("utf-8", errors="ignore")).hexdigest()[:10]
                name = f"{int(time.time())}_{digest}{ext}"
                rel = f"inbox/{gid}/{name}"
                path = PIPE_ADD_DIR / rel
                path.write_bytes(data)
                saved.append(rel)
                print(f"[读图] 已保存 {rel} ({len(data)} bytes, via {source or 'unknown'})")
            except Exception as e:
                print(f"[读图] 写盘失败: {e}")

        if saved:
            self._cleanup_inbox_group(gid)
        return saved


    def _guess_media_type(self, path: Path) -> str:
        ext = path.suffix.lower()
        return {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".gif": "image/gif",
            ".bmp": "image/bmp",
        }.get(ext, "image/jpeg")

    async def _describe_image(self, rel: str) -> str:
        """中继侧直接打多模态 API，返回短描述。失败返回空串。"""
        path = PIPE_ADD_DIR / rel
        if not path.is_file():
            return ""
        try:
            raw = path.read_bytes()
            if not raw:
                return ""
            # 过大就再压一把（无 Pillow 则原样）
            if len(raw) > IMAGE_COMPRESS_BYTES:
                raw, ext = _process_image_bytes(raw, self._guess_media_type(path))
                # 不改原文件名后缀，仅用于上传
            b64 = __import__("base64").b64encode(raw).decode("ascii")
            media_type = self._guess_media_type(path)
            content = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": b64,
                    },
                },
                {
                    "type": "text",
                    "text": (
                        "用中文简短描述这张图里看得见的内容，一句话，"
                        f"不超过{VISION_CAPTION_MAX}字。不要猜测看不见的信息，不要加前后缀。"
                    ),
                },
            ]
            # 复用 direct 的 anthropic 兼容接口，system 用极简，避免 persona 抢戏
            r = await self.http.post(
                f"{DEEPSEEK_BASE}/v1/messages",
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": DEEPSEEK_MODEL,
                    "system": "你是图片描述器。只输出对图片的客观短描述。",
                    "messages": [{"role": "user", "content": content}],
                    "max_tokens": 120,
                },
                timeout=45.0,
            )
            r.raise_for_status()
            data = r.json()
            text = ""
            for block in data.get("content") or []:
                if block.get("type") == "text":
                    text = (block.get("text") or "").strip()
                    break
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) > VISION_CAPTION_MAX:
                text = text[:VISION_CAPTION_MAX].rstrip() + "..."
            return text
        except Exception as e:
            print(f"[读图] vision 描述失败 {rel}: {e}")
            return ""

    async def _captions_for_images(self, image_rels: list[str]) -> dict[str, str]:
        """为若干本地图生成描述；写同名 .txt 缓存，避免重复请求。"""
        out: dict[str, str] = {}
        for rel in image_rels or []:
            path = PIPE_ADD_DIR / rel
            side = path.with_suffix(".txt")
            if side.is_file():
                try:
                    cap = side.read_text(encoding="utf-8", errors="replace").strip()
                    if cap:
                        out[rel] = cap
                        continue
                except Exception:
                    pass
            cap = await self._describe_image(rel)
            if cap:
                try:
                    side.write_text(cap, encoding="utf-8")
                except Exception:
                    pass
                out[rel] = cap
                print(f"[读图] 描述就绪 {rel}: {cap[:60]}")
            else:
                print(f"[读图] 描述为空 {rel}")
        return out

    def _format_pipe_line(
        self,
        nick: str,
        text: str,
        image_rels: list[str] | None = None,
        captions: dict[str, str] | None = None,
    ) -> str:
        body = strip_cq_images(text)
        captions = captions or {}
        markers = []
        for p in image_rels or []:
            cap = (captions.get(p) or "").strip()
            if cap:
                markers.append(f"[图片描述: {cap}]")
            else:
                markers.append("[图片]")
        mark = " ".join(markers)
        if mark and body:
            return f"[{nick}] {mark} {body}"
        if mark:
            return f"[{nick}] {mark}"
        return f"[{nick}] {body or text}"

    def _with_vision_prompt(self, gid: int, speak_prompt: str, extra_lines: list[str] | None = None):
        """若上下文里已有图片描述，补一句“按描述接话”；不再要求 CLI Read。
        返回 (prompt, has_image_context: list 占位兼容，非空表示本轮有图上下文)。
        """
        if not self.vision_enabled(gid):
            return speak_prompt, []
        lines = list(self._pipe_recent.get(gid, []))
        if extra_lines:
            lines = lines + list(extra_lines)
        joined = "\n".join(lines)
        has_cap = "[图片描述:" in joined
        has_img = ("[图片]" in joined) or has_cap or bool(list_image_markers(joined))
        if not has_img:
            return speak_prompt, []
        hint = (
            "\n\n聊天记录里若出现 [图片描述: ...]，那是中继已经看过图后的客观描述，"
            "请据此自然接话，不要提文件名/路径，不要说“我看到描述了”。"
            "如果只有 [图片] 没有描述，就当看不清，别瞎编细节。"
        )
        # 非空 list 仅用于“有图上下文时跳过表情包指令”
        return speak_prompt + hint, ["caption"]


    async def on_group_msg(self, gid: int, uid: int, nick: str, text: str, event: dict | None = None):
        # 发布消息事件到 WebUI
        await self.publish_event({
            "type": "message",
            "data": {"gid": gid, "uid": uid, "nick": nick, "text": text}
        })

        # 骰子（全群通用，不需要 @）
        if text.startswith(".r") or text.startswith("。r"):
            r = self._roll(text[2:].strip())
            if r:
                await self.send_group(gid, r)
            return

        mode = GROUP_MODE.get(gid, FALLBACK_MODE)
        at_tag = f"[CQ:at,qq={self.bot_qq}]"
        is_at = at_tag in text

        if mode == "pipe":
            # === 管道群（聊天群 / 测试群） ===
            self._ensure_pipe_state(gid)

            # 跳过 ! 管理命令
            if text.startswith("!"):
                return

            # 去 @ 后的干净文本
            clean = text.replace(at_tag, "").strip() if is_at else text
            clean = strip_cq_images(clean)

            # 读图开启时：消息到达即落盘，并中继侧 vision 出描述（@ / 普通消息都下）
            image_rels: list[str] = []
            captions: dict[str, str] = {}
            if self.vision_enabled(gid):
                image_rels = await self._download_pipe_images(gid, text, event=event)
                if image_rels:
                    captions = await self._captions_for_images(image_rels)

            should_speak = False
            speak_prompt = ""
            vision_paths: list[str] = []
            current_line = self._format_pipe_line(nick, text, image_rels, captions)

            if is_at:
                should_speak = True
                # @ 也写入窗口，方便“刚才那张图”类追问
                self._append_pipe_recent(gid, current_line, image_rels)
                recent_ctx = "\n".join(self._pipe_recent[gid])
                at_body = clean
                if image_rels:
                    # 与窗口一致：优先描述
                    parts = []
                    for p in image_rels:
                        cap = (captions.get(p) or "").strip()
                        parts.append(f"[图片描述: {cap}]" if cap else "[图片]")
                    mark = " ".join(parts)
                    at_body = f"{mark} {clean}".strip() if clean else mark
                if recent_ctx:
                    if at_body:
                        speak_prompt = f"以下是群聊上下文（最近 {len(self._pipe_recent[gid])} 条）：\n{recent_ctx}\n\n[{nick}] 艾特了你：{at_body}\n\n每条回复在85字以内。"
                    else:
                        speak_prompt = f"以下是群聊上下文（最近 {len(self._pipe_recent[gid])} 条）：\n{recent_ctx}\n\n[{nick}] 叫了你一声\n\n每条回复在85字以内。"
                else:
                    speak_prompt = f"[{nick}] {at_body}\n\n每条回复在85字以内。" if at_body else f"[{nick}] 有人叫了你一声\n\n每条回复在85字以内。"
                speak_prompt, vision_paths = self._with_vision_prompt(gid, speak_prompt)
            else:
                # 记录到滑动窗口（出窗时删对应 inbox 图）
                self._append_pipe_recent(gid, current_line, image_rels)
                # 主动发言计数
                self._pipe_counters[gid] += 1
                if self._pipe_counters[gid] >= self._pipe_thresholds[gid]:
                    recent = "\n".join(self._pipe_recent[gid])
                    if not recent.strip():
                        # 窗口为空，跳过本次触发
                        self._pipe_counters[gid] = 0
                        self._pipe_thresholds[gid] = self._init_pipe_threshold()
                        print(f"[管道] 群 {gid} 窗口为空，跳过主动发言，下次阈值: {self._pipe_thresholds[gid]}")
                    else:
                        should_speak = True
                        speak_prompt = f"以下是最近群聊记录：\n\n{recent if recent else '(暂无聊天内容)'}\n\n你是群成员，自然接一句，不要太正式。每条回复在85字以内。"
                        speak_prompt, vision_paths = self._with_vision_prompt(gid, speak_prompt)

            # === 插件钩子（窗口已更新、计数已累加；在 AI 回复之前） ===
            if await plugins.run_plugins(self, gid, uid, nick, text, is_at):
                return

            if should_speak:
                # 确认 AI 真要发言了再消费计数（避免插件抢话后计数器被吞）
                self._pipe_counters[gid] = 0
                self._pipe_thresholds[gid] = self._init_pipe_threshold()
                print(f"[管道] 群 {gid} {'@触发' if is_at else '主动发言'}，下次阈值: {self._pipe_thresholds[gid]}")
                await self.publish_event({
                    "type": "pipe_trigger",
                    "data": {"gid": gid, "threshold": self._pipe_thresholds[gid]}
                })
                # 50% 概率带表情包指令；本轮要读群图时禁用，避免“不要聊图”打架
                if (not vision_paths) and random.random() < 0.5:
                    speak_prompt = "\n".join([speak_prompt, MEME_PROMPT_EXTRA])
                # 只有主人 @ 触发才给写权限，其他情况（别人 @ / 主动发言）只能读
                pipe_tools = PIPE_ALLOWED_TOOLS_ADMIN if (is_at and uid == MASTER_QQ) else PIPE_ALLOWED_TOOLS
                async with self._pipe_locks[gid]:
                    reply = await self._call_pipe(gid, speak_prompt, allowed_tools=pipe_tools)
                if reply:
                    await self.send_group(gid, reply)
                    self._append_pipe_recent(gid, f"[{BOT_NAME}] {reply}", [])
                    await self.publish_event({
                        "type": "reply",
                        "data": {"gid": gid, "text": reply, "mode": "pipe", "is_at": is_at}
                    })

        else:
            # === 直调群（跑团群 / 未知） ===

            # 管理指令（不需要 @）
            if text.startswith("!"):
                await self.on_admin(gid, uid, text[1:].strip())
                return

            # AI 聊天：只响应 @bot
            if not is_at:
                # 插件钩子（direct 群，非 @ 消息）
                if await plugins.run_plugins(self, gid, uid, nick, text, is_at=False):
                    return
                return
            clean = text.replace(at_tag, "").strip()
            if not clean:
                return

            # 插件钩子（direct 群，@触发时）
            if await plugins.run_plugins(self, gid, uid, nick, text, is_at=True):
                return

            await self.on_ai_chat(gid, uid, nick, clean)

    def _roll(self, expr: str):
        if not expr:
            return None
        ds = expr.split()[0].lower()
        try:
            if "d" in ds:
                if ds.startswith("d"):
                    cnt, sides = 1, int(ds[1:])
                else:
                    cnt, sides = map(int, ds.split("d"))
                cnt, sides = min(cnt, 100), max(sides, 1)
                rolls = [random.randint(1, sides) for _ in range(cnt)]
                total = sum(rolls)
                if cnt == 1:
                    return f"🎲 D{sides} → **{total}**"
                return f"🎲 {cnt}D{sides} → {' + '.join(map(str, rolls))} = **{total}**"
            else:
                s = int(ds)
                return f"🎲 D{s} → **{random.randint(1, s)}**"
        except ValueError:
            return "格式不对，试试 .r d20"

    async def on_admin(self, gid: int, uid: int, cmd: str):
        if uid != MASTER_QQ:
            await self.send_group(gid, "你没有管理权限")
            return
        if cmd == "清空记忆":
            self._save_conv(gid, [])
            await self.send_group(gid, "本群对话历史已清空 ✅")
        elif cmd == "重载":
            self.persona = load_persona()
            await self.send_group(gid, f"人设+知识库已重载（{PERSONA_FILE.name}）")
        elif cmd.startswith("人设"):
            # !人设 xxxx — 修改人设
            new_persona = cmd[2:].strip()
            if new_persona:
                PERSONA_FILE.write_text(new_persona, encoding="utf-8")
                self.persona = load_persona()
                await self.send_group(gid, "人设已更新+重载")
        elif cmd == "知识":
            knowledge_list = []
            if KNOWLEDGE_DIR.exists():
                for f in sorted(KNOWLEDGE_DIR.glob("*.md")):
                    knowledge_list.append(f.stem)
            if knowledge_list:
                await self.send_group(gid, "知识库文件:\n" + "\n".join(f"- {k}" for k in knowledge_list))
            else:
                await self.send_group(gid, "知识库为空")
        elif cmd == "状态":
            conv = self._load_conv(gid)
            msg_count = len(conv)
            total_chars = sum(len(e.get("content", "")) for e in conv)
            # 粗略估算 token 数（中英文混合约 0.75 倍字符数）
            est_tokens = int(total_chars * 0.75)
            info = [
                f"群 {gid}",
                f"消息数: {msg_count}/{MAX_HISTORY}",
                f"总字符: {total_chars}",
                f"估计 tokens: ~{est_tokens}",
            ]
            await self.send_group(gid, "\n".join(info))
        elif cmd.startswith("打标"):
            # !打标 — 列出 unsorted 里的新图
            if not MEME_UNSORTED_DIR.exists():
                await self.send_group(gid, "表情包目录还没建")
                return
            files = sorted(MEME_UNSORTED_DIR.iterdir())
            if not files:
                await self.send_group(gid, "没有待分类的表情包 ✌️")
                return
            lines = [f"待分类表情包（{len(files)} 张）："]
            for f in files:
                size_kb = f.stat().st_size // 1024
                lines.append(f"  {f.name}  ({size_kb}KB)")
            lines.append("用 !标 <文件名> <标签1, 标签2, ...> 来归档，比如：!标 xxx.jpg 草, 无奈")
            await self.send_group(gid, "\n".join(lines))
        elif cmd.startswith("标 "):
            # !标 <文件名> <标签1, 标签2, ...>
            parts = cmd[2:].strip().split(maxsplit=1)
            if len(parts) < 2:
                await self.send_group(gid, "格式: !标 <文件名> <标签1, 标签2, ...>")
                return
            fname, tags_str = parts
            src = MEME_UNSORTED_DIR / fname
            if not src.exists():
                await self.send_group(gid, f"文件 {fname} 不在 unsorted 里")
                return
            # 移入 archive（平铺，不分子目录）
            dst = MEME_ARCHIVE_DIR / fname
            src.rename(dst)
            # 写入 index.md
            index_path = MEME_ARCHIVE_DIR / "index.md"
            if not index_path.exists():
                index_path.write_text("# 表情包索引\n", encoding="utf-8")
            entry = f"- {fname} — {tags_str}\n"
            with open(index_path, "r", encoding="utf-8") as f:
                content = f.read()
            # 插在第一个二级标题前面，保持有序
            if content.strip().endswith("---"):
                content += f"\n{entry}"
            else:
                # 追加在文件末尾
                content += entry
            index_path.write_text(content, encoding="utf-8")
            await self.send_group(gid, f"✅ {fname} 归档，标签：{tags_str}")
        elif cmd == "帮助":
            await self.send_group(gid, (
                "!清空记忆 — 清空对话历史\n"
                "!重载 — 重读人设+知识库\n"
                "!人设 xxx — 修改人设\n"
                "!知识 — 查看知识库\n"
                "!状态 — 群聊对话统计\n"
                "!打标 — 列出待分类表情包\n"
                "!标 <文件名> <标签1, 标签2, ...> — 归档表情包并打标签"
            ))

    async def on_ai_chat(self, gid: int, uid: int, nick: str, text: str):
        self._push_conv(gid, {
            "role": "user", "user_id": uid, "nickname": nick,
            "content": text, "time": datetime.now().isoformat()
        })
        conv = self._load_conv(gid)
        msgs = []
        for e in conv:
            if e["role"] == "user":
                msgs.append({"role": "user", "content": f"[{e['nickname']}] {e['content']}"})
            else:
                msgs.append({"role": "assistant", "content": e["content"]})
        try:
            reply = await self._call_api(msgs)
            self._push_conv(gid, {
                "role": "assistant", "user_id": 0, "nickname": BOT_NAME,
                "content": reply, "time": datetime.now().isoformat()
            })
            await self.send_group(gid, reply)
            await self.publish_event({
                "type": "reply",
                "data": {"gid": gid, "text": reply, "mode": "direct"}
            })
        except Exception as e:
            print(f"[中繼] AI 错误: {e}")

    async def _call_api(self, messages: list) -> str:
        r = await self.http.post(
            f"{DEEPSEEK_BASE}/v1/messages",
            headers={
                "x-api-key": DEEPSEEK_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": DEEPSEEK_MODEL,
                "system": self.persona,
                "messages": messages,
                "max_tokens": 500,
            },
        )
        r.raise_for_status()
        data = r.json()
        for block in data["content"]:
            if block.get("type") == "text":
                return block["text"]
        return ""

    def _split_message(self, msg: str) -> list[str]:
        """拆分长消息。
        - ≤30 字 → 不拆分
        - >30 字 → 在句尾（。！？）拆，每段尽量 ≤100 字
        - 不按段落拆，避免空行变多条消息"""
        if len(msg) <= 30:
            return [msg]

        chunks, buf = [], ""
        for ch in msg:
            buf += ch
            if ch in "。！？" and len(buf) >= 30:
                chunks.append(buf.strip())
                buf = ""
        if buf.strip():
            chunks.append(buf.strip())
        return chunks if chunks else [msg]

    def _sanitize_outbound_message(self, msg: str) -> str:
        """发送前剥离本图床不存在的 CQ:image，避免图 404 导致整条消息失败。

        - 仅校验 host.docker.internal / 127.0.0.1 + MEME_SERVER_PORT
        - 外链图床原样放行
        - 文件不存在则去掉该 CQ 段，保留文本
        """
        if not msg or "[CQ:image," not in msg:
            return msg

        markers = (
            f"host.docker.internal:{MEME_SERVER_PORT}/",
            f"127.0.0.1:{MEME_SERVER_PORT}/",
            f"localhost:{MEME_SERVER_PORT}/",
        )
        cq_image_re = re.compile(r"\[CQ:image,([^\]]*)\]")

        def repl(m: re.Match) -> str:
            params = m.group(1)
            fm = re.search(r"file=([^,\]]+)", params)
            if not fm:
                return m.group(0)
            file_url = unquote(fm.group(1).strip())
            marker = next((mk for mk in markers if mk in file_url), None)
            if not marker:
                return m.group(0)  # 外链放行
            rel = file_url.split(marker, 1)[-1].lstrip("/")
            # 防路径穿越：只允许相对 MEME_DIR 的 archive/unsorted 等子路径
            path = (MEME_DIR / rel).resolve()
            try:
                path.relative_to(MEME_DIR.resolve())
            except ValueError:
                print(f"[图床] 发送前剥离越界路径: {rel}")
                return ""
            if path.is_file():
                return m.group(0)
            print(f"[图床] 发送前剥离不存在的图: {rel}")
            return ""

        cleaned = cq_image_re.sub(repl, msg)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return cleaned

    async def send_group(self, gid: int, msg: str):
        if not self.ws or self.ws.close_code is not None:
            return
        msg = self._sanitize_outbound_message(msg)
        if not msg.strip():
            return
        chunks = self._split_message(msg)
        for i, chunk in enumerate(chunks):
            await self.ws.send(json.dumps({
                "action": "send_group_msg",
                "params": {"group_id": gid, "message": chunk},
            }))
            if i < len(chunks) - 1:
                await asyncio.sleep(0.9)


# ============ HTTP 图床 ============
def start_meme_http_server():
    """启动极简 HTTP 文件服务器，供容器内通过宿主网络访问表情包"""
    import http.server
    import threading

    class MemeHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(MEME_DIR), **kwargs)
        def log_message(self, fmt, *args):
            try:
                print(f"[图床] {fmt % args}")
            except Exception:
                print(f"[图床] {args}")

    try:
        server = http.server.HTTPServer(("0.0.0.0", MEME_SERVER_PORT), MemeHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        print(f"[图床] http://0.0.0.0:{MEME_SERVER_PORT}（容器内通过 host.docker.internal:{MEME_SERVER_PORT} 访问）")
    except OSError as e:
        print(f"[图床] 启动失败（端口 {MEME_SERVER_PORT} 可能被占）: {e}")


async def main():
    retry = 3

    # 启动图床服务器（供容器内通过宿主网络读图）
    start_meme_http_server()

    # 可选启动 WebUI
    if "--webui" in sys.argv or os.getenv("WEBUI_ENABLED", "").lower() == "true":
        try:
            from webui import EventBus, start_webui
            eventbus = EventBus()
            bot = RelayBot(eventbus=eventbus)
            webui_port = int(os.getenv("WEBUI_PORT", "8800"))
            webui_host = os.getenv("WEBUI_HOST", "127.0.0.1")
            asyncio.create_task(start_webui(bot, eventbus, host=webui_host, port=webui_port))
            print(f"[中繼] WebUI 启动于 http://{webui_host}:{webui_port}")
        except Exception as e:
            print(f"[中繼] WebUI 启动失败: {e}")
            bot = RelayBot()
    else:
        bot = RelayBot()

    while True:
        try:
            await bot.run()
        except (websockets.exceptions.ConnectionClosed, OSError) as e:
            print(f"[中繼] 断开 ({e})，{retry} 秒后重连...")
            await asyncio.sleep(retry)
            retry = min(retry + 1, 10)
        except Exception as e:
            print(f"[中繼] 异常: {e}")
            await asyncio.sleep(10)
            retry = 3


if __name__ == "__main__":
    asyncio.run(main())
