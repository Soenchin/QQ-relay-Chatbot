"""Pipe context tests use fake bot state/process calls; no QQ, CLI or API traffic."""
import asyncio
from collections import deque
from datetime import datetime
import unittest
from unittest.mock import AsyncMock, Mock, patch

import relay


def make_bot():
    bot = relay.RelayBot.__new__(relay.RelayBot)
    bot.bot_qq = 42
    bot.eventbus = None
    bot.pipe_persona = 'synthetic persona'
    bot.pipe_gap_minutes = 5
    bot.GROUP_VISION = set()
    for name in ('_pipe_recent', '_pipe_recent_imgs', '_pipe_ts', '_pipe_counters',
                 '_pipe_thresholds', '_pipe_locks', '_sent_message_ids', '_sent_message_queues'):
        setattr(bot, name, {})
    bot._init_pipe_threshold = lambda: 4
    bot._sent_message_max = 100
    bot._seen_ids = set()
    bot._seen_queue = deque()
    bot._seen_max = 500
    bot.send_group = AsyncMock()
    return bot


class PipeWindowTests(unittest.TestCase):
    def setUp(self):
        self.bot = make_bot()

    def test_window_timestamps_and_images_evict_together(self):
        self.bot._delete_inbox_paths = Mock()
        for index in range(35):
            self.bot._append_pipe_recent(101, f'A-{index}', [f'inbox/101/{index}.png'])
        self.assertEqual(list(self.bot._pipe_recent[101]), [f'A-{index}' for index in range(5, 35)])
        self.assertEqual(len(self.bot._pipe_ts[101]), 30)
        self.assertEqual(len(self.bot._pipe_recent_imgs[101]), 30)
        self.assertEqual(self.bot._delete_inbox_paths.call_count, 5)
        self.bot._delete_inbox_paths.assert_any_call(['inbox/101/0.png'])

    def test_windows_and_locks_are_per_group(self):
        self.bot._append_pipe_recent(101, 'ONLY_A')
        self.bot._append_pipe_recent(202, 'ONLY_B')
        self.assertNotIn('ONLY_B', self.bot._fmt_window(101))
        self.assertNotIn('ONLY_A', self.bot._fmt_window(202))
        self.assertIsNot(self.bot._pipe_locks[101], self.bot._pipe_locks[202])

    def test_gap_separator_retains_old_messages_without_line_timestamps(self):
        self.bot._ensure_pipe_state(101)
        self.bot._pipe_recent[101].extend(['OLD_TOPIC', 'NEW_TOPIC', 'NEXT_LINE'])
        self.bot._pipe_ts[101].extend([datetime(2026, 10, 28, 9), datetime(2026, 10, 28, 12),
                                        datetime(2026, 10, 28, 12, 1)])
        rendered = self.bot._fmt_window(101)
        self.assertEqual(rendered, 'OLD_TOPIC\n--- 隔 3 小时 ---\nNEW_TOPIC\nNEXT_LINE')
        self.assertEqual(len(self.bot._pipe_recent[101]), 3)

    def test_cross_day_gap_and_live_interval(self):
        self.bot._ensure_pipe_state(101)
        self.bot._pipe_recent[101].extend(['BEFORE', 'AFTER'])
        self.bot._pipe_ts[101].extend([datetime(2026, 10, 27, 23, 58), datetime(2026, 10, 28, 0, 5)])
        self.assertIn('--- 隔 7 分钟，10-28 00:05 ---', self.bot._fmt_window(101))
        self.bot.pipe_gap_minutes = 10
        self.assertEqual(self.bot._fmt_window(101), 'BEFORE\nAFTER')

    def test_prompt_matches_production_rules_and_keeps_explicit_anchor(self):
        self.bot._append_pipe_recent(101, '[user] latest unrelated topic')
        prompt, images = self.bot._build_speak_prompt(101, 'at', '[user] ORIGINAL_QUESTION')
        self.assertIn('ORIGINAL_QUESTION', prompt)
        self.assertIn('通常 5~50 字，最多 100 字', prompt)
        self.assertIn('不要主动评论时间', prompt)
        self.assertEqual(images, [])
        active, _ = self.bot._build_speak_prompt(101, 'active')
        self.assertIn('最近三到五条', active)
        self.assertNotIn('ORIGINAL_QUESTION', active)

    def test_new_instance_has_no_previous_in_memory_context(self):
        self.bot._append_pipe_recent(101, 'OLD_PROCESS_CONTEXT')
        fresh = make_bot()
        fresh._append_pipe_recent(101, 'NEW_PROCESS_CONTEXT')
        self.assertNotIn('OLD_PROCESS_CONTEXT', fresh._fmt_window(101))


class PipeCallTests(unittest.IsolatedAsyncioTestCase):
    async def test_stateless_flags_and_selected_model_use_owned_process_seam(self):
        bot = make_bot()
        bot._pipe_process = AsyncMock(return_value=(0, b'reply', b''))
        with patch.object(relay, 'DEEPSEEK_MODEL', 'test-custom-model'):
            self.assertEqual(await bot._call_pipe(101, 'question'), 'reply')
        args, message, env = bot._pipe_process.call_args.args
        self.assertIn('--no-session-persistence', args)
        for flag in ('--resume', '--session-id', '--continue'):
            self.assertNotIn(flag, args)
        self.assertEqual(message, 'question')
        self.assertEqual(env['ANTHROPIC_MODEL'], 'test-custom-model')
        self.assertEqual(bot._pipe_process.await_count, 1)

    async def test_missing_session_error_never_retries(self):
        bot = make_bot()
        bot._pipe_process = AsyncMock(return_value=(1, b'', b'session does not exist'))
        await bot._call_pipe(101, 'question')
        self.assertEqual(bot._pipe_process.await_count, 1)

    async def test_timeout_and_cancellation_preserve_cleanup_contract(self):
        bot = make_bot()
        bot._pipe_process = AsyncMock(side_effect=asyncio.TimeoutError)
        self.assertIn('过会再来', await bot._call_pipe(101, 'question'))
        bot._pipe_process = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await bot._call_pipe(101, 'question')

    async def test_queued_at_uses_previous_answer_and_its_own_anchor(self):
        bot = make_bot()
        entered = asyncio.Event()
        release = asyncio.Event()
        prompts = []
        async def call(gid, prompt, **kwargs):
            prompts.append(prompt)
            number = len(prompts)
            if number == 1:
                entered.set()
                await release.wait()
            return f'BOT_ANSWER_{number}'
        bot._call_pipe = call
        with patch.object(relay, 'GROUP_MODE', {101: 'pipe'}), \
             patch.object(relay.plugins, 'run_plugins', new=AsyncMock(return_value=False)), \
             patch.object(relay.random, 'random', return_value=1):
            first = asyncio.create_task(bot.on_group_msg(101, 2, 'user', '[CQ:at,qq=42] FIRST'))
            await asyncio.wait_for(entered.wait(), 2)
            second = asyncio.create_task(bot.on_group_msg(101, 2, 'user', '[CQ:at,qq=42] SECOND'))
            await asyncio.sleep(0)
            third = asyncio.create_task(bot.on_group_msg(101, 2, 'user', '[CQ:at,qq=42] THIRD'))
            await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(asyncio.gather(first, second, third), 2)
        self.assertEqual(len(prompts), 3)
        self.assertIn('BOT_ANSWER_1', prompts[1])
        self.assertIn('BOT_ANSWER_2', prompts[2])
        self.assertIn('本次触发消息：\n[user] [CQ:at,qq=42] SECOND', prompts[1])
        self.assertIn('本次触发消息：\n[user] [CQ:at,qq=42] THIRD', prompts[2])
        self.assertEqual(sum('BOT_ANSWER_1' in line for line in bot._pipe_recent[101]), 1)

    async def test_active_speech_uses_same_prompt_builder(self):
        bot = make_bot()
        bot._ensure_pipe_state(101)
        bot._pipe_thresholds[101] = 1
        bot._call_pipe = AsyncMock(return_value='synthetic answer')
        with patch.object(relay, 'GROUP_MODE', {101: 'pipe'}), \
             patch.object(relay.plugins, 'run_plugins', new=AsyncMock(return_value=False)), \
             patch.object(relay.random, 'random', return_value=1):
            await bot.on_group_msg(101, 2, 'user', 'ACTIVE_TOPIC')
        prompt = bot._call_pipe.call_args.args[1]
        self.assertIn('最近三到五条', prompt)
        self.assertIn('ACTIVE_TOPIC', prompt)
        self.assertEqual(len(bot._pipe_ts[101]), 2)

    async def test_own_echo_records_reply_id_without_duplicate_context_or_self_trigger(self):
        bot = make_bot()
        bot._append_pipe_recent(101, '[bot] existing answer')
        bot.on_group_msg = AsyncMock()
        event = {'post_type': 'message', 'message_type': 'group', 'group_id': 101,
                 'user_id': 42, 'message_id': 99, 'raw_message': 'existing answer'}
        await bot.on_message(event)
        await bot.on_message(event)
        bot.on_group_msg.assert_not_awaited()
        self.assertEqual(list(bot._pipe_recent[101]), ['[bot] existing answer'])
        self.assertTrue(bot._is_reply_to_bot(101, '[CQ:reply,id=99] question'))
        self.assertFalse(bot._is_reply_to_bot(202, '[CQ:reply,id=99] question'))

    async def test_structured_reply_triggers_at_anchor(self):
        bot = make_bot()
        bot._remember_sent_message_id(101, 99)
        bot._call_pipe = AsyncMock(return_value='synthetic answer')
        event = {'message': [{'type': 'reply', 'data': {'id': '99'}},
                             {'type': 'text', 'data': {'text': 'FOLLOWUP'}}]}
        with patch.object(relay, 'GROUP_MODE', {101: 'pipe'}), \
             patch.object(relay.plugins, 'run_plugins', new=AsyncMock(return_value=False)), \
             patch.object(relay.random, 'random', return_value=1):
            await bot.on_group_msg(101, 2, 'user', 'FOLLOWUP', event=event)
        self.assertIn('本次触发消息', bot._call_pipe.call_args.args[1])
        self.assertEqual(bot._call_pipe.await_count, 1)


if __name__ == '__main__':
    unittest.main()
