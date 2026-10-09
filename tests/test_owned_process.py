import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from owned_process import OwnedProcess


def process_alive(pid):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle: return False
        try: return kernel.WaitForSingleObject(handle, 0) == 258
        finally: kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f'/proc/{pid}/stat')
    return not stat.exists() or stat.read_text().split()[2] != 'Z'


class OwnedProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_and_missing_program(self):
        command = OwnedProcess()
        code, out, err = await command.run([sys.executable, '-c', "import sys; print(sys.stdin.read())"], input_data=b'owned-test', timeout=5)
        self.assertEqual(code,0)
        self.assertIn(b'owned-test',out)
        self.assertTrue(command._closed)
        missing = OwnedProcess()
        with self.assertRaises(FileNotFoundError):
            await missing.run(['nonexistent-relay-test-command-xyz'])
        self.assertTrue(missing._closed)

    async def test_timeout_kills_owned_root(self):
        command = OwnedProcess()
        with self.assertRaises(asyncio.TimeoutError):
            await command.run([sys.executable,'-c','import time; time.sleep(60)'], timeout=.1)
        self.assertFalse(process_alive(command.process.pid))

    async def test_cancel_kills_own_tree_not_unrelated_process(self):
        with tempfile.TemporaryDirectory() as temp:
            child_file = Path(temp) / 'child.pid'
            script = "import subprocess,sys,time; from pathlib import Path; child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);Path(sys.argv[1]).write_text(str(child.pid));time.sleep(60)"
            # This independent dummy is created by the test, never a user process.
            unrelated = subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
            command = OwnedProcess()
            task = asyncio.create_task(command.run([sys.executable,'-c',script,str(child_file)]))
            try:
                for _ in range(200):
                    if child_file.exists(): break
                    if task.done(): await task
                    await asyncio.sleep(.01)
                self.assertTrue(child_file.exists())
                child_pid = int(child_file.read_text())
                task.cancel()
                with self.assertRaises(asyncio.CancelledError): await task
                for _ in range(100):
                    if not process_alive(child_pid): break
                    await asyncio.sleep(.01)
                self.assertFalse(process_alive(command.process.pid))
                self.assertFalse(process_alive(child_pid))
                self.assertIsNone(unrelated.poll())
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task,return_exceptions=True)
                await command.close()
                unrelated.kill()
                unrelated.wait(timeout=5)


if __name__ == '__main__': unittest.main()
