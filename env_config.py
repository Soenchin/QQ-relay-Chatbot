"""Plain .env configuration shared by the relay and WebUI (no interpolation)."""
import ipaddress
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from app_paths import discover_paths

ENV_PATH = discover_paths().env
DEFAULTS = {
    'BOT_NAME': 'QQ Bot',
    'MASTER_QQ': '0',
    'NAPCAT_WS_URL': 'ws://127.0.0.1:3001',
    'NAPCAT_TOKEN': '',
    'DEEPSEEK_BASE_URL': 'https://api.deepseek.com/anthropic',
    'DEEPSEEK_API_KEY': '',
    'DEEPSEEK_MODEL': 'deepseek-v4-flash',
    'CLAUDE_CMD': 'claude.cmd' if os.name == 'nt' else 'claude',
    'MEM_DIR': './memory',
    'PIPE_ADD_DIR': './memory',
    'MEME_SERVER_PORT': '8801',
    'WEBUI_ENABLED': 'false',
    'WEBUI_HOST': '127.0.0.1',
    'WEBUI_PORT': '8800',
}
# Capture before relay loads .env so file values are not misreported as OS overrides.
INHERITED_ENV = {key: os.environ[key] for key in (*DEFAULTS, 'GROUP_MODE', 'GROUP_VISION', 'FALLBACK_MODE') if key in os.environ}
SECRET_FIELDS = frozenset({'NAPCAT_TOKEN', 'DEEPSEEK_API_KEY'})
PORT_FIELDS = frozenset({'MEME_SERVER_PORT', 'WEBUI_PORT'})


def read_env_file(path: Path = ENV_PATH) -> dict:
    result = {}
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                result[key.strip()] = value.strip()
    return result


def runtime_settings(path: Path = ENV_PATH) -> dict:
    """Re-read saved settings without mutating os.environ; restart sees fresh file values."""
    return {**DEFAULTS, 'GROUP_MODE': '', 'GROUP_VISION': '', 'FALLBACK_MODE': 'direct',
            **read_env_file(path), **INHERITED_ENV}


def write_env_file(updates: dict, path: Path = ENV_PATH) -> None:
    """Preserve unrelated settings/comments and replace the file atomically."""
    lines = path.read_text(encoding='utf-8-sig').splitlines() if path.exists() else []
    updated = set()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith('#') and '=' in stripped:
            key = stripped.split('=', 1)[0].strip()
            if key in updates:
                lines[index] = f'{key}={updates[key]}'
                updated.add(key)
    lines.extend(f'{key}={value}' for key, value in updates.items() if key not in updated)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.env-', suffix='.tmp', delete=False) as stream:
            temp_path = Path(stream.name)
            stream.write('\n'.join(lines) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def public_settings(env: dict, inherited: dict) -> dict:
    """Only return editable non-secrets and presence flags, never masked key suffixes."""
    return {
        'values': {key: env.get(key, inherited.get(key, default))
                   for key, default in DEFAULTS.items() if key not in SECRET_FIELDS},
        'secrets': {key: {'configured': bool(env.get(key, inherited.get(key, '')))}
                    for key in sorted(SECRET_FIELDS)},
        'environment_overrides': sorted(key for key in DEFAULTS if key in inherited),
    }


def validate_updates(payload, env: dict, inherited: dict) -> tuple[dict, dict]:
    """Validate the entire patch before touching disk; messages contain no input values."""
    if not isinstance(payload, dict) or set(payload) - {'values', 'clear_secrets'}:
        return {}, {'_form': '配置请求格式不正确'}
    values = payload.get('values', {})
    clear = payload.get('clear_secrets', [])
    if not isinstance(values, dict) or not isinstance(clear, list):
        return {}, {'_form': '配置请求格式不正确'}
    if any(key not in DEFAULTS for key in values) or any(not isinstance(key, str) or key not in SECRET_FIELDS for key in clear):
        return {}, {'_form': '包含不支持的配置项'}
    errors = {}
    updates = {}
    for key, raw in values.items():
        if not isinstance(raw, str):
            errors[key] = '请输入文本值'
            continue
        if len(raw) > 4096 or any(ord(char) < 32 or char in '\x7f\x85\u2028\u2029' for char in raw):
            errors[key] = '不允许换行、控制字符或超过 4096 个字符'
            continue
        value = raw.strip()
        if key in SECRET_FIELDS:
            if value and key in clear:
                errors[key] = '不能同时替换和清除密钥'
            elif value:
                updates[key] = value
            continue  # Empty secret inputs deliberately mean keep the existing secret.
        if not value:
            errors[key] = '此项不能为空'
            continue
        if key == 'BOT_NAME' and len(value) > 60:
            errors[key] = '名称不能超过 60 个字符'
        elif key == 'MASTER_QQ' and (not value.isascii() or not value.isdecimal() or len(value) > 20):
            errors[key] = '请输入 QQ 号；0 表示不指定主人'
        elif key in PORT_FIELDS:
            if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 65535:
                errors[key] = '端口应为 1–65535 的整数'
        elif key in {'NAPCAT_WS_URL', 'DEEPSEEK_BASE_URL'}:
            schemes = {'ws', 'wss'} if key == 'NAPCAT_WS_URL' else {'http', 'https'}
            try:
                url = urlsplit(value)
                valid = (url.scheme in schemes and bool(url.hostname) and url.username is None
                         and url.password is None and not url.query and not url.fragment
                         and not any(char.isspace() for char in value))
                if url.port is not None and not 1 <= url.port <= 65535:
                    valid = False
            except ValueError:
                valid = False
            if not valid:
                errors[key] = '请输入有效连接地址，不要在地址中夹带账号、密钥、查询参数或片段'
        elif key == 'WEBUI_HOST':
            try:
                valid = value == 'localhost' or ipaddress.ip_address(value).is_loopback
            except ValueError:
                valid = False
            if not valid:
                errors[key] = '此管理界面没有登录鉴权，只允许 localhost 或回环 IP'
        elif key == 'WEBUI_ENABLED' and value not in {'true', 'false'}:
            errors[key] = '请选择开启或关闭'
        updates[key] = value
    for key in clear:
        updates[key] = ''
    effective = {**DEFAULTS, **inherited, **env, **updates}
    if PORT_FIELDS.intersection(updates) and effective['MEME_SERVER_PORT'] == effective['WEBUI_PORT']:
        for key in PORT_FIELDS.intersection(updates):
            errors[key] = 'WebUI 与图床不能使用同一个端口'
    return updates, errors
