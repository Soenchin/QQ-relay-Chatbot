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


def register(name: str, desc: str, handler, default_enabled: bool = True):
    """Register a plugin handler."""
    # Prevent duplicate registration
    for p in _plugins:
        if p.name == name:
            raise ValueError(f"Plugin '{name}' already registered")
    _plugins.append(PluginInfo(name, desc, handler, default_enabled))


def get_all() -> list[PluginInfo]:
    """Return all registered plugins."""
    return _plugins.copy()


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
    # Priority: group setting > default > False
    if gid is not None:
        group_val = plugin_cfg.get("groups", {}).get(str(gid))
        if group_val is not None:
            return group_val
    return plugin_cfg.get("default", False)


def get_plugin_info() -> list:
    """Return list of dicts with name, desc, default, and group settings."""
    config = load_config()
    result = []
    for p in _plugins:
        cfg = config.get(p.name, {})
        result.append({
            "name": p.name,
            "desc": p.desc,
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


def set_group_enabled(name: str, gid: int, enabled: bool, config: dict | None = None):
    if config is None:
        config = load_config()
    if name not in config:
        # Find default from registry
        default_val = True
        for p in _plugins:
            if p.name == name:
                default_val = p.default
                break
        config[name] = {"default": default_val, "groups": {}}
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
        # Find default from registry
        default_val = True
        for p in _plugins:
            if p.name == name:
                default_val = p.default
                break
        config[name] = {"default": default_val, "groups": {}}
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
    Run all registered plugin handlers.
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
            print(f"[Plugin] {p.name} error: {e}")
    return False


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
