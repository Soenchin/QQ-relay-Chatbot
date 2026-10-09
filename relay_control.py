"""Single-owner async relay lifecycle. The WebUI outlives the managed relay."""
import asyncio
import json
import time

import env_config


class RelayControlError(RuntimeError):
    pass


def validate_runtime(config):
    fields = {key: config[key] for key in env_config.DEFAULTS if key in config}
    _, errors = env_config.validate_updates({'values': fields}, {}, {})
    if errors:
        raise RelayControlError('启动配置有误：' + '；'.join(f'{key}: {value}' for key, value in errors.items()))
    try:
        modes = json.loads(config.get('GROUP_MODE') or '{}')
        vision = json.loads(config.get('GROUP_VISION') or '[]')
        if not isinstance(modes, dict) or any(not str(key).isascii() or not str(key).isdigit() or value not in ('direct', 'pipe') for key, value in modes.items()):
            raise ValueError()
        if not isinstance(vision, list) or any(isinstance(value, bool) or not str(value).isascii() or not str(value).isdigit() for value in vision):
            raise ValueError()
        if config.get('FALLBACK_MODE') not in ('direct', 'pipe'):
            raise ValueError()
    except (ValueError, TypeError):
        raise RelayControlError('群设置格式有误，请检查群号、模式和读图列表') from None


def create_runtime(config, paths, eventbus):
    # Import only after validation, never merely to open the configuration page.
    from relay import ManagedRelay
    return ManagedRelay(config, paths, eventbus)


class RelayController:
    def __init__(self, paths, eventbus=None, logs=None, factory=create_runtime, on_change=None,
                 retry_delay=3, stop_timeout=15):
        self.base_paths = paths
        self.paths = paths
        self.eventbus = eventbus
        self.logs = logs
        self.factory = factory
        self.on_change = on_change
        self.retry_delay = retry_delay
        self.stop_timeout = stop_timeout
        self.state = 'stopped'
        self.message = '中继未启动'
        self.runtime = None
        self.task = None
        self.started_at = None
        self.config = None
        self._lock = asyncio.Lock()

    def snapshot(self):
        bot = self.runtime.bot if self.runtime else None
        return {'state': self.state, 'message': self.message,
                'connected': bool(bot and getattr(bot, 'connected', False)),
                'uptime': int(time.time() - self.started_at) if self.started_at else 0,
                'data_dir': str(self.base_paths.data), 'memory_dir': str(self.paths.memory),
                'portable': self.base_paths.portable,
                'can_start': self.runtime is None and self.state in ('stopped', 'error')}

    async def _state(self, state, message):
        self.state, self.message = state, message
        if self.on_change:
            self.on_change(self)
        if self.eventbus:
            await self.eventbus.publish({'type': 'relay_state', 'data': self.snapshot()})

    async def start(self):
        if self._lock.locked():
            raise RelayControlError('已有启停操作正在进行，请稍候')
        async with self._lock:
            return await self._start()

    async def _start(self):
        if self.runtime is not None:
            if self.state == 'error':
                raise RelayControlError('上次资源尚未清理，请先停止中继；不要重复启动')
            return self.snapshot()
        await self._state('starting', '正在读取配置并初始化中继')
        try:
            if not self.base_paths.env.exists() and not env_config.INHERITED_ENV:
                raise RelayControlError('请先在“连接与配置”保存初始配置')
            config = env_config.runtime_settings(self.base_paths.env)
            validate_runtime(config)
            paths = self.base_paths.with_config(config)
            paths.check_writable(include_runtime=True)
            if self.logs:
                self.logs.remember_secrets(config.get(key, '') for key in env_config.SECRET_FIELDS)
            self.runtime = self.factory(config, paths, self.eventbus)
            await self.runtime.start()
            self.paths, self.config = paths, config
            self.started_at = time.time()
            if self.on_change:
                self.on_change(self)
            self.task = asyncio.create_task(self._run(), name='relay-connection')
            print('[中继控制] 已启动，正在连接 OneBot')
        except (Exception, asyncio.CancelledError) as error:
            cleanup_error = None
            if self.runtime is not None:
                try:
                    await asyncio.wait_for(self.runtime.close(), self.stop_timeout)
                    self.runtime = None
                except Exception as failure:
                    cleanup_error = failure
            message = str(error) if isinstance(error, RelayControlError) else '启动失败，请查看运行日志（配置、权限或图床端口可能有误）'
            if cleanup_error:
                message += '；资源清理未完成，请先停止中继'
            print(f'[中继控制] 启动失败: {error}')
            await self._state('error', message)
            if isinstance(error, asyncio.CancelledError):
                raise
            raise RelayControlError(message) from None
        return self.snapshot()

    async def _connected(self):
        await self._state('running', '已连接 OneBot，正在监听消息')

    async def _run(self):
        try:
            while True:
                try:
                    await self.runtime.run(self._connected)
                    reason = 'OneBot 连接已关闭'
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    print(f'[中继控制] 连接异常: {error}')
                    reason = '连接失败或断开'
                await self._state('reconnecting', f'{reason}，{self.retry_delay} 秒后重试')
                await asyncio.sleep(self.retry_delay)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f'[中继控制] 运行异常: {error}')
            await self._state('error', '中继意外停止，请先停止清理后重启')

    async def stop(self):
        if self._lock.locked():
            raise RelayControlError('已有启停操作正在进行，请稍候')
        async with self._lock:
            return await self._stop()

    async def _stop(self):
        if self.runtime is None:
            self.started_at = None
            await self._state('stopped', '中继已停止，控制台仍可使用')
            return self.snapshot()
        await self._state('stopping', '正在关闭连接、任务与自有子进程')
        failures = []
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self.task), self.stop_timeout)
            except asyncio.CancelledError:
                if not self.task.done():
                    raise
            except Exception as error:
                failures.append(error)
        try:
            await asyncio.wait_for(self.runtime.close(), self.stop_timeout)
        except Exception as error:
            failures.append(error)
        if failures:
            print(f'[中继控制] 停止清理失败: {failures[0]}')
            await self._state('error', '停止未完成，仍保留资源归属；请重试停止并查看日志')
            raise RelayControlError(self.message)
        self.runtime = None
        self.task = None
        self.started_at = None
        print('[中继控制] 中继已停止')
        await self._state('stopped', '中继已停止，控制台仍可使用')
        return self.snapshot()

    async def restart(self):
        if self._lock.locked():
            raise RelayControlError('已有启停操作正在进行，请稍候')
        async with self._lock:
            await self._stop()
            return await self._start()

    async def shutdown(self):
        # App shutdown waits for an in-flight action, rather than abandoning its resources.
        async with self._lock:
            await self._stop()
