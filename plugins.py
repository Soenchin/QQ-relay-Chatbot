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

# ==============================================================================
# 链接摘要插件：B站 / GitHub / 通用网页（原 link_summary_plugin.py，并入 plugins）
# ==============================================================================

import re
from urllib.parse import urlparse

import httpx

# ============================================================
#  通用工具
# ============================================================

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}


def strip_cq_segments(text: str) -> str:
    """去掉 CQ 段，避免把 CQ:image 的 url= 当成网页链接。"""
    return re.sub(r"\[CQ:[^\]]*\]", " ", text or "")


def extract_urls(text: str) -> list[str]:
    """从文本中提取网页 URL（忽略图片/表情等 CQ 内嵌链）。"""
    clean = strip_cq_segments(text)
    urls = re.findall(r"https?://[^\s,，。、<>\"']+", clean)
    # 再挡一层：QQ 多媒体直链 / 常见图床直链不算“网页摘要”
    blocked = (
        "multimedia.nt.qq.com.cn",
        "gchat.qpic.cn",
        "c2cpicdw.qpic.cn",
        "thirdqq.qlogo.cn",
        "qpic.cn/",
    )
    out = []
    for u in urls:
        low = u.lower()
        if any(b in low for b in blocked):
            continue
        # 纯图片后缀也跳过
        if re.search(r"\.(jpg|jpeg|png|gif|webp|bmp)(?:\?|$)", low):
            continue
        out.append(u.rstrip(")]}>'\",.;"))
    return out


SUMMARY_MAX_CHARS = 100

# 统计行必须完整保留，并始终放在摘要最后一行
_STATS_LINE_RE = re.compile(
    r"(?:^播放\s|^\s*播放\s)|(?:\bstar\b.*\bfork\b)|(?:点赞)|(?:弹幕)",
    re.IGNORECASE,
)


def _is_stats_line(line: str) -> bool:
    s = (line or "").strip()
    if not s or s.startswith("[CQ:"):
        return False
    if s.startswith("播放 ") or " | 点赞 " in s or " | 弹幕 " in s:
        return True
    if s.startswith("star ") or " · " in s and ("star " in s or "fork " in s):
        return True
    # GitHub meta: star x · fork y · ...
    if re.search(r"\bstar\b", s, re.I) and re.search(r"\bfork\b", s, re.I):
        return True
    return False


def truncate_summary(text: str, limit: int = SUMMARY_MAX_CHARS) -> str:
    """整条摘要总长限制。

    - CQ:image 行保留
    - 统计行（播放/点赞/弹幕 或 star/fork）始终完整，并放在最后新起一行
    - 其余文字超过 limit 截断加 ...
    """
    if not text:
        return text
    lines = text.split("\n")
    cq_lines = []
    stats_lines = []
    body_lines = []
    for line in lines:
        if line.startswith("[CQ:image,"):
            cq_lines.append(line)
        elif _is_stats_line(line):
            stats_lines.append(line.strip())
        else:
            body_lines.append(line)
    body = "\n".join(body_lines).strip()
    if len(body) > limit:
        body = body[:limit].rstrip() + "..."
    parts = []
    parts.extend(cq_lines)
    if body:
        parts.append(body)
    # 统计数据无论如何接上，最后新起一行
    if stats_lines:
        # 通常只有一行；多行也逐行放最后
        parts.extend(stats_lines)
    return "\n".join(parts)


async def fetch_json(client: httpx.AsyncClient, url: str, headers: dict | None = None) -> dict | None:
    """安全的 GET → JSON，失败返回 None"""
    try:
        resp = await client.get(url, headers={**UA, **(headers or {})}, timeout=15.0, follow_redirects=True)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[链接摘要] 请求失败: {url} — {e}")
        return None


# ============================================================
#  B站
# ============================================================

BV_RE = re.compile(r"[Bb][Vv][0-9A-Za-z]{10,12}")


def extract_bvid(url: str) -> str | None:
    m = BV_RE.search(url or "")
    if m:
        return m.group()
    path = urlparse(url or "").path
    if "/video/" in path:
        seg = path.split("/video/")[-1].split("/")[0].split("?")[0]
        if BV_RE.match(seg):
            return seg
    return None


def fmt_bilibili(data: dict) -> str | None:
    d = data.get("data")
    if not d:
        return None

    title = (d.get("title") or "").strip()
    desc = (d.get("desc") or d.get("dynamic") or "").strip()
    stat = d.get("stat") or {}
    owner = d.get("owner") or {}
    pic = d.get("pic") or ""

    view = stat.get("view", 0)
    like = stat.get("like", 0)
    danmaku = stat.get("danmaku", 0)
    uname = owner.get("name", "")

    if len(desc) > 120:
        desc = desc[:120] + "…"

    def fmt_num(n: int) -> str:
        try:
            n = int(n)
        except Exception:
            return str(n)
        if n >= 10000:
            return f"{n / 10000:.1f}万"
        return str(n)

    lines = []
    if pic:
        lines.append(f"[CQ:image,file={pic}]")
    lines.append(f"【{uname}】{title}" if uname else title or "B站视频")
    if desc:
        lines.append(desc)
    lines.append(f"播放 {fmt_num(view)} | 点赞 {fmt_num(like)} | 弹幕 {fmt_num(danmaku)}")
    return "\n".join(lines)


async def handle_bilibili(url: str, client: httpx.AsyncClient) -> str | None:
    bvid = extract_bvid(url)

    if not bvid and "b23.tv" in (url or ""):
        try:
            resp = await client.get(url, headers=UA, follow_redirects=True, timeout=10.0)
            bvid = extract_bvid(str(resp.url))
        except Exception as e:
            print(f"[链接摘要] b23.tv 展开失败: {e}")

    if not bvid:
        return None

    api_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
    data = await fetch_json(client, api_url)
    if not data:
        return None
    return fmt_bilibili(data)


# ============================================================
#  GitHub
# ============================================================

GH_RE = re.compile(r"github\.com[/:]([^/\s]+)/([^/\s#?]+)")


def extract_gh(url: str) -> tuple[str, str] | None:
    m = GH_RE.search(url or "")
    if not m:
        return None
    owner = m.group(1).strip()
    repo = m.group(2).strip().replace(".git", "")
    return owner, repo


def fmt_github(data: dict, owner: str, repo: str) -> str | None:
    full_name = data.get("full_name", f"{owner}/{repo}")
    desc = (data.get("description") or "").strip()
    lang = data.get("language") or ""
    stars = data.get("stargazers_count", 0)
    forks = data.get("forks_count", 0)
    issues = data.get("open_issues_count", 0)
    license_info = data.get("license")
    topics = data.get("topics") or []
    avatar = (data.get("owner") or {}).get("avatar_url", "")

    def fmt_num(n: int) -> str:
        try:
            n = int(n)
        except Exception:
            return str(n)
        if n >= 1000:
            return f"{n / 1000:.1f}k"
        return str(n)

    lines = []
    if avatar:
        lines.append(f"[CQ:image,file={avatar}]")
    lines.append(f"GitHub {full_name}")
    if desc:
        if len(desc) > 150:
            desc = desc[:150] + "…"
        lines.append(desc)

    meta = [f"star {fmt_num(stars)}", f"fork {fmt_num(forks)}"]
    if lang:
        meta.append(lang)
    if license_info and license_info.get("spdx_id") and license_info["spdx_id"] != "NOASSERTION":
        meta.append(license_info["spdx_id"])
    if issues:
        meta.append(f"issues {fmt_num(issues)}")
    lines.append(" · ".join(meta))

    if topics:
        lines.append(" ".join(f"#{t}" for t in topics[:5]))
    return "\n".join(lines)


async def handle_github(url: str, client: httpx.AsyncClient) -> str | None:
    parsed = extract_gh(url)
    if not parsed:
        return None
    owner, repo = parsed
    api_url = f"https://api.github.com/repos/{owner}/{repo}"
    data = await fetch_json(client, api_url, headers={"Accept": "application/vnd.github+json"})
    if not data:
        return None
    return fmt_github(data, owner, repo)


# ============================================================
#  通用 OG 兜底
# ============================================================

def _meta_content(html: str, prop: str) -> str:
    # property/name 在 content 前或后都尽量匹配
    patterns = [
        rf'<meta\s+[^>]*(?:property|name)=["\']{re.escape(prop)}["\'][^>]*content=["\']([^"\']+)["\']',
        rf'<meta\s+[^>]*content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']{re.escape(prop)}["\']',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.I)
        if m:
            return m.group(1).strip()
    return ""


async def handle_generic(url: str, client: httpx.AsyncClient) -> str | None:
    try:
        resp = await client.get(url, headers=UA, timeout=15.0, follow_redirects=True)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        print(f"[链接摘要] 通用抓取失败: {url} — {e}")
        return None

    title = _meta_content(html, "og:title")
    if not title:
        m = re.search(r"<title>([^<]+)</title>", html, re.I)
        if m:
            title = m.group(1).strip()

    desc = _meta_content(html, "og:description")
    if not desc:
        desc = _meta_content(html, "description")
    image = _meta_content(html, "og:image")

    if not title and not desc:
        return None

    title = (title or url).strip()[:100]
    desc = (desc or "").strip()
    if len(desc) > 150:
        desc = desc[:150] + "…"

    lines = []
    if image:
        lines.append(f"[CQ:image,file={image}]")
    lines.append(f"链接 {title}")
    if desc:
        lines.append(desc)
    lines.append(urlparse(url).netloc)
    return "\n".join(lines)


# ============================================================
#  去重 / 限流
# ============================================================

_seen_links: dict[str, float] = {}
_SEEN_TTL = 300  # 5 分钟

_group_rates: dict[int, list[float]] = {}
_RATE_LIMIT = 3
_RATE_WIN = 60


def _check_rate(gid: int) -> bool:
    now = _time.time()
    bucket = _group_rates.setdefault(gid, [])
    _group_rates[gid] = [t for t in bucket if now - t < _RATE_WIN]
    if len(_group_rates[gid]) >= _RATE_LIMIT:
        return False
    _group_rates[gid].append(now)
    return True


async def link_summary_handler(bot, gid: int, uid: int, nick: str, text: str, is_at: bool) -> bool:
    """
    返回 True = 截胡 AI；False = AI 可继续。
    有链接时尽量发摘要；拉不到就发（链接内容拉取失败）。
    """
    urls = extract_urls(text)
    if not urls:
        return False

    url = urls[0]
    now = _time.time()
    link_key = f"{gid}:{url}"
    if link_key in _seen_links and now - _seen_links[link_key] < _SEEN_TTL:
        return False
    _seen_links[link_key] = now

    if not _check_rate(gid):
        print(f"[链接摘要] 群 {gid} 频率超限，跳过")
        return False

    print(f"[链接摘要] 群 {gid} | {nick}({uid}) | {url}")

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=UA) as client:
            reply = None
            if "bilibili.com" in url or "b23.tv" in url:
                reply = await handle_bilibili(url, client)
            elif "github.com" in url:
                reply = await handle_github(url, client)
            else:
                reply = await handle_generic(url, client)

            if not reply:
                await bot.send_group(gid, "（链接内容拉取失败）")
                print(f"[链接摘要] 拉取失败: {url}")
                return False

            reply = truncate_summary(reply)
            await bot.send_group(gid, reply)
            print(f"[链接摘要] 已发送: {url} ({len(reply)} chars)")
            return False
    except Exception as e:
        print(f"[链接摘要] 异常: {e}")
        try:
            await bot.send_group(gid, "（链接内容拉取失败）")
        except Exception as e2:
            print(f"[链接摘要] 失败提示也没发出去: {e2}")
        return False




register(
    "link_summary",
    "链接自动摘要 — B站/GitHub/通用网页",
    link_summary_handler,
    default_enabled=True,
)
print("[插件] link_summary 已注册")
