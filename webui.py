"""
WebUI — FastAPI backend for QQ relay bot
Provides REST API + WebSocket real-time push + static file serving
"""

import asyncio
import json
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from collections import deque

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import sys
sys.path.insert(0, str(Path(__file__).parent))
import plugins as plugin_mod

# ============ Paths ============
BOT_NAME = os.getenv("BOT_NAME", "QQ Bot")

MEM_DIR = Path(os.getenv("MEM_DIR", str(Path(__file__).parent / "memory")))
KNOWLEDGE_DIR = MEM_DIR / "knowledge"
CONV_DIR = MEM_DIR / "conv"
PERSONA_FILE = MEM_DIR / "persona.md"

STATIC_DIR = Path(__file__).parent / "static"
ENV_PATH = Path(__file__).parent / ".env"


# ============ .env Helpers ============
def read_env_file() -> dict:
    """Read .env file into a dict."""
    result = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                result[k.strip()] = v.strip()
    return result


def write_env_file(updates: dict) -> None:
    """Write updates to .env file, preserving comments and order."""
    lines = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()

    # Build a set of keys we're updating
    update_keys = set(updates.keys())
    updated = set()
    new_lines = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, v = stripped.split("=", 1)
            k = k.strip()
            if k in update_keys:
                new_lines.append(f"{k}={updates[k]}")
                updated.add(k)
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)

    # Append any new keys not found
    for k, v in updates.items():
        if k not in updated:
            new_lines.append(f"{k}={v}")

    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


# ============ EventBus ============
class EventBus:
    """Async pub-sub, relay.py pushes real-time events to WebSocket clients via this."""

    def __init__(self, maxsize: int = 500):
        self._subscribers: list[asyncio.Queue] = []
        self._maxsize = maxsize

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self._subscribers:
            self._subscribers.remove(q)

    async def publish(self, event: dict):
        dead = []
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass
            except Exception:
                dead.append(q)
        for q in dead:
            self._subscribers.remove(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


# ============ LogCapture ============
class LogCapture:
    """Intercept print output, write to both stdout and in-memory queue."""

    def __init__(self, max_lines: int = 1000):
        self._lines = deque(maxlen=max_lines)
        self._original_stdout = sys.stdout

    def write(self, text: str):
        try:
            self._original_stdout.write(text)
        except UnicodeEncodeError:
            self._original_stdout.write(text.encode('utf-8', errors='replace').decode('gbk', errors='replace'))
        self._original_stdout.flush()
        if text.strip():
            self._lines.append({
                "time": datetime.now().isoformat(),
                "msg": text.rstrip("\n"),
            })

    def flush(self):
        self._original_stdout.flush()

    def isatty(self) -> bool:
        return False

    @property
    def encoding(self):
        return self._original_stdout.encoding

    def get_recent(self, limit: int = 100, since: str | None = None) -> list[dict]:
        lines = list(self._lines)
        if since:
            lines = [l for l in lines if l["time"] >= since]
        return lines[-limit:]

    def install(self):
        sys.stdout = self

    def uninstall(self):
        sys.stdout = self._original_stdout


# ============ Pydantic Models ============
class GroupConfigUpdate(BaseModel):
    mode: str | None = None
    pipe_threshold: int | None = None


class SendMessageRequest(BaseModel):
    group_id: int
    message: str


class KnowledgeUpdate(BaseModel):
    content: str


class PersonaUpdate(BaseModel):
    content: str


class EnvConfigUpdate(BaseModel):
    group_mode: str | None = None
    fallback_mode: str | None = None
    group_vision: str | None = None  # JSON array string


# ============ FastAPI App ============
@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


def create_app(relay_bot=None, eventbus: EventBus | None = None) -> FastAPI:
    app = FastAPI(lifespan=lifespan, title=f"{BOT_NAME} Relay WebUI")

    app.state.relay_bot = relay_bot
    app.state.eventbus = eventbus or EventBus()

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ============ Config ============
    @app.get("/api/config")
    async def get_config():
        return {"bot_name": BOT_NAME}

    # ============ Env Config (Group Mode) ============
    @app.get("/api/env-config")
    async def get_env_config():
        """Read current .env group mode configuration."""
        env = read_env_file()
        group_mode_raw = env.get("GROUP_MODE", "")
        group_mode = {}
        if group_mode_raw:
            try:
                group_mode = {int(k): v for k, v in json.loads(group_mode_raw).items()}
            except Exception:
                pass
        group_vision_raw = env.get("GROUP_VISION", "")
        group_vision = []
        if group_vision_raw:
            try:
                group_vision = [int(x) for x in json.loads(group_vision_raw)]
            except Exception:
                group_vision = []
        return {
            "group_mode": group_mode,
            "fallback_mode": env.get("FALLBACK_MODE", "direct"),
            "group_mode_raw": group_mode_raw,
            "group_vision": group_vision,
            "group_vision_raw": group_vision_raw,
        }

    @app.put("/api/env-config")
    async def update_env_config(data: EnvConfigUpdate):
        """Write group mode configuration to .env file. Requires restart to take effect."""
        updates = {}
        if data.group_mode is not None:
            # Validate JSON
            try:
                parsed = json.loads(data.group_mode)
                if not isinstance(parsed, dict):
                    raise HTTPException(422, "group_mode must be a JSON object")
                for k, v in parsed.items():
                    if v not in ("direct", "pipe"):
                        raise HTTPException(422, f"mode for group {k} must be 'direct' or 'pipe'")
                updates["GROUP_MODE"] = data.group_mode
            except json.JSONDecodeError as e:
                raise HTTPException(422, f"Invalid JSON: {e}")
        if data.fallback_mode is not None:
            if data.fallback_mode not in ("direct", "pipe"):
                raise HTTPException(422, "fallback_mode must be 'direct' or 'pipe'")
            updates["FALLBACK_MODE"] = data.fallback_mode
        if data.group_vision is not None:
            try:
                parsed_v = json.loads(data.group_vision)
                if not isinstance(parsed_v, list):
                    raise HTTPException(422, "group_vision must be a JSON array")
                vision_ids = [int(x) for x in parsed_v]
                updates["GROUP_VISION"] = json.dumps(vision_ids, ensure_ascii=False, separators=(",", ":"))
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(422, f"Invalid group_vision: {e}")

        if not updates:
            raise HTTPException(422, "No fields to update")

        write_env_file(updates)
        bot = app.state.relay_bot
        if bot is not None and "GROUP_VISION" in updates:
            try:
                bot.GROUP_VISION = {int(x) for x in json.loads(updates["GROUP_VISION"])}
            except Exception:
                pass
        return {"ok": True, "updated": list(updates.keys()), "restart_required": True}

    # ============ Dashboard ============
    @app.get("/api/status")
    async def get_status():
        bot = app.state.relay_bot
        if not bot:
            return {"connected": False, "bot_qq": None, "uptime": 0}
        uptime = 0
        if hasattr(bot, "_start_time"):
            uptime = int(time.time() - bot._start_time)
        ws_ok = bot.ws is not None and (bot.ws.close_code is None if hasattr(bot.ws, 'close_code') else True)
        return {
            "connected": ws_ok,
            "bot_qq": bot.bot_qq,
            "uptime": uptime,
            "mode_count": len(getattr(bot, "GROUP_MODE", {})),
            "pipe_groups": getattr(bot, "PIPE_GROUPS", []),
            "subscribers": app.state.eventbus.subscriber_count,
        }

    @app.get("/api/stats")
    async def get_stats():
        bot = app.state.relay_bot
        if not bot:
            return {"groups": {}, "total_messages": 0}
        groups = {}
        total = 0
        for gid, mode in getattr(bot, "GROUP_MODE", {}).items():
            conv = bot._load_conv(gid) if hasattr(bot, '_load_conv') else []
            groups[str(gid)] = {
                "mode": mode,
                "history_count": len(conv),
                "threshold": bot._pipe_thresholds.get(gid, None),
                "counter": bot._pipe_counters.get(gid, 0) if hasattr(bot, '_pipe_counters') else 0,
            }
            total += len(conv)
        return {"groups": groups, "total_messages": total}

    @app.get("/api/pipe-state")
    async def get_pipe_state():
        bot = app.state.relay_bot
        if not bot:
            return {"groups": [], "pipe_groups": []}
        result = []
        for gid in getattr(bot, "PIPE_GROUPS", []):
            recent_raw = list(bot._pipe_recent.get(gid, [])) if hasattr(bot, '_pipe_recent') else []
            result.append({
                "gid": gid,
                "mode": bot.GROUP_MODE.get(gid, "pipe"),
                "counter": bot._pipe_counters.get(gid, 0) if hasattr(bot, '_pipe_counters') else 0,
                "threshold": bot._pipe_thresholds.get(gid, 0) if hasattr(bot, '_pipe_thresholds') else 0,
                "recent": recent_raw,
                "recent_count": len(recent_raw),
            })
        return {"groups": result, "pipe_groups": getattr(bot, "PIPE_GROUPS", [])}

    @app.post("/api/pipe-state/reload")
    async def reload_persona_from_webui():
        bot = app.state.relay_bot
        if not bot:
            raise HTTPException(404, "机器人未启动")
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
        bot.persona = "\n\n".join(parts)
        if hasattr(bot, 'pipe_persona') and PERSONA_FILE.exists():
            bot.pipe_persona = PERSONA_FILE.read_text(encoding="utf-8").strip()
        return {"ok": True}

    # ============ Group Management ============
    @app.get("/api/groups")
    async def list_groups():
        bot = app.state.relay_bot
        if not bot:
            return []
        result = []
        for gid, mode in getattr(bot, "GROUP_MODE", {}).items():
            conv = bot._load_conv(gid) if hasattr(bot, '_load_conv') else []
            result.append({
                "gid": gid,
                "mode": mode,
                "history_count": len(conv),
                "threshold": bot._pipe_thresholds.get(gid, None),
                "counter": bot._pipe_counters.get(gid, 0) if hasattr(bot, '_pipe_counters') else 0,
            })
        result.sort(key=lambda x: x["gid"])
        return result

    @app.get("/api/groups/{gid}")
    async def get_group(gid: int):
        bot = app.state.relay_bot
        if not bot:
            raise HTTPException(404, "机器人未启动")
        mode = bot.GROUP_MODE.get(gid)
        if mode is None:
            raise HTTPException(404, f"群 {gid} 未在配置中")
        conv = bot._load_conv(gid) if hasattr(bot, '_load_conv') else []
        return {
            "gid": gid,
            "mode": mode,
            "history_count": len(conv),
            "threshold": bot._pipe_thresholds.get(gid, None),
            "counter": bot._pipe_counters.get(gid, 0) if hasattr(bot, '_pipe_counters') else 0,
            "recent": list(bot._pipe_recent.get(gid, [])) if hasattr(bot, '_pipe_recent') else [],
        }

    @app.put("/api/groups/{gid}")
    async def update_group(gid: int, update: GroupConfigUpdate):
        bot = app.state.relay_bot
        if not bot:
            raise HTTPException(404, "机器人未启动")
        if gid not in bot.GROUP_MODE:
            raise HTTPException(404, f"群 {gid} 未在配置中")

        changes = []
        if update.mode is not None:
            if update.mode not in ("direct", "pipe"):
                raise HTTPException(422, "mode must be direct or pipe")
            bot.GROUP_MODE[gid] = update.mode
            changes.append(f"mode -> {update.mode}")

        if update.pipe_threshold is not None:
            if update.pipe_threshold < 1 or update.pipe_threshold > 100:
                raise HTTPException(422, "pipe_threshold must be 1-100")
            if hasattr(bot, '_pipe_thresholds'):
                bot._pipe_thresholds[gid] = update.pipe_threshold
            changes.append(f"threshold -> {update.pipe_threshold}")

        return {"ok": True, "changes": changes, "gid": gid}

    # ============ Chat History ============
    @app.get("/api/groups/{gid}/history")
    async def get_history(gid: int, limit: int = 50, offset: int = 0):
        bot = app.state.relay_bot
        if not bot:
            raise HTTPException(404, "机器人未启动")
        conv = bot._load_conv(gid) if hasattr(bot, '_load_conv') else []
        total = len(conv)
        page = conv[offset:offset + limit]
        return {"total": total, "offset": offset, "limit": limit, "messages": page}

    @app.delete("/api/groups/{gid}/history")
    async def clear_history(gid: int):
        bot = app.state.relay_bot
        if not bot:
            raise HTTPException(404, "机器人未启动")
        if hasattr(bot, '_save_conv'):
            bot._save_conv(gid, [])
        return {"ok": True, "gid": gid}

    @app.get("/api/groups/{gid}/history/export")
    async def export_history(gid: int):
        bot = app.state.relay_bot
        if not bot:
            raise HTTPException(404, "机器人未启动")
        conv = bot._load_conv(gid) if hasattr(bot, '_load_conv') else []
        return JSONResponse(
            content=conv,
            headers={"Content-Disposition": f"attachment; filename=conv-{gid}.json"},
        )

    # ============ Manual Send ============
    @app.post("/api/send")
    async def send_message(req: SendMessageRequest):
        bot = app.state.relay_bot
        if not bot:
            raise HTTPException(404, "机器人未启动")
        if not hasattr(bot, 'send_group') or not bot.ws:
            raise HTTPException(503, "机器人未连接")
        try:
            await bot.send_group(req.group_id, req.message)
            await app.state.eventbus.publish({
                "type": "manual_send",
                "data": {"gid": req.group_id, "text": req.message}
            })
            return {"ok": True, "gid": req.group_id}
        except Exception as e:
            raise HTTPException(500, f"发送失败: {e}")

    # ============ Knowledge Base ============
    @app.get("/api/knowledge")
    async def list_knowledge():
        if not KNOWLEDGE_DIR.exists():
            return []
        files = []
        for f in sorted(KNOWLEDGE_DIR.glob("*.md")):
            files.append({
                "name": f.name,
                "size": f.stat().st_size,
                "mtime": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
            })
        return files

    @app.get("/api/knowledge/{filename}")
    async def get_knowledge(filename: str):
        if not filename.endswith(".md"):
            filename += ".md"
        fpath = KNOWLEDGE_DIR / filename
        if not fpath.exists() or not fpath.is_relative_to(KNOWLEDGE_DIR):
            raise HTTPException(404, "文件不存在")
        return {"name": fpath.name, "content": fpath.read_text(encoding="utf-8")}

    @app.put("/api/knowledge/{filename}")
    async def update_knowledge(filename: str, data: KnowledgeUpdate):
        if not filename.endswith(".md"):
            filename += ".md"
        fpath = KNOWLEDGE_DIR / filename
        if not fpath.is_relative_to(KNOWLEDGE_DIR):
            raise HTTPException(400, "非法文件名")
        KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
        fpath.write_text(data.content, encoding="utf-8")
        return {"ok": True, "name": fpath.name}

    @app.delete("/api/knowledge/{filename}")
    async def delete_knowledge(filename: str):
        if not filename.endswith(".md"):
            filename += ".md"
        fpath = KNOWLEDGE_DIR / filename
        if not fpath.exists() or not fpath.is_relative_to(KNOWLEDGE_DIR):
            raise HTTPException(404, "文件不存在")
        fpath.unlink()
        return {"ok": True, "name": fpath.name}

    # ============ Persona ============
    @app.get("/api/persona")
    async def get_persona():
        if not PERSONA_FILE.exists():
            return {"content": "", "exists": False}
        return {"content": PERSONA_FILE.read_text(encoding="utf-8"), "exists": True}

    @app.put("/api/persona")
    async def update_persona(data: PersonaUpdate):
        PERSONA_FILE.parent.mkdir(parents=True, exist_ok=True)
        PERSONA_FILE.write_text(data.content, encoding="utf-8")
        return {"ok": True}

    @app.post("/api/persona/load")
    async def reload_persona():
        bot = app.state.relay_bot
        if not bot:
            raise HTTPException(404, "机器人未启动")
        if hasattr(bot, 'load_persona'):
            import relay
            bot.persona = relay.load_persona()
        if hasattr(bot, 'pipe_persona') and PERSONA_FILE.exists():
            bot.pipe_persona = PERSONA_FILE.read_text(encoding="utf-8").strip()
        return {"ok": True}

    # ============ Logs ============
    @app.get("/api/logs")
    async def get_logs(limit: int = 100):
        log_capture = app.state.log_capture
        if not log_capture:
            return {"lines": []}
        return {"lines": log_capture.get_recent(limit=limit)}

    # ============ WebSocket Real-time Push ============
    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket):
        await websocket.accept()
        eventbus = app.state.eventbus
        queue = eventbus.subscribe()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30)
                    await websocket.send_json(event)
                except asyncio.TimeoutError:
                    try:
                        await websocket.send_json({"type": "ping"})
                    except Exception:
                        break
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            eventbus.unsubscribe(queue)

    # ============ Plugin Management ============
    @app.get("/api/plugins")
    async def list_plugins():
        return {
            "plugins": plugin_mod.get_plugin_info(),
            "groups": plugin_mod.get_groups(),
        }

    @app.put("/api/plugins/{name}")
    async def update_plugin(name: str, request: Request):
        data = await request.json()
        default = data.get("default")
        group = data.get("group")
        group_enabled = data.get("group_enabled")
        plugin_mod.update_plugin(
            name,
            default=default,
            group=str(group) if group is not None else None,
            group_enabled=group_enabled,
        )
        return {"ok": True}

    @app.post("/api/plugins/reload")
    async def reload_plugins():
        plugin_mod.reload_plugins()
        return {"ok": True}

    # ============ SPA Entry ============
    @app.get("/")
    async def serve_spa():
        spa_path = STATIC_DIR / "index.html"
        if spa_path.exists():
            return FileResponse(str(spa_path))
        return {"error": "index.html not found"}

    # Install log capture
    log_capture = LogCapture()
    log_capture.install()
    app.state.log_capture = log_capture

    return app


# ============ Startup Function ============
async def start_webui(bot, eventbus: EventBus, host: str = "127.0.0.1", port: int = 8800):
    """Start WebUI server (runs as an asyncio task)"""
    app = create_app(relay_bot=bot, eventbus=eventbus)

    import uvicorn
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        ws="websockets",
    )
    server = uvicorn.Server(config)
    print(f"[WebUI] Started at http://{host}:{port}")
    await server.serve()


def run_webui_standalone():
    """Standalone mode (for development testing)"""
    app = create_app()
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8800, log_level="info")


if __name__ == "__main__":
    run_webui_standalone()
