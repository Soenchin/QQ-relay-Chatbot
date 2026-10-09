"""All log files and console targets in these tests are temporary/synthetic."""
import io
import logging
import subprocess
import sys
import tempfile
import threading
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime_logs import RuntimeLogs, MAX_PENDING


class RuntimeLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / 'logs'
        self.logs = RuntimeLogs(self.directory, secrets=['private-real-key'])
        self.addCleanup(self.logs.close)

    def all_text(self):
        self.logs.flush()
        return '\n'.join(line['msg'] for line in self.logs.snapshot(1000)['lines']) + ''.join(
            p.read_text(encoding='utf-8') for p in self.directory.glob('relay.log*'))

    def test_split_writes_redacted_before_console_memory_disk(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, 'stdout', out), patch.object(sys, 'stderr', err):
            self.logs.install()
            self.logs.install()  # Idempotent, never tee back into itself.
            sys.stdout.write('value private-')
            sys.stdout.flush()
            self.assertEqual(out.getvalue(), '')
            sys.stdout.write('real-key\n')
            print('中文日志')
            print('failed NAPCAT_TOKEN=unregistered-token', file=sys.stderr)
            self.logs.uninstall()
            self.assertIs(sys.stdout, out)
            self.assertIs(sys.stderr, err)
        for text in [out.getvalue(), err.getvalue(), self.all_text()]:
            self.assertNotIn('private-real-key', text)
            self.assertNotIn('unregistered-token', text)
        self.assertIn('中文日志', out.getvalue())
        rows = self.logs.snapshot(1000)['lines']
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1]['stream'], 'stderr')

    def test_common_credential_formats(self):
        samples = [
            ('Authorization: Bearer bearer-value-abc', 'bearer-value-abc'),
            ('{"api_key": "unknown-json-value"}', 'unknown-json-value'),
            ("token='a token with spaces'", 'a token with spaces'),
            ('https://example.test/?access_token=query-secret&next=1', 'query-secret'),
            ('wss://user:password-value@example.test/ws', 'password-value'),
            ('key sk-123456789abcdefghijklmnop', 'sk-123456789abcdefghijklmnop'),
            ('ANTHROPIC_AUTH_TOKEN=not-in-local-env', 'not-in-local-env'),
        ]
        for message, secret in samples:
            with self.subTest(secret_type=message.split(':')[0]):
                self.logs.record(message)
                self.assertNotIn(secret, self.all_text())
                self.assertIn('[REDACTED]', self.all_text())

    def test_new_and_old_secret_values_stay_redacted(self):
        self.logs.remember_secrets(['new/secret+value'])
        self.logs.record('private-real-key new/secret+value new%2Fsecret%2Bvalue')
        self.assertNotIn('new/secret+value', self.all_text())
        self.assertNotIn('new%2Fsecret%2Bvalue', self.all_text())
        self.assertNotIn('private-real-key', self.all_text())

    def test_stderr_traceback_and_python_logging(self):
        out = io.StringIO()
        with patch.object(sys, 'stdout', out), patch.object(sys, 'stderr', out):
            self.logs.install()
            logger = logging.Logger('synthetic')
            handler = logging.StreamHandler()
            logger.addHandler(handler)
            logger.error('logging private-real-key')
            try:
                raise ValueError('failed private-real-key')
            except ValueError:
                traceback.print_exc()
            self.logs.uninstall()
        self.assertIn('Traceback', self.all_text())
        self.assertIn('ValueError', self.all_text())
        self.assertNotIn('private-real-key', self.all_text())
        self.assertTrue(all(row['level'] == 'ERROR' for row in self.logs.snapshot(1000)['lines']))

    def test_threaded_print_chunks_do_not_mix(self):
        barrier = threading.Barrier(3)
        def worker(index):
            self.logs.write(f'thread-{index} private-', 'stdout')
            barrier.wait()
            self.logs.write('real-key\n', 'stdout')
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        rows = self.logs.snapshot(1000)['lines']
        self.assertEqual(len(rows), 3)
        self.assertEqual({row['msg'] for row in rows}, {f'thread-{i} [REDACTED]' for i in range(3)})

    def test_flush_partial_at_close_and_oversize_drop(self):
        self.logs.write('partial private-real-key', 'stdout')
        self.assertEqual(self.logs.snapshot()['lines'], [])
        self.logs.uninstall()
        self.assertEqual(self.logs.snapshot()['lines'][0]['msg'], 'partial [REDACTED]')
        self.logs.write('x' * (MAX_PENDING + 1) + 'private-real-key\n', 'stdout')
        self.assertIn('整行丢弃', self.logs.snapshot()['lines'][-1]['msg'])
        self.assertNotIn('private-real-key', self.all_text())

    def test_bounded_memory_and_incremental_cursor(self):
        for i in range(1100): self.logs.record(f'message {i}')
        first = self.logs.snapshot(limit=1000)
        self.assertEqual(len(first['lines']), 1000)
        self.assertEqual(first['lines'][0]['id'], 101)
        self.assertEqual(first['cursor'], 1100)
        self.assertEqual(self.logs.snapshot(after=first['cursor'])['lines'], [])
        self.logs.record('new')
        self.assertEqual(len(self.logs.snapshot(after=1100)['lines']), 1)
        gap = self.logs.snapshot(after=1)
        self.assertTrue(gap['gap'])
        self.assertEqual(gap['cursor'], 1101)
        self.assertTrue(self.logs.snapshot(after=9999)['reset'])
        self.assertTrue(self.logs.snapshot(session='old-process')['reset'])

    def test_incremental_pagination_does_not_skip(self):
        for i in range(5): self.logs.record(str(i))
        one = self.logs.snapshot(limit=2, after=0)
        two = self.logs.snapshot(limit=2, after=one['cursor'])
        three = self.logs.snapshot(limit=2, after=two['cursor'])
        self.assertEqual([r['id'] for page in [one,two,three] for r in page['lines']], [1,2,3,4,5])

    def test_rotation_and_restart_keep_disk_history(self):
        store = RuntimeLogs(Path(self.tmp.name) / 'rotation', max_bytes=512)
        self.addCleanup(store.close)
        for i in range(80): store.record(f'row-{i:03d} ' + 'text ' * 8)
        store.close()
        files = list(store.directory.glob('relay.log*'))
        self.assertEqual({p.name for p in files}, {'relay.log', 'relay.log.1'})
        self.assertTrue(all(p.stat().st_size < 650 for p in files))
        previous = (store.directory / 'relay.log').read_text(encoding='utf-8')
        reopened = RuntimeLogs(store.directory, max_bytes=512)
        self.addCleanup(reopened.close)
        self.assertNotEqual(store.session, reopened.session)
        self.assertEqual(previous, (store.directory / 'relay.log').read_text(encoding='utf-8'))
        self.assertEqual(reopened.snapshot()['lines'], [])

    def test_disk_unavailable_keeps_memory_without_recursion(self):
        blocker = Path(self.tmp.name) / 'not-a-directory'
        blocker.write_text('keep', encoding='utf-8')
        store = RuntimeLogs(blocker)
        self.addCleanup(store.close)
        store.record('still running')
        state = store.snapshot()
        self.assertFalse(state['storage']['available'])
        self.assertIn('still running', state['lines'][-1]['msg'])
        self.assertEqual(blocker.read_text(encoding='utf-8'), 'keep')

    def test_midrun_write_failure_does_not_leak_or_crash(self):
        with patch.object(self.logs._file, 'shouldRollover', side_effect=OSError('private-real-key')):
            self.logs.record('private-real-key')
        self.assertFalse(self.logs.snapshot()['storage']['available'])
        self.assertNotIn('private-real-key', self.all_text())

    def test_uncaught_crash_is_persisted_and_redacted(self):
        directory = Path(self.tmp.name) / 'crash'
        code = """import atexit, sys
from runtime_logs import RuntimeLogs
logs = RuntimeLogs(sys.argv[1], secrets=['mock-crash-key'])
logs.install()
atexit.register(logs.close)
print('startup before crash')
raise RuntimeError('mock-crash-key')
"""
        result = subprocess.run([sys.executable, '-c', code, str(directory)], capture_output=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        text = (directory / 'relay.log').read_text(encoding='utf-8')
        self.assertIn('startup before crash', text)
        self.assertIn('RuntimeError', text)
        self.assertNotIn('mock-crash-key', text)
        self.assertNotIn(b'mock-crash-key', result.stderr)

    def test_no_console_handles(self):
        with patch.object(sys, 'stdout', None), patch.object(sys, 'stderr', None):
            self.logs.install()
            self.logs.write('windowed exe\n', 'stderr')
            self.logs.uninstall()
        self.assertIn('windowed exe', self.all_text())


if __name__ == '__main__':
    unittest.main()
