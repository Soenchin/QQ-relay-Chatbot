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
import json
import os
import random

import subprocess
import sys
import uuid
from collections import deque
from pathlib import Path
import time
from datetime import datetime

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
    import json
    GROUP_MODE = json.loads(GROUP_MODE_RAW)
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
        self.http = httpx.AsyncClient(timeout=60.0)
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
        # 主动发言计数器（仅管道群）
        self._pipe_counters = {}   # {gid: 当前计数}
        self._pipe_thresholds = {} # {gid: 触发阈值}
        self._pipe_locks = {}      # {gid: asyncio.Lock} 串行排队
        self._pipe_recent = {}     # {gid: deque(maxlen=10)} 最近消息滑动窗口
        self._init_pipe_threshold = lambda: random.randint(4, 8)
        for gid in self.PIPE_GROUPS:
            self._pipe_counters[gid] = 0
            self._pipe_thresholds[gid] = self._init_pipe_threshold()
            self._pipe_locks[gid] = asyncio.Lock()
            self._pipe_recent[gid] = deque(maxlen=30)

        print(f"[中繼] 初始化完成（persona: {PERSONA_FILE.name if PERSONA_FILE.exists() else '默认'}）")
        print(f"[中繼] 管道群: {PIPE_GROUPS}, 其他群走 {FALLBACK_MODE}")
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
                asyncio.create_task(self.on_message(data))
            except json.JSONDecodeError:
                pass

    async def on_message(self, data: dict):
        if data.get("post_type") != "message":
            return
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
            await self.on_group_msg(gid, uid, nick, text)
        elif mt == "private":
            # 私聊由 cc-connect 处理
            pass

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

    async def on_group_msg(self, gid: int, uid: int, nick: str, text: str):
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

            # 跳过 ! 管理命令
            if text.startswith("!"):
                return

            # 去 @ 后的干净文本
            clean = text.replace(at_tag, "").strip() if is_at else text

            should_speak = False
            speak_prompt = ""

            if is_at:
                should_speak = True
                # 读取窗口上下文（不含本条 @ 消息）
                recent_ctx = "\n".join(self._pipe_recent[gid])
                if recent_ctx:
                    if clean:
                        speak_prompt = f"以下是群聊上下文（最近 {len(self._pipe_recent[gid])} 条）：\n{recent_ctx}\n\n[{nick}] 艾特了你：{clean}"
                    else:
                        speak_prompt = f"以下是群聊上下文（最近 {len(self._pipe_recent[gid])} 条）：\n{recent_ctx}\n\n[{nick}] 叫了你一声"
                else:
                    speak_prompt = f"[{nick}] {clean}" if clean else f"[{nick}] 有人叫了你一声"
            else:
                # 记录到滑动窗口
                self._pipe_recent[gid].append(f"[{nick}] {text}")
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
                        speak_prompt = f"以下是最近群聊记录：\n\n{recent if recent else '(暂无聊天内容)'}\n\n你是群成员，自然接一句，不要太正式。"
                        self._pipe_counters[gid] = 0
                        self._pipe_thresholds[gid] = self._init_pipe_threshold()
                        print(f"[管道] 群 {gid} 主动触发，下次阈值: {self._pipe_thresholds[gid]}")
                        await self.publish_event({
                            "type": "pipe_trigger",
                            "data": {"gid": gid, "threshold": self._pipe_thresholds[gid]}
                        })

            if should_speak:
                print(f"[管道] 群 {gid} {'@触发' if is_at else '主动发言'}")
                # 50% 概率带表情包指令
                if random.random() < 0.5:
                    speak_prompt = "\n".join([speak_prompt, MEME_PROMPT_EXTRA])
                # 只有主人 @ 触发才给写权限，其他情况（别人 @ / 主动发言）只能读
                pipe_tools = PIPE_ALLOWED_TOOLS_ADMIN if (is_at and uid == MASTER_QQ) else PIPE_ALLOWED_TOOLS
                async with self._pipe_locks[gid]:
                    reply = await self._call_pipe(gid, speak_prompt, allowed_tools=pipe_tools)
                if reply:
                    await self.send_group(gid, reply)
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
                return
            clean = text.replace(at_tag, "").strip()
            if not clean:
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

    async def send_group(self, gid: int, msg: str):
        if not self.ws or self.ws.close_code is not None:
            return
        chunks = self._split_message(msg)
        for i, chunk in enumerate(chunks):
            await self.ws.send(json.dumps({
                "action": "send_group_msg",
                "params": {"group_id": gid, "message": chunk},
            }))
            if i < len(chunks) - 1:
                await asyncio.sleep(0.4)


# ============ HTTP 图床 ============
def start_meme_http_server():
    """启动极简 HTTP 文件服务器，供容器内通过宿主网络访问表情包"""
    import http.server
    import threading

    class MemeHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(MEME_DIR), **kwargs)
        def log_message(self, fmt, *args):
            print(f"[图床] {args[0]} {args[1]} {args[2]}")

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
