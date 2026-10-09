"""Stable resource/data paths. Discovery never creates or migrates user data."""
import argparse
from dataclasses import dataclass, replace
import os
from pathlib import Path
import sys
import tempfile


@dataclass(frozen=True)
class AppPaths:
    resources: Path
    data: Path
    memory: Path
    pipe: Path
    logs: Path
    portable: bool

    @property
    def env(self): return self.data / '.env'
    @property
    def knowledge(self): return self.memory / 'knowledge'
    @property
    def persona(self): return self.memory / 'persona.md'
    @property
    def conv(self): return self.memory / 'conv'
    @property
    def memes(self): return self.memory / 'memes'
    @property
    def inbox(self): return self.pipe / 'inbox'
    @property
    def static(self): return self.resources / 'static'

    def with_config(self, config):
        def resolve(value):
            path = Path(value).expanduser()
            return (path if path.is_absolute() else self.data / path).resolve()
        memory = resolve(config.get('MEM_DIR') or './memory')
        pipe = resolve(config.get('PIPE_ADD_DIR') or './memory')
        return replace(self, memory=memory, pipe=pipe,
                       logs=self.data / 'logs' if self.portable else memory / 'logs')

    def check_writable(self, include_runtime=False):
        directories = [self.data]
        if include_runtime:
            directories += [self.memory, self.pipe]
        for directory in dict.fromkeys(directories):
            directory.mkdir(parents=True, exist_ok=True)
            # Fail visibly instead of silently switching to another user-data location.
            with tempfile.TemporaryFile(dir=directory) as probe:
                probe.write(b'path-check')


def discover_paths(data_dir=None, *, source_root=None, frozen=None, executable=None, resources=None):
    source = Path(source_root or Path(__file__).parent).resolve()
    frozen = getattr(sys, 'frozen', False) if frozen is None else frozen
    selected = data_dir if data_dir is not None else os.environ.get('QQBOT_DATA_DIR')
    portable = bool(selected) or frozen
    data = (Path(selected).expanduser().resolve() if selected else
            Path(executable or sys.executable).resolve().parent / 'QQBotData' if frozen else source)
    assets = Path(resources or getattr(sys, '_MEIPASS', source)).resolve()
    return AppPaths(assets, data, data / 'memory', data / 'memory',
                    data / 'logs' if portable else data / 'memory' / 'logs', portable)


def bootstrap_data_dir(argv=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--data-dir')
    args, _ = parser.parse_known_args(argv)
    if args.data_dir:
        os.environ['QQBOT_DATA_DIR'] = str(Path(args.data_dir).expanduser().resolve())


class InstanceLock:
    """OS advisory lock held by an open handle, never by a stale PID file."""
    def __init__(self, data_dir):
        self.path = Path(data_dir) / '.relay.lock'
        self._file = None

    def acquire(self):
        if self._file is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(self.path, 'a+b')
        try:
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            stream.close()
            raise RuntimeError('该数据目录已有控制台或中继运行，请使用已有窗口。') from error
        self._file = stream

    def close(self):
        if self._file is not None:
            self._file.close()  # OS releases the lock even after a crash.
            self._file = None
