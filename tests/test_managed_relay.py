"""Exercise real cleanup with a fake OneBot socket and a temporary loopback image server."""
import asyncio
import http.server
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app_paths import discover_paths
import env_config


class FakeSocket:
    def __init__(self):
        self.close_code = None
        self.sent = False
        self.closed = False
    async def recv(self): return json.dumps({'self_id': 42})
    def __aiter__(self): return self
    async def __anext__(self):
        if not self.sent:
            self.sent = True
            return json.dumps({'post_type':'test-only'})
        await asyncio.Event().wait()
    async def close(self):
        self.closed = True
        self.close_code = 1000


class ManagedRelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_closes_socket_message_tasks_http_and_image_listener(self):
        import relay
        import plugins
        with tempfile.TemporaryDirectory() as temp:
            paths = discover_paths(data_dir=temp)
            config = {**env_config.DEFAULTS, 'GROUP_MODE':'{}', 'GROUP_VISION':'[]', 'FALLBACK_MODE':'direct',
                      'MEM_DIR':'./memory', 'PIPE_ADD_DIR':'./memory', 'DEEPSEEK_API_KEY':'test-only'}
            paths = paths.with_config(config)
            paths.check_writable(include_runtime=True)
            old_plugin_paths = (plugins.MEM_DIR,plugins.PLUGINS_CONFIG)
            server_class = http.server.HTTPServer
            runtime = relay.ManagedRelay(config, paths, None)
            socket = FakeSocket()
            message_started = asyncio.Event()
            async def message_handler(data):
                message_started.set()
                await asyncio.Event().wait()
            run_task = None
            try:
                # Never bind the configured production port or connect a real OneBot.
                with patch('http.server.HTTPServer',side_effect=lambda address,handler:server_class(('127.0.0.1',0),handler)):
                    await runtime.start()
                self.assertTrue(runtime.meme_server[1].is_alive())
                self.assertEqual(relay.MEM_DIR, paths.memory)
                self.assertEqual(relay.INBOX_DIR, paths.inbox)
                runtime.bot.on_message = message_handler
                connected = AsyncMock()
                with patch.object(relay.websockets,'connect',new=AsyncMock(return_value=socket)):
                    run_task = asyncio.create_task(runtime.run(connected))
                    await asyncio.wait_for(message_started.wait(),2)
                    self.assertTrue(runtime.bot.connected)
                    run_task.cancel()
                    await asyncio.gather(run_task,return_exceptions=True)
                thread = runtime.meme_server[1]
                await runtime.close()
                self.assertTrue(socket.closed)
                self.assertFalse(runtime.bot.connected)
                self.assertEqual(runtime.bot._message_tasks,set())
                self.assertEqual(runtime.bot._api_futures,{})
                self.assertTrue(runtime.bot.http.is_closed)
                self.assertFalse(thread.is_alive())
                self.assertIsNone(runtime.meme_server)
                self.assertTrue((paths.memory/'conv').exists())
            finally:
                if run_task and not run_task.done():
                    run_task.cancel()
                    await asyncio.gather(run_task,return_exceptions=True)
                await runtime.close()
                plugins.MEM_DIR,plugins.PLUGINS_CONFIG = old_plugin_paths

    async def test_image_port_failure_can_cleanup_created_http_client(self):
        import relay
        with tempfile.TemporaryDirectory() as temp:
            paths=discover_paths(data_dir=temp)
            paths.check_writable(include_runtime=True)
            config={**env_config.DEFAULTS,'GROUP_MODE':'{}','GROUP_VISION':'[]','FALLBACK_MODE':'direct'}
            runtime=relay.ManagedRelay(config,paths,None)
            with patch('http.server.HTTPServer',side_effect=OSError('synthetic port conflict')):
                with self.assertRaises(OSError): await runtime.start()
            await runtime.close()
            self.assertTrue(runtime.bot.http.is_closed)
            self.assertIsNone(runtime.meme_server)


if __name__ == '__main__': unittest.main()
