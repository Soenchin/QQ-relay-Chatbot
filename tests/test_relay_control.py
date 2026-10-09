import asyncio
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app_paths import discover_paths, InstanceLock
import env_config
from relay_control import RelayController, RelayControlError


class FakeRuntime:
    def __init__(self, config, paths, eventbus=None):
        self.config, self.paths = config, paths
        self.bot = SimpleNamespace(connected=False, bot_qq=123, ws=None, _start_time=time.time(),
            GROUP_MODE={}, PIPE_GROUPS=[], GROUP_VISION=set())
        self.started = 0
        self.closed = 0
        self.connects = 0
        self.fail_start = False
        self.fail_close = False
        self.break_connection = asyncio.Event()
        self.start_gate = None

    async def start(self):
        self.started += 1
        if self.start_gate: await self.start_gate.wait()
        if self.fail_start: raise OSError('test init failure')

    async def run(self, on_connected):
        self.connects += 1
        self.bot.connected = True
        await on_connected()
        try:
            await self.break_connection.wait()
            self.break_connection.clear()
        finally:
            self.bot.connected = False

    async def close(self):
        self.closed += 1
        self.bot.connected = False
        if self.fail_close: raise OSError('test cleanup failure')


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = discover_paths(data_dir=self.tmp.name)
        env_config.write_env_file({**env_config.DEFAULTS, 'GROUP_MODE': '{}'}, self.paths.env)
        override = patch.object(env_config, 'INHERITED_ENV', {})
        override.start()
        self.addCleanup(override.stop)
        self.instances = []
        def factory(*args):
            runtime = FakeRuntime(*args)
            self.instances.append(runtime)
            return runtime
        self.controller = RelayController(self.paths, factory=factory, retry_delay=.01, stop_timeout=.1)
        self.addAsyncCleanup(self.controller.shutdown)

    async def wait_state(self, state):
        for _ in range(100):
            if self.controller.state == state: return
            await asyncio.sleep(.005)
        self.fail(f'Expected {state}, got {self.controller.state}')

    async def test_start_stop_restart_reload_saved_configuration(self):
        await self.controller.start()
        await self.wait_state('running')
        first = self.instances[0]
        self.assertTrue(self.controller.snapshot()['connected'])
        env_config.write_env_file({'BOT_NAME': 'New Bot', 'MEM_DIR': 'new-memory', 'DEEPSEEK_MODEL': 'new-model'}, self.paths.env)
        await self.controller.restart()
        await self.wait_state('running')
        self.assertEqual(first.closed, 1)
        self.assertEqual(self.instances[-1].config['BOT_NAME'], 'New Bot')
        self.assertEqual(self.instances[-1].config['DEEPSEEK_MODEL'], 'new-model')
        self.assertEqual(self.controller.paths.memory, self.paths.data / 'new-memory')
        await self.controller.stop()
        self.assertEqual(self.controller.state, 'stopped')
        self.assertIsNone(self.controller.runtime)
        self.assertFalse(self.controller.snapshot()['connected'])
        await self.controller.stop()
        self.assertEqual(self.instances[-1].closed, 1)

    async def test_repeated_start_never_creates_second_runtime(self):
        await asyncio.gather(*[self.controller.start() for _ in range(10)])
        self.assertEqual(len(self.instances), 1)
        self.assertEqual(self.instances[0].started, 1)

    async def test_starting_rejects_overlapping_action(self):
        gate = asyncio.Event()
        def factory(*args):
            result = FakeRuntime(*args)
            result.start_gate = gate
            self.instances.append(result)
            return result
        self.controller.factory = factory
        starting = asyncio.create_task(self.controller.start())
        await asyncio.sleep(.01)
        for operation in [self.controller.start, self.controller.stop, self.controller.restart]:
            with self.assertRaises(RelayControlError): await operation()
        gate.set()
        await starting
        self.assertEqual(len(self.instances), 1)

    async def test_connection_close_retries_but_stop_cancels_retry(self):
        await self.controller.start()
        await self.wait_state('running')
        runtime = self.instances[0]
        runtime.break_connection.set()
        for _ in range(100):
            if runtime.connects >= 2: break
            await asyncio.sleep(.005)
        self.assertGreaterEqual(runtime.connects, 2)
        await self.controller.stop()
        count = runtime.connects
        await asyncio.sleep(.03)
        self.assertEqual(runtime.connects, count)

    async def test_validation_error_leaves_console_controllable(self):
        env_config.write_env_file({'MASTER_QQ': 'not-a-number'}, self.paths.env)
        with self.assertRaises(RelayControlError): await self.controller.start()
        self.assertEqual(self.controller.state, 'error')
        self.assertEqual(self.instances, [])
        self.assertTrue(self.controller.snapshot()['can_start'])
        env_config.write_env_file({'MASTER_QQ': '0'}, self.paths.env)
        await self.controller.start()
        await self.wait_state('running')

    async def test_missing_config_does_not_connect(self):
        self.paths.env.unlink()
        with self.assertRaisesRegex(RelayControlError, '保存初始配置'):
            await self.controller.start()
        self.assertEqual(self.instances, [])

    async def test_start_failure_cleans_partial_resources(self):
        def factory(*args):
            result = FakeRuntime(*args)
            result.fail_start = True
            self.instances.append(result)
            return result
        self.controller.factory = factory
        with self.assertRaises(RelayControlError): await self.controller.start()
        self.assertEqual(self.instances[0].closed, 1)
        self.assertIsNone(self.controller.runtime)

    async def test_cleanup_failure_does_not_claim_stopped_or_allow_start(self):
        await self.controller.start()
        self.instances[0].fail_close = True
        with self.assertRaises(RelayControlError): await self.controller.stop()
        self.assertEqual(self.controller.state, 'error')
        self.assertFalse(self.controller.snapshot()['can_start'])
        with self.assertRaises(RelayControlError): await self.controller.start()
        self.instances[0].fail_close = False
        await self.controller.stop()
        self.assertEqual(self.controller.state, 'stopped')

    async def test_cancellation_during_start_cleans_resources(self):
        def factory(*args):
            result = FakeRuntime(*args)
            result.start_gate = asyncio.Event()
            self.instances.append(result)
            return result
        self.controller.factory = factory
        task = asyncio.create_task(self.controller.start())
        await asyncio.sleep(.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(self.instances[0].closed, 1)
        self.assertIsNone(self.controller.runtime)

    async def test_group_validation_prevents_import_or_start(self):
        for raw in ['[]', '{"abc":"pipe"}', '{"123":"other"}']:
            env_config.write_env_file({'GROUP_MODE': raw}, self.paths.env)
            with self.assertRaises(RelayControlError): await self.controller.start()
        self.assertEqual(self.instances, [])


class PathTests(unittest.TestCase):
    def test_legacy_portable_and_frozen_paths(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            root = Path(temp).resolve()
            source = root / 'source'
            legacy = discover_paths(source_root=source, frozen=False)
            self.assertEqual(legacy.env, source / '.env')
            self.assertEqual(legacy.memory, source / 'memory')
            self.assertEqual(legacy.logs, source / 'memory' / 'logs')
            self.assertFalse(legacy.portable)
            portable = discover_paths(data_dir=root / '中文 data', source_root=source)
            self.assertEqual(portable.logs, root / '中文 data' / 'logs')
            self.assertEqual(portable.with_config({'MEM_DIR': './custom'}).memory, root / '中文 data' / 'custom')
            absolute = root / 'external'
            self.assertEqual(portable.with_config({'MEM_DIR': str(absolute)}).memory, absolute)
            frozen = discover_paths(frozen=True, executable=root / 'app' / 'QQBot.exe', resources=root / 'bundle')
            self.assertEqual(frozen.env, root / 'app' / 'QQBotData' / '.env')
            self.assertEqual(frozen.static, root / 'bundle' / 'static')
            self.assertFalse((root / 'app').exists())

    def test_paths_do_not_follow_working_directory(self):
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            paths = discover_paths(data_dir=one)
            before = Path.cwd()
            try:
                os.chdir(two)
                self.assertEqual(paths.with_config({'MEM_DIR': './memory'}).memory, Path(one).resolve() / 'memory')
            finally:
                os.chdir(before)

    def test_lock_rejects_second_owner_and_recovers_after_release(self):
        with tempfile.TemporaryDirectory() as temp:
            first, second = InstanceLock(temp), InstanceLock(temp)
            first.acquire()
            try:
                with self.assertRaises(RuntimeError): second.acquire()
            finally:
                first.close()
            second.acquire()
            second.close()
            self.assertTrue((Path(temp) / '.relay.lock').exists())

    def test_unwritable_location_never_falls_back(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / 'blocked'
            file.write_text('keep')
            paths = discover_paths(data_dir=file)
            with self.assertRaises(OSError): paths.check_writable()
            self.assertEqual(file.read_text(), 'keep')


class ControlAPITests(unittest.TestCase):
    def test_control_api_keeps_ui_alive_and_uses_selected_data_root(self):
        from fastapi.testclient import TestClient
        import webui
        from runtime_logs import RuntimeLogs
        with tempfile.TemporaryDirectory() as temp, patch.object(env_config, 'INHERITED_ENV', {}):
            paths = discover_paths(data_dir=Path(temp) / 'QQBotData')
            paths.data.mkdir()
            config = {**env_config.DEFAULTS, 'MEM_DIR': './custom-memory'}
            env_config.write_env_file(config, paths.env)
            selected_paths = paths.with_config(config)
            logs = RuntimeLogs(paths.logs)
            try:
                app = webui.create_app(paths=paths, log_capture=logs, managed=True, runtime_factory=FakeRuntime)
                with TestClient(app, base_url='http://127.0.0.1:8800', client=('127.0.0.1',1234)) as client:
                    status = client.get('/api/relay').json()
                    self.assertEqual(status['state'], 'stopped')
                    self.assertEqual(status['memory_dir'], str(selected_paths.memory))
                    self.assertEqual(client.post('/api/relay/start', headers={'Origin':'https://evil.test'}).status_code,403)
                    self.assertEqual(client.post('/api/relay/start').status_code,200)
                    self.assertEqual(client.post('/api/relay/start').status_code,200)
                    self.assertEqual(client.post('/api/relay/stop').json()['state'],'stopped')
                    self.assertEqual(client.get('/').status_code,200)
                    self.assertEqual(client.get('/api/settings').status_code,200)
                    self.assertEqual(client.get('/api/logs').status_code,200)
                    self.assertEqual(client.put('/api/knowledge/test.md',json={'content':'portable'}).status_code,200)
                    self.assertEqual((selected_paths.knowledge/'test.md').read_text(),'portable')
                    self.assertEqual(client.put('/api/knowledge/..%5Cescape.md',json={'content':'bad'}).status_code,400)
                    self.assertEqual(client.post('/api/relay/restart').status_code,200)
                self.assertEqual(app.state.controller.state,'stopped')
                lock = InstanceLock(paths.data)
                lock.acquire()
                lock.close()
            finally:
                logs.close()


if __name__ == '__main__': unittest.main()
