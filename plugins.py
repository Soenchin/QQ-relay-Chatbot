#!/usr/bin/env python3
"""
Plugin system for QQ relay bot.
Register plugins, per-group enable/disable, and run them in hooks.

How to add a plugin:
  1. Create an async function: async def my_plugin(bot, gid, uid, nick, text, is_at):
         # bot: RelayBot instance
         # gid, uid, nick, text, is_at: message context
         # return True if the plugin handled the message (AI will not reply)
     
  2. Register it at the bottom of this file or in your startup script:
     plugins.register("my_plugin", "Description", my_plugin, default_enabled=True)

  3. The plugin will be called in every group where it is enabled.
"""

import json
import os
from pathlib import Path

# ===== Config =====
MEM_DIR = Path(os.getenv("MEM_DIR", str(Path(__file__).parent / "memory")))
PLUGINS_CONFIG = MEM_DIR / "plugins.json"


# ===== Plugin Info =====
class PluginInfo:
    __slots__ = ("name", "desc", "handler", "default")

    def __init__(self, name: str, desc: str, handler, default_enabled: bool = True):
        self.name = name
        self.desc = desc
        self.handler = handler
        self.default = default_enabled


# ===== Registry =====
_plugins: list[PluginInfo] = []
_notice_plugins: list[PluginInfo] = []
_request_plugins: list[PluginInfo] = []


def register(name: str, desc: str, handler, default_enabled: bool = True):
    """Register a message plugin handler."""
    for p in _plugins:
        if p.name == name:
            raise ValueError(f"Plugin '{name}' already registered")
    _plugins.append(PluginInfo(name, desc, handler, default_enabled))


def register_notice(name: str, desc: str, handler, default_enabled: bool = True):
    """Register a notice event plugin (poke, group_increase, group_decrease, etc.)."""
    for p in _notice_plugins:
        if p.name == name:
            raise ValueError(f"Notice plugin '{name}' already registered")
    _notice_plugins.append(PluginInfo(name, desc, handler, default_enabled))


def register_request(name: str, desc: str, handler, default_enabled: bool = True):
    """Register a request event plugin (group join request, friend request, etc.)."""
    for p in _request_plugins:
        if p.name == name:
            raise ValueError(f"Request plugin '{name}' already registered")
    _request_plugins.append(PluginInfo(name, desc, handler, default_enabled))


def get_all() -> list[PluginInfo]:
    """Return all registered message plugins."""
    return _plugins.copy()


def get_all_notice() -> list[PluginInfo]:
    """Return all registered notice plugins."""
    return _notice_plugins.copy()


def get_all_request() -> list[PluginInfo]:
    """Return all registered request plugins."""
    return _request_plugins.copy()


# ===== Config Management =====
def load_config() -> dict:
    if not PLUGINS_CONFIG.exists():
        return {}
    try:
        return json.loads(PLUGINS_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(config: dict):
    PLUGINS_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    PLUGINS_CONFIG.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def is_enabled(name: str, gid: int | None = None, config: dict | None = None) -> bool:
    if config is None:
        config = load_config()
    plugin_cfg = config.get(name, {})
    # Priority: 群设置 > 配置文件default > 注册时default_enabled > False
    if gid is not None:
        group_val = plugin_cfg.get("groups", {}).get(str(gid))
        if group_val is not None:
            return group_val
    if "default" in plugin_cfg:
        return plugin_cfg["default"]
    # 回退到注册时的 default_enabled
    for registry in (_plugins, _notice_plugins, _request_plugins):
        for p in registry:
            if p.name == name:
                return p.default
    return False


def get_plugin_info() -> list:
    """Return list of dicts with name, desc, type, default, and group settings."""
    config = load_config()
    result = []
    for registry, ptype in [
        (_plugins, "message"),
        (_notice_plugins, "notice"),
        (_request_plugins, "request"),
    ]:
        for p in registry:
            cfg = config.get(p.name, {})
            result.append({
                "name": p.name,
                "desc": p.desc,
                "type": ptype,
                "default": cfg.get("default", p.default),
                "groups": cfg.get("groups", {}),
            })
    return result


def get_groups() -> list:
    """Return all known group IDs from plugin config."""
    config = load_config()
    groups = set()
    for cfg in config.values():
        for gid in cfg.get("groups", {}).keys():
            groups.add(int(gid))
    return sorted(groups)


def _find_default(name: str) -> bool:
    """Search all registries for a plugin's default_enabled."""
    for registry in (_plugins, _notice_plugins, _request_plugins):
        for p in registry:
            if p.name == name:
                return p.default
    return True


def set_group_enabled(name: str, gid: int, enabled: bool, config: dict | None = None):
    if config is None:
        config = load_config()
    if name not in config:
        config[name] = {"default": _find_default(name), "groups": {}}
    if "groups" not in config[name]:
        config[name]["groups"] = {}
    if enabled:
        config[name]["groups"][str(gid)] = True
    else:
        config[name]["groups"].pop(str(gid), None)
    save_config(config)
    return config


def update_plugin(name: str, default: bool | None = None, group: str | None = None, group_enabled: bool | None = None):
    config = load_config()
    if name not in config:
        config[name] = {"default": _find_default(name), "groups": {}}
    if default is not None:
        config[name]["default"] = default
    if group is not None and group_enabled is not None:
        if "groups" not in config[name]:
            config[name]["groups"] = {}
        if group_enabled:
            config[name]["groups"][str(group)] = True
        else:
            config[name]["groups"].pop(str(group), None)
    save_config(config)


def reload_plugins():
    """Reload plugin config from disk."""
    pass  # Config is always read from disk, but can be used for cache invalidation


# ===== Plugin Runner =====
async def run_plugins(bot, gid: int, uid: int, nick: str, text: str, is_at: bool = False) -> bool:
    """
    Run all registered message plugin handlers.
    Returns True if any plugin handled (intercepted) the message.
    """
    config = load_config()
    for p in _plugins:
        if not is_enabled(p.name, gid, config):
            continue
        try:
            result = await p.handler(bot, gid, uid, nick, text, is_at)
            if result is True:
                return True
        except Exception as e:
            print(f"[Plugin:message] {p.name} error: {e}")
    return False


async def run_notice_plugins(bot, data: dict) -> None:
    """
    Run all registered notice event plugins.
    Handler signature: async def handler(bot, data) -> None
    data: full OneBot notice event dict.
    """
    config = load_config()
    gid = data.get("group_id")
    for p in _notice_plugins:
        if not is_enabled(p.name, gid, config):
            continue
        try:
            await p.handler(bot, data)
        except Exception as e:
            print(f"[Plugin:notice] {p.name} error: {e}")


async def run_request_plugins(bot, data: dict) -> None:
    """
    Run all registered request event plugins.
    Handler signature: async def handler(bot, data) -> None
    data: full OneBot request event dict.
    """
    config = load_config()
    gid = data.get("group_id")
    for p in _request_plugins:
        if not is_enabled(p.name, gid, config):
            continue
        try:
            await p.handler(bot, data)
        except Exception as e:
            print(f"[Plugin:request] {p.name} error: {e}")


# ===== Example plugins (uncomment to enable) =====

# async def handle_hello(bot, gid, uid, nick, text, is_at):
#     if "你好" in text:
#         await bot.send_group(gid, f"{nick} 你好！")
#         return True
#     return None

# async def handle_keyword_reply(bot, gid, uid, nick, text, is_at):
#     keywords = {
#         "早安": "早安！新的一天也要元气满满！",
#         "晚安": "晚安，做个好梦~",
#     }
#     for kw, reply in keywords.items():
#         if kw in text:
#             await bot.send_group(gid, reply)
#             return True
#     return None

# register("hello", "回复'你好'", handle_hello, default_enabled=True)
# register("keyword_reply", "关键词自动回复", handle_keyword_reply, default_enabled=False)


# ==============================================================================
# 三 个 事 件 插 件 ： 戳 一 戳 回 戳 / 加 群 申 请 通 知 / 入 群 欢 迎
# ==============================================================================

import time as _time

# 戳一戳冷却缓存 { "gid:uid": 上次触发时间戳 }
_poke_cooldown: dict[str, float] = {}
_POKE_COOLDOWN_SEC = 10


async def poke_reply(bot, data: dict):
    """被戳回戳，10 秒冷却"""
    if data.get("notice_type") != "notify":
        return
    if data.get("sub_type") != "poke":
        return

    gid = data.get("group_id")
    uid = data.get("user_id")
    target = data.get("target_id")

    # 只处理戳机器人的情况，别人互戳不管
    if target != bot.bot_qq:
        return

    # 冷却检查
    key = f"{gid}:{uid}"
    now = _time.time()
    last = _poke_cooldown.get(key, 0)
    if now - last < _POKE_COOLDOWN_SEC:
        return
    _poke_cooldown[key] = now

    # 戳回去
    import json
    await bot.ws.send(json.dumps({
        "action": "group_poke",
        "params": {"group_id": gid, "user_id": uid}
    }))
    print(f"[插件:poke] 群 {gid} | 回戳 {uid}")


async def join_request_notify(bot, data: dict):
    """加群申请时在群内提示"""
    if data.get("request_type") != "group":
        return
    if data.get("sub_type") != "add":
        return

    gid = data.get("group_id")
    if not gid:
        return

    await bot.send_group(gid, "好像有加群申请哦~")
    print(f"[插件:join_request] 群 {gid} | 提示加群申请")


async def group_welcome(bot, data: dict):
    """新人入群 @欢迎"""
    if data.get("notice_type") != "group_increase":
        return

    gid = data.get("group_id")
    uid = data.get("user_id")

    if not gid or not uid:
        return

    msg = f"[CQ:at,qq={uid}] 欢迎入群！"
    await bot.send_group(gid, msg)
    print(f"[插件:welcome] 群 {gid} | 欢迎 {uid}")


# 注册三个插件
register_notice("poke_reply", "被戳回戳（10秒冷却）", poke_reply, default_enabled=True)
register_request("join_request_notify", "加群申请通知", join_request_notify, default_enabled=True)
register_notice("group_welcome", "入群欢迎", group_welcome, default_enabled=True)
