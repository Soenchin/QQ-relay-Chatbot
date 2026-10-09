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
import ipaddress
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app_paths import bootstrap_data_dir, discover_paths, InstanceLock

if __name__ == '__main__':
    bootstrap_data_dir()

import env_config
from relay_control import RelayController, RelayControlError
from runtime_logs import RuntimeLogs, start_runtime_logging

if __name__ == '__main__':
    start_runtime_logging()

import sys
sys.path.insert(0, str(Path(__file__).parent))
import plugins as plugin_mod

# ============ Paths ============
BOT_NAME = os.getenv("BOT_NAME", "QQ Bot")

ENV_PATH = env_config.ENV_PATH


def require_local_settings(request: Request) -> None:
    """Credentials/CLI paths may only be configured from a local, same-origin UI."""
    def is_local(host):
        if host == 'localhost':
            return True
        try:
            return ipaddress.ip_address(host or '').is_loopback
        except ValueError:
            return False

    if not request.client or not is_local(request.client.host) or not is_local(request.url.hostname):
        raise HTTPException(403, '配置和运行日志仅允许从本机访问')
    origin = request.headers.get('origin')
    if request.headers.get('sec-fetch-site') == 'cross-site' or (origin and origin != f'{request.url.scheme}://{request.url.netloc}'):
        raise HTTPException(403, '不允许跨站配置请求')


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
def create_app(relay_bot=None, eventbus: EventBus | None = None, log_capture: RuntimeLogs | None = None,
               paths=None, managed=False, auto_start=False, runtime_factory=None) -> FastAPI:
    base = paths or discover_paths()
    env_file = paths.env if paths else ENV_PATH
    if base.env != env_file:
        base = discover_paths(data_dir=env_file.parent)

    def read_env_file():
        return env_config.read_env_file(env_file)

    def write_env_file(updates):
        env_config.write_env_file(updates, env_file)

    @asynccontextmanager
    async def lifespan(app):
        lock = InstanceLock(base.data) if managed else None
        if lock:
            base.check_writable()
            lock.acquire()
        try:
            if auto_start and app.state.controller:
                try:
                    await app.state.controller.start()
                except RelayControlError:
                    pass  # Configuration errors must not prevent opening the console.
            yield
        finally:
            try:
                if app.state.controller:
                    await app.state.controller.shutdown()
            finally:
                if lock:
                    lock.close()

    app = FastAPI(lifespan=lifespan, title=f'{BOT_NAME} Relay WebUI')
    app.state.relay_bot = relay_bot
    app.state.eventbus = eventbus or EventBus()
    app.state.log_capture = log_capture if log_capture is not None else start_runtime_logging()
    app.state.paths = base.with_config(env_config.runtime_settings(env_file))
    app.state.bot_name = env_config.runtime_settings(env_file)['BOT_NAME']
    plugin_mod.configure_paths(app.state.paths)
    app.state.controller = None
    if managed:
        def activated(controller):
            app.state.relay_bot = controller.runtime.bot if controller.runtime else None
            if controller.config is not None:
                app.state.paths = controller.paths
                app.state.bot_name = controller.config['BOT_NAME']
            plugin_mod.configure_paths(app.state.paths)
        kwargs = {'factory': runtime_factory} if runtime_factory else {}
        app.state.controller = RelayController(app.state.paths, app.state.eventbus, app.state.log_capture,
                                               on_change=activated, **kwargs)
    if base.static.exists():
        app.mount('/static', StaticFiles(directory=str(base.static)), name='static')

    # ============ Config ============
    @app.get("/api/config")
    async def get_config():
        return {'bot_name': app.state.bot_name}

    @app.get('/api/relay')
    async def relay_status(request: Request):
        require_local_settings(request)
        controller = app.state.controller
        return JSONResponse({'managed': controller is not None,
                             **(controller.snapshot() if controller else {'state': 'unmanaged'})},
                            headers={'Cache-Control': 'no-store'})

    @app.post('/api/relay/{action}')
    async def relay_action(action: str, request: Request):
        require_local_settings(request)
        if action not in ('start', 'stop', 'restart'):
            raise HTTPException(404, '未知的中继操作')
        controller = app.state.controller
        if controller is None:
            raise HTTPException(409, '当前服务未启用中继管理')
        try:
            # A closed browser request must not abandon half-started/stopped resources.
            result = await asyncio.shield(getattr(controller, action)())
        except RelayControlError as error:
            raise HTTPException(409, str(error)) from None
        return JSONResponse({'managed': True, **result}, headers={'Cache-Control': 'no-store'})

    # ============ Connection Settings ============
    @app.get('/api/settings')
    async def get_settings(request: Request):
        require_local_settings(request)
        try:
            env = read_env_file()
        except (OSError, UnicodeError):
            raise HTTPException(500, '无法读取配置文件，请检查权限和 UTF-8 编码')
        return JSONResponse({**env_config.public_settings(env, env_config.INHERITED_ENV),
                             'env_exists': env_file.exists()}, headers={'Cache-Control': 'no-store'})

    @app.put('/api/settings')
    async def update_settings(request: Request):
        require_local_settings(request)
        if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
            raise HTTPException(415, '请使用 JSON 配置请求')
        if len(await request.body()) > 65536:
            raise HTTPException(413, '配置请求过大')
        try:
            payload = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, '配置请求格式不正确')
        try:
            env = read_env_file()
            updates, errors = env_config.validate_updates(payload, env, env_config.INHERITED_ENV)
            if errors:
                return JSONResponse({'detail': '请检查标出的配置项', 'errors': errors}, status_code=422,
                                    headers={'Cache-Control': 'no-store'})
            # A blank secret is keep, not clear. Only explicit clear_secrets removes it.
            updates = {key: value for key, value in updates.items() if env.get(key) != value}
            if updates:
                app.state.log_capture.remember_secrets(source.get(key, '') for source in (env, updates) for key in env_config.SECRET_FIELDS)
                write_env_file(updates)
            return JSONResponse({'ok': True, 'updated': sorted(updates),
                                 'restart_required': bool(updates),
                                 **env_config.public_settings({**env, **updates}, env_config.INHERITED_ENV)},
                                headers={'Cache-Control': 'no-store'})
        except (OSError, UnicodeError):
            raise HTTPException(500, '保存失败，请检查配置文件权限和 UTF-8 编码；原配置未更新')

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
        ws_ok = bool(getattr(bot, 'connected', False))
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
        PERSONA_FILE = app.state.paths.persona
        KNOWLEDGE_DIR = app.state.paths.knowledge
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
        KNOWLEDGE_DIR = app.state.paths.knowledge
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

    def knowledge_path(filename):
        if not filename.endswith('.md'):
            filename += '.md'
        root = app.state.paths.knowledge.resolve()
        path = (root / filename).resolve()
        if '/' in filename or '\\' in filename or not path.is_relative_to(root):
            raise HTTPException(400, '非法文件名')
        return path

    @app.get("/api/knowledge/{filename}")
    async def get_knowledge(filename: str):
        fpath = knowledge_path(filename)
        if not fpath.exists():
            raise HTTPException(404, '文件不存在')
        return {'name': fpath.name, 'content': fpath.read_text(encoding='utf-8')}

    @app.put("/api/knowledge/{filename}")
    async def update_knowledge(filename: str, data: KnowledgeUpdate):
        fpath = knowledge_path(filename)
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(data.content, encoding='utf-8')
        return {"ok": True, "name": fpath.name}

    @app.delete("/api/knowledge/{filename}")
    async def delete_knowledge(filename: str):
        fpath = knowledge_path(filename)
        if not fpath.exists():
            raise HTTPException(404, '文件不存在')
        fpath.unlink()
        return {"ok": True, "name": fpath.name}

    # ============ Persona ============
    @app.get("/api/persona")
    async def get_persona():
        PERSONA_FILE = app.state.paths.persona
        if not PERSONA_FILE.exists():
            return {"content": "", "exists": False}
        return {"content": PERSONA_FILE.read_text(encoding="utf-8"), "exists": True}

    @app.put("/api/persona")
    async def update_persona(data: PersonaUpdate):
        PERSONA_FILE = app.state.paths.persona
        PERSONA_FILE.parent.mkdir(parents=True, exist_ok=True)
        PERSONA_FILE.write_text(data.content, encoding="utf-8")
        return {"ok": True}

    @app.post("/api/persona/load")
    async def reload_persona():
        PERSONA_FILE = app.state.paths.persona
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
    async def get_logs(request: Request, limit: int = Query(200, ge=1, le=1000),
                       after: int | None = Query(None, ge=0), session: str | None = Query(None, max_length=64)):
        require_local_settings(request)
        return JSONResponse(app.state.log_capture.snapshot(limit=limit, after=after, session=session),
                            headers={'Cache-Control': 'no-store'})

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
        spa_path = base.static / 'index.html'
        if spa_path.exists():
            return FileResponse(str(spa_path))
        return {"error": "index.html not found"}

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
        access_log=False,  # Polling logs must not produce another access-log entry.
        ws="websockets",
    )
    server = uvicorn.Server(config)
    print(f"[WebUI] Started at http://{host}:{port}")
    await server.serve()


async def serve_managed_webui(paths=None, auto_start=False):
    paths = paths or discover_paths()
    config = env_config.runtime_settings(paths.env)
    # Bind once per console process. Worker restart deliberately does not rebind the UI.
    host, port = config['WEBUI_HOST'], config['WEBUI_PORT']
    _, errors = env_config.validate_updates({'values': {'WEBUI_HOST': host, 'WEBUI_PORT': port}}, {}, {})
    if errors:
        print('[WebUI] 监听配置无效，使用本机 127.0.0.1:8800 供修复配置')
        host, port = '127.0.0.1', '8800'
    app = create_app(paths=paths, managed=True, auto_start=auto_start)
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=int(port), log_level='info', access_log=False, ws='websockets'))
    await server.serve()


def run_webui_standalone():
    """Open the controller without connecting QQ until Start is clicked."""
    asyncio.run(serve_managed_webui(auto_start='--start' in sys.argv))


if __name__ == "__main__":
    run_webui_standalone()
