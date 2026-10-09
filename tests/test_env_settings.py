"""Configuration tests only use temporary files; never import/run the relay."""
import ast
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Isolate plugin import-time configuration from real memory/ data.
_IMPORT_DATA = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {'MEM_DIR': _IMPORT_DATA.name}):
    import webui
import env_config
from fastapi.testclient import TestClient


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / '.env'
        self.path.write_text('# user comment\nBOT_NAME=Old Bot\nDEEPSEEK_API_KEY=secret-old-key\n'
                             'NAPCAT_TOKEN=secret-old-token\nGROUP_MODE={"123":"pipe"}\n'
                             'GROUP_VISION=[123]\nCUSTOM_KEY=untouched\n', encoding='utf-8')
        self.path_patch = patch.object(webui, 'ENV_PATH', self.path)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.env_patch = patch.object(env_config, 'INHERITED_ENV', {})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.logs = webui.RuntimeLogs(Path(self.tmp.name) / 'logs', secrets=['secret-old-key', 'secret-old-token'])
        self.addCleanup(self.logs.close)
        app = webui.create_app(log_capture=self.logs)
        self.client = TestClient(app, base_url='http://127.0.0.1:8800', client=('127.0.0.1', 45000))
        self.addCleanup(self.client.close)

    def save(self, values=None, clear=None, **kwargs):
        return self.client.put('/api/settings', json={'values': values or {}, 'clear_secrets': clear or []}, **kwargs)

    def test_read_never_returns_secrets(self):
        response = self.client.get('/api/settings')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        data = response.json()
        self.assertTrue(data['secrets']['DEEPSEEK_API_KEY']['configured'])
        self.assertNotIn('DEEPSEEK_API_KEY', data['values'])
        self.assertNotIn('secret-old', response.text)
        self.assertNotIn('CUSTOM_KEY', response.text)

    def test_patch_preserves_comments_groups_and_blank_secrets(self):
        response = self.save({'BOT_NAME': '测试机器人', 'DEEPSEEK_API_KEY': '', 'NAPCAT_TOKEN': ''})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['updated'], ['BOT_NAME'])
        self.assertTrue(response.json()['restart_required'])
        env = env_config.read_env_file(self.path)
        self.assertEqual(env['DEEPSEEK_API_KEY'], 'secret-old-key')
        self.assertEqual(env['NAPCAT_TOKEN'], 'secret-old-token')
        self.assertEqual(env['GROUP_MODE'], '{"123":"pipe"}')
        self.assertEqual(env['CUSTOM_KEY'], 'untouched')
        self.assertIn('# user comment', self.path.read_text(encoding='utf-8'))

    def test_replace_then_explicit_clear(self):
        response = self.save({'DEEPSEEK_API_KEY': 'new-private-key'})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('new-private-key', response.text)
        self.assertEqual(env_config.read_env_file(self.path)['DEEPSEEK_API_KEY'], 'new-private-key')
        response = self.save(clear=['DEEPSEEK_API_KEY'])
        self.assertFalse(response.json()['secrets']['DEEPSEEK_API_KEY']['configured'])
        self.assertEqual(env_config.read_env_file(self.path)['DEEPSEEK_API_KEY'], '')

    def test_clear_replace_conflict_rejects_entire_patch(self):
        before = self.path.read_bytes()
        response = self.save({'BOT_NAME': 'not saved', 'NAPCAT_TOKEN': 'private-conflict'}, ['NAPCAT_TOKEN'])
        self.assertEqual(response.status_code, 422)
        self.assertNotIn('private-conflict', response.text)
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_fields_never_write(self):
        cases = [
            {'WEBUI_PORT': '0'}, {'MEME_SERVER_PORT': '65536'}, {'WEBUI_PORT': '1.5'},
            {'WEBUI_PORT': '8801'}, {'WEBUI_HOST': '0.0.0.0'}, {'WEBUI_HOST': 'example.com'},
            {'NAPCAT_WS_URL': 'https://localhost:3001'}, {'NAPCAT_WS_URL': 'ws://localhost:0'},
            {'DEEPSEEK_BASE_URL': 'file:///tmp/key'}, {'DEEPSEEK_BASE_URL': 'https://u:secret@host/'},
            {'NAPCAT_WS_URL': 'ws://host/?access_token=private-query'}, {'MASTER_QQ': 'NaN'},
            {'BOT_NAME': ''}, {'WEBUI_ENABLED': 'maybe'}, {'DEEPSEEK_MODEL': 42},
            {'DEEPSEEK_API_KEY': {'private-key': 'do-not-echo'}},
        ]
        for values in cases:
            with self.subTest(keys=list(values)):
                before = self.path.read_bytes()
                response = self.save(values)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertNotIn('private-query', response.text)
                self.assertNotIn('do-not-echo', response.text)

    def test_line_injection_rejected(self):
        for separator in ['\n', '\r', '\v', '\f', '\x00', '\x1e', '\x85', '\u2028', '\u2029']:
            with self.subTest(separator=repr(separator)):
                before = self.path.read_bytes()
                response = self.save({'DEEPSEEK_API_KEY': f'private{separator}MASTER_QQ=456'})
                self.assertEqual(response.status_code, 422)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertNotIn('private', response.text)

    def test_unknown_fields_and_bad_envelopes(self):
        for payload in [[], {'values': {'GROUP_MODE': '{}'}}, {'values': {'secret-unknown-name': 'x'}},
                        {'values': []}, {'clear_secrets': [23]}, {'clear_secrets': ['BOT_NAME']},
                        {'clear_secrets': {}}, {'unexpected': True}]:
            with self.subTest(payload_type=type(payload).__name__):
                before = self.path.read_bytes()
                response = self.client.put('/api/settings', json=payload)
                self.assertEqual(response.status_code, 422)
                self.assertNotIn('secret-unknown-name', response.text)
                self.assertEqual(self.path.read_bytes(), before)

    def test_same_origin_local_only(self):
        for headers in [{'Origin': 'https://evil.example'}, {'Origin': 'null'},
                        {'Sec-Fetch-Site': 'cross-site'}, {'Host': 'evil.example:8800'}]:
            with self.subTest(headers=headers):
                self.assertEqual(self.save({'BOT_NAME': 'not saved'}, headers=headers).status_code, 403)
        self.assertEqual(self.save({'BOT_NAME': 'safe'}, headers={'Origin': 'http://127.0.0.1:8800'}).status_code, 200)
        self.assertEqual(self.client.put('/api/settings', content='{}', headers={'Content-Type': 'text/plain'}).status_code, 415)
        app = webui.create_app(log_capture=self.logs)
        with TestClient(app, base_url='http://127.0.0.1:8800', client=('192.168.1.12', 44000)) as remote:
            self.assertEqual(remote.get('/api/settings').status_code, 403)

    def test_invalid_json_and_size(self):
        self.assertEqual(self.client.put('/api/settings', content='{', headers={'Content-Type': 'application/json'}).status_code, 400)
        self.assertEqual(self.save({'DEEPSEEK_API_KEY': 'x' * 70000}).status_code, 413)

    def test_atomic_failure_preserves_original(self):
        before = self.path.read_bytes()
        with patch.object(env_config.os, 'replace', side_effect=OSError('private filesystem info')):
            response = self.save({'BOT_NAME': 'not saved'})
        self.assertEqual(response.status_code, 500)
        self.assertNotIn('private filesystem info', response.text)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(list(self.path.parent.glob('.env-*.tmp')), [])

    def test_create_from_missing_file(self):
        self.path.unlink()
        data = self.client.get('/api/settings').json()
        self.assertFalse(data['env_exists'])
        self.assertEqual(data['values']['DEEPSEEK_MODEL'], 'deepseek-v4-flash')
        response = self.save({**data['values'], 'BOT_NAME': '新机器人'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.path.exists())
        self.assertEqual(env_config.read_env_file(self.path)['BOT_NAME'], '新机器人')

    def test_noop_and_os_override(self):
        self.assertFalse(self.save({'BOT_NAME': 'Old Bot'}).json()['restart_required'])
        with patch.object(env_config, 'INHERITED_ENV', {'DEEPSEEK_API_KEY': 'os-private', 'WEBUI_PORT': '9000'}):
            data = self.client.get('/api/settings')
            self.assertNotIn('os-private', data.text)
            self.assertEqual(data.json()['values']['WEBUI_PORT'], '9000')
            self.assertEqual(data.json()['environment_overrides'], ['DEEPSEEK_API_KEY', 'WEBUI_PORT'])
            self.assertEqual(self.save({'WEBUI_PORT': '9001'}).json()['values']['WEBUI_PORT'], '9001')

    def test_group_api_remains_compatible(self):
        response = self.client.put('/api/env-config', json={'group_mode': '{"456":"direct"}', 'group_vision': '[456]', 'fallback_mode': 'pipe'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/api/env-config').json()['group_mode'], {'456': 'direct'})
        self.assertEqual(env_config.read_env_file(self.path)['DEEPSEEK_API_KEY'], 'secret-old-key')

    def test_logs_api_cursor_privacy_and_read_only(self):
        self.logs.record('startup secret-old-key')
        self.logs.record('ERROR: test failure', stream='stderr')
        response = self.client.get('/api/logs?limit=1')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(len(response.json()['lines']), 1)
        self.assertEqual(response.json()['cursor'], 2)
        self.assertEqual(self.client.get('/api/logs?after=2').json()['lines'], [])
        self.assertTrue(self.client.get('/api/logs?session=old-session').json()['reset'])
        self.assertNotIn('secret-old-key', self.client.get('/api/logs').text)
        before = (self.logs.directory / 'relay.log').read_bytes()
        self.assertEqual(self.client.delete('/api/logs').status_code, 405)
        self.assertEqual((self.logs.directory / 'relay.log').read_bytes(), before)
        for query in ['limit=0', 'limit=1001', 'after=-1', 'after=bad']:
            self.assertEqual(self.client.get('/api/logs?' + query).status_code, 422)
        self.assertEqual(self.client.get('/api/logs', headers={'Origin': 'https://evil.example'}).status_code, 403)

    def test_changed_secrets_enter_redaction_set(self):
        self.assertEqual(self.save({'DEEPSEEK_API_KEY': 'new-log-secret'}).status_code, 200)
        self.logs.record('secret-old-key new-log-secret')
        response = self.client.get('/api/logs')
        self.assertNotIn('secret-old-key', response.text)
        self.assertNotIn('new-log-secret', response.text)

    def test_parser_literals_duplicates_and_model_loader(self):
        self.path.write_text('\ufeff# keep\nBOT_NAME=first\nBOT_NAME=last\nCUSTOM=a=b#c\n', encoding='utf-8')
        self.assertEqual(env_config.read_env_file(self.path)['BOT_NAME'], 'last')
        env_config.write_env_file({'BOT_NAME': '新名字'}, self.path)
        self.assertEqual(self.path.read_text(encoding='utf-8').count('BOT_NAME=新名字'), 2)
        self.assertEqual(env_config.read_env_file(self.path)['CUSTOM'], 'a=b#c')
        # Exercise the actual assignment without importing relay (which would touch real services/data).
        tree = ast.parse(Path('relay.py').read_text(encoding='utf-8'))
        assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == 'DEEPSEEK_MODEL' for target in node.targets))
        with patch.dict(os.environ, {'DEEPSEEK_MODEL': 'test-model-id'}):
            self.assertEqual(eval(compile(ast.Expression(assignment.value), 'relay.py', 'eval'), {'os': os}), 'test-model-id')


if __name__ == '__main__':
    unittest.main()
