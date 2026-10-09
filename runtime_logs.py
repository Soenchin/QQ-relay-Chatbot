"""Redacted stdout/stderr capture with bounded memory and rotating UTF-8 files."""
import atexit
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys
import threading
from collections import deque
from datetime import datetime
from urllib.parse import quote
from uuid import uuid4

MAX_LINES = 1000
MAX_BYTES = 2 * 1024 * 1024
BACKUPS = 1
MAX_PENDING = 65536
REDACTED = '[REDACTED]'
_SECRET_KEYS = ('DEEPSEEK_API_KEY', 'NAPCAT_TOKEN', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'OPENAI_API_KEY')
_BEARER = re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+')
_KEY_VALUE = re.compile(r'''(?ix)
    (\b(?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|token|password|secret|authorization)
    ["']?\s*[:=]\s*)
    (?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s&,;}<>]+)
''')
_URL_AUTH = re.compile(r'(?i)(\b(?:https?|wss?)://)[^/\s@]+@')
_SK_KEY = re.compile(r'\bsk-[A-Za-z0-9_-]{8,}')


class _FileHandler(RotatingFileHandler):
    def handleError(self, record):
        # logging's default handler prints the unsanitized record to stderr and can recurse.
        self.write_failed = True


class _Stream:
    def __init__(self, owner, name, original):
        self.owner, self.name, self.original = owner, name, original

    def write(self, text):
        self.owner.write(text, self.name)
        return len(text)

    def flush(self):
        # Do not publish partial lines: secrets may arrive across separate writes/flushes.
        self.owner.flush()

    def isatty(self):
        return False

    @property
    def encoding(self):
        return getattr(self.original, 'encoding', None) or 'utf-8'

    @property
    def errors(self):
        return 'replace'


class RuntimeLogs:
    def __init__(self, directory, secrets=(), max_lines=MAX_LINES, max_bytes=MAX_BYTES, backups=BACKUPS):
        self.directory = Path(directory)
        self.max_bytes, self.backups = max_bytes, backups
        self.session = uuid4().hex
        self.lines = deque(maxlen=max_lines)
        self.sequence = 0
        self._lock = threading.RLock()
        self._pending = {}
        self._secrets = set()
        self._originals = None
        self._streams = None
        self._file = None
        self._storage_error = None
        self._closed = False
        self.remember_secrets(secrets)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._file = _FileHandler(self.directory / 'relay.log', maxBytes=max_bytes,
                                      backupCount=backups, encoding='utf-8')
            self._file.write_failed = False
            self._file.setFormatter(logging.Formatter('%(message)s'))
        except OSError:
            self._storage_error = '本地日志无法写入，请检查目录权限或磁盘空间；内存日志仍可查看。'
            self.record(self._storage_error, level='WARNING')

    def remember_secrets(self, values):
        with self._lock:
            for value in values:
                if isinstance(value, str) and value:
                    self._secrets.update((value, quote(value, safe=''), json.dumps(value, ensure_ascii=False)[1:-1]))

    def redact(self, text):
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, REDACTED)
        text = _BEARER.sub('Bearer ' + REDACTED, text)
        text = _KEY_VALUE.sub(lambda match: match.group(1) + REDACTED, text)
        text = _URL_AUTH.sub(lambda match: match.group(1) + REDACTED + '@', text)
        return _SK_KEY.sub(REDACTED, text)

    @staticmethod
    def _level(text, stream):
        explicit = re.match(r'^\s*(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\b', text, re.I)
        if explicit:
            return 'WARNING' if explicit.group(1).upper() == 'WARN' else explicit.group(1).upper()
        if any(word in text for word in ('错误', '异常', '失败', 'Traceback')):
            return 'ERROR'
        return 'ERROR' if stream == 'stderr' else 'INFO'

    def record(self, text, stream='stdout', level=None):
        with self._lock:
            if self._closed or not text.strip():
                return
            safe = self.redact(text).replace('\r', '')
            if len(safe) > 8192:
                safe = safe[:8192] + ' …（日志过长，已截断）'
            level = level or self._level(safe, stream)
            self.sequence += 1
            entry = {'id': self.sequence, 'time': datetime.now().astimezone().isoformat(timespec='milliseconds'),
                     'level': level, 'stream': stream, 'msg': safe}
            self.lines.append(entry)
            output = f"{entry['time']} [{level}] [{stream}] {safe}"
            if self._file:
                record = logging.LogRecord('relay', logging.INFO, '', 0, output, (), None)
                self._file.handle(record)
                if self._file.write_failed:
                    self._storage_error = '日志文件写入失败，请检查目录权限或磁盘空间；内存日志仍可查看。'
            # Only redacted text goes to the original console, not the raw write chunks.
            if self._originals:
                original = self._originals[1 if stream == 'stderr' else 0]
                if original:
                    try:
                        original.write(safe + '\n')
                        original.flush()
                    except UnicodeEncodeError:
                        original.write((safe + '\n').encode(getattr(original, 'encoding', None) or 'utf-8', errors='replace').decode(getattr(original, 'encoding', None) or 'utf-8'))
                    except (OSError, ValueError):
                        pass  # Windowed executables can have detached console handles.

    def write(self, text, stream):
        if not isinstance(text, str):
            raise TypeError('日志内容必须是文本')
        key = (threading.get_ident(), stream)
        with self._lock:
            pending, dropping = self._pending.get(key, ('', False))
            parts = text.split('\n')
            for index, part in enumerate(parts):
                if not dropping:
                    if len(pending) + len(part) > MAX_PENDING:
                        pending, dropping = '', True
                    else:
                        pending += part
                if index < len(parts) - 1:
                    self.record('日志行超过长度限制，已整行丢弃。' if dropping else pending, stream)
                    pending, dropping = '', False
            if pending or dropping:
                self._pending[key] = (pending, dropping)
            else:
                self._pending.pop(key, None)

    def snapshot(self, limit=100, after=None, session=None):
        with self._lock:
            items = list(self.lines)
            reset = bool(session and session != self.session) or (after is not None and after > self.sequence)
            gap = bool(items and after is not None and after < items[0]['id'] - 1 and not reset)
            if after is not None and not reset and not gap:
                items = [line for line in items if line['id'] > after]
            # Return newest context on initial load, restart, or a missed buffer window.
            selected = items[-limit:] if after is None or reset or gap else items[:limit]
            return {'lines': selected, 'session': self.session,
                    'cursor': selected[-1]['id'] if selected else self.sequence,
                    'reset': reset, 'gap': gap,
                    'storage': {'directory': str(self.directory.resolve()), 'filename': 'relay.log',
                                'max_bytes': self.max_bytes, 'backups': self.backups,
                                'available': bool(self._file) and not self._storage_error,
                                'error': self._storage_error}}

    def flush(self):
        with self._lock:
            if self._file and not self._closed:
                try:
                    self._file.flush()
                except OSError:
                    self._storage_error = '日志文件刷新失败，请检查磁盘空间；内存日志仍可查看。'

    def install(self):
        with self._lock:
            if self._originals is not None:
                return
            self._originals = (sys.stdout, sys.stderr)
            self._streams = (_Stream(self, 'stdout', sys.stdout), _Stream(self, 'stderr', sys.stderr))
            sys.stdout, sys.stderr = self._streams

    def uninstall(self):
        with self._lock:
            for (_, stream), (pending, dropping) in list(self._pending.items()):
                self.record('日志行超过长度限制，已整行丢弃。' if dropping else pending, stream)
            self._pending.clear()
            if self._originals:
                if sys.stdout is self._streams[0]:
                    sys.stdout = self._originals[0]
                if sys.stderr is self._streams[1]:
                    sys.stderr = self._originals[1]
                self._originals = None

    def close(self):
        self.uninstall()
        with self._lock:
            if self._file:
                try:
                    self._file.close()
                except OSError:
                    self._storage_error = '日志文件关闭时写入失败，请检查磁盘空间。'
            self._closed = True


_runtime = None


def start_runtime_logging(directory=None):
    global _runtime
    if _runtime is None:
        import env_config
        from app_paths import discover_paths
        saved = env_config.read_env_file()
        paths = discover_paths().with_config(env_config.runtime_settings())
        values = [source.get(key, '') for source in (saved, os.environ) for key in _SECRET_KEYS]
        _runtime = RuntimeLogs(directory or paths.logs, secrets=values)
        _runtime.install()
        atexit.register(_runtime.close)
    return _runtime
