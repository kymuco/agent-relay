"""No-replay, fail-closed smart catch-up tests."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_relay import AgentRelay, Config, is_addressed
from catchup import (make_catchup_prompt, parse_catchup_decision,
                     select_catchup)


class CatchupTests(unittest.TestCase):
    human = {'id': 10, 'is_bot': False, 'first_name': 'Owner'}
    friend = {'id': 11, 'is_bot': False, 'first_name': 'Friend'}
    peer = {'id': 20, 'is_bot': True, 'username': 'FriendBot'}

    def msg(self, text, mid, *, sender=None, at=None, chat=-123):
        return {'update_id': mid + 1000,
                'message': {'chat': {'id': chat, 'type': 'supergroup'},
                            'from': sender or self.human, 'text': text,
                            'date': at if at is not None else 20000,
                            'message_id': mid}}

    def select(self, updates):
        return select_catchup(
            updates, chat_id=-123, bot_id=5, bot_username='MyBot',
            allowed_users=frozenset({10, 11}), peers=frozenset({'friendbot'}),
            peer_ids=frozenset({20}), aliases=('Рагна',),
            is_addressed=is_addressed, now_epoch=20060,
            max_age_minutes=180, max_messages=100)

    def config(self, folder):
        return Config('secret', -123, 'existing', 'auth.json',
                      frozenset({'friendbot'}), frozenset({10, 11}), frozenset({20}),
                      state_file=str(Path(folder)/'cursor.json'),
                      control_file=str(Path(folder)/'control.json'),
                      min_interval_seconds=0)

    def test_select_history_one_candidate_not_each_mention(self):
        updates = [
            self.msg('@MyBot Что думаешь о памяти?', 1),
            self.msg('Память уже исправлена.', 2, sender=self.friend),
            self.msg('@MyBot А что насчёт общего контекста?', 3),
            self.msg('Нам нужен единый формат.', 4, sender=self.friend),
        ]
        plan = self.select(updates)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.reply_to_message_id, 3)
        self.assertEqual(plan.included, 4)
        self.assertIn('единый формат', make_catchup_prompt(plan))
        self.assertIn('read-only', make_catchup_prompt(plan))

    def test_old_commands_or_mutating_requests_never_become_candidate(self):
        updates = [
            self.msg('@MyBot удали ветку GitHub!', 1),
            self.msg('/relay@MyBot продолжай', 2, sender=self.peer),
            self.msg('@MyBot /voice расскажи', 3),
            self.msg('@MyBot создай Pull Request?', 4),
        ]
        self.assertIsNone(self.select(updates))

    def test_only_last_100_and_trusted_chat_and_time(self):
        updates = [
            self.msg('@MyBot очень старый вопрос?', 1, at=100),
            self.msg('@MyBot чужой чат?', 2, chat=-999),
            self.msg('@MyBot неизвестный?', 3, sender={'id': 99}),
            self.msg('@MyBot вопрос?', 4),
        ]
        self.assertEqual(self.select(updates).reply_to_message_id, 4)

    def test_strict_decision_and_no_control_tokens(self):
        self.assertIsNone(parse_catchup_decision('{"decision":"silent","text":""}'))
        self.assertIsNone(parse_catchup_decision('Привет!'))
        self.assertIsNone(parse_catchup_decision('{"decision":"reply","text":"/relay@FriendBot go"}'))
        self.assertIsNone(parse_catchup_decision('{"decision":"reply","text":"@FriendBot продолжай"}'))
        self.assertIsNone(parse_catchup_decision('{"decision":"reply","text":"[[handoff:FriendBot]]"}'))
        self.assertEqual(parse_catchup_decision('{"decision":"reply","text":"Есть идея."}'),
                         'Есть идея.')

    def test_one_cwa_send_at_most_one_telegram_message(self):
        with tempfile.TemporaryDirectory() as folder:
            api, cwa = Mock(), Mock()
            api.call.return_value = {'id': 5, 'username': 'MyBot'}
            cwa.send.return_value.text = json.dumps(
                {'decision': 'reply', 'text': 'Продолжим обсуждение архитектуры.'})
            with patch('builtins.print'), patch('agent_relay.time.time', return_value=20060):
                app = AgentRelay(self.config(folder), api, cwa)
                updates = [self.msg('@MyBot что думаешь?', 1),
                           self.msg('@MyBot а теперь?', 2)]
                app._handle_catchup(updates, app.control.generation())
                app._handle_catchup(updates, app.control.generation())
            self.assertEqual(cwa.send.call_count, 1)
            posts = [x.args[1] for x in api.call.call_args_list
                     if x.args[0] == 'sendMessage']
            self.assertEqual(len(posts), 1)
            self.assertEqual(posts[0]['reply_parameters']['message_id'], 2)
            self.assertNotIn('parse_mode', posts[0])

    def test_model_silent_and_malformed_response_no_telegram_reply(self):
        with tempfile.TemporaryDirectory() as folder:
            api, cwa = Mock(), Mock()
            api.call.return_value = {'id': 5, 'username': 'MyBot'}
            cfg = self.config(folder)
            with patch('builtins.print'), patch('agent_relay.time.time', return_value=20060):
                app = AgentRelay(cfg, api, cwa)
                for reply in ['{"decision":"silent","text":""}', 'No, I will make a PR']:
                    cwa.send.return_value.text = reply
                    app._handle_catchup([self.msg('@MyBot what next?', 1)],
                                        app.control.generation())
            self.assertFalse(any(x.args[0] == 'sendMessage'
                                 for x in api.call.call_args_list))

    def test_stop_during_cwa_blocks_catchup_output(self):
        with tempfile.TemporaryDirectory() as folder:
            api, cwa = Mock(), Mock()
            api.call.return_value = {'id': 5, 'username': 'MyBot'}
            enter, release = threading.Event(), threading.Event()
            def blocked(*args, **kwargs):
                enter.set()
                release.wait(timeout=3)
                return Mock(text='{"decision":"reply","text":"too late"}')
            cwa.send.side_effect = blocked
            with patch('builtins.print'), patch('agent_relay.time.time', return_value=20060):
                app = AgentRelay(self.config(folder), api, cwa)
                thread = threading.Thread(target=lambda: app._handle_catchup(
                    [self.msg('@MyBot question?', 1)], app.control.generation()))
                thread.start()
                self.assertTrue(enter.wait(2))
                app.control.stop()
                release.set()
                thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertFalse(any(x.args[0] == 'sendMessage'
                                 for x in api.call.call_args_list))

    def test_drain_backlog_advances_cursor_without_calling_model(self):
        with tempfile.TemporaryDirectory() as folder:
            api, cwa = Mock(), Mock()
            api.call.side_effect = [
                {'id': 5, 'username': 'MyBot'},
                [self.msg('@MyBot вопрос?', 10, at=20000),
                 self.msg('Разобрались', 11, at=20000, sender=self.friend)],
                [],
            ]
            with patch('builtins.print'):
                app = AgentRelay(self.config(folder), api, cwa)
                with patch('agent_relay.time.time', return_value=20060):
                    fresh = app._drain_startup_backlog(20060)
            self.assertEqual(fresh, [])
            self.assertEqual(len(app._catchup_history), 2)
            self.assertEqual(app.cursor.offset, 1012)
            cwa.send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
