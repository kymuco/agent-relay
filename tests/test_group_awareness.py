import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_relay import AgentRelay, Config, is_addressed, usernames
from group_awareness import GroupAwareness, addressed_by_alias, explicit_handoff, trusted_sender


class GroupAwarenessTests(unittest.TestCase):
    def setUp(self):
        self.human = {'id': 10, 'first_name': 'Alice', 'is_bot': False}
        self.peer = {'id': 20, 'username': 'FriendBot', 'is_bot': True}
        self.peers = usernames('FriendBot')
        self.allowed = frozenset({10})
        self.peer_ids = frozenset({20})

    def msg(self, sender, text, mid=1, group=-123):
        return {'chat': {'id': group, 'type': 'supergroup'},
                'message_id': mid, 'from': sender, 'text': text}

    def test_allowlist_and_sender_chat(self):
        self.assertTrue(trusted_sender(self.msg(self.human, 'hello'), self.allowed, self.peers, self.peer_ids, 5))
        self.assertTrue(trusted_sender(self.msg(self.peer, 'hello'), self.allowed, self.peers, self.peer_ids, 5))
        self.assertFalse(trusted_sender(self.msg({**self.peer, 'id': 99}, 'hi'), self.allowed, self.peers, self.peer_ids, 5))
        self.assertFalse(trusted_sender(self.msg({**self.peer, 'username': 'fakebot'}, 'hi'), self.allowed, self.peers, self.peer_ids, 5))
        self.assertFalse(trusted_sender(self.msg({**self.peer, 'id': 5}, 'hi'), self.allowed, self.peers, self.peer_ids, 5))
        self.assertFalse(trusted_sender(self.msg({'id': 11}, 'hi'), self.allowed, self.peers, self.peer_ids, 5))
        self.assertFalse(trusted_sender({**self.msg(self.human, 'hi'), 'sender_chat': {'id': -123}}, self.allowed, self.peers, self.peer_ids, 5))

    def test_bounded_context_and_exclusion(self):
        group = GroupAwareness(2)
        group.remember(self.msg(self.human, 'first', 1))
        group.remember(self.msg(self.human, 'second', 2))
        group.remember(self.msg(self.human, 'third', 3))
        self.assertNotIn('first', group.recent_text())
        self.assertIn('second', group.recent_text())
        self.assertNotIn('third', group.recent_text(exclude_message_id=3))
        group.remember_own_reply('MyBot', 'reply')
        self.assertIn('bot MyBot: reply', group.recent_text())
        group.remember(self.msg(self.human, 'x' * 800, 4))
        self.assertNotIn('x' * 800, group.recent_text())
        self.assertEqual(GroupAwareness(0).recent_text(), '')

    def test_alias_exact_words_only(self):
        self.assertTrue(addressed_by_alias('Рагна, что думаешь?', ('Рагна',)))
        self.assertTrue(addressed_by_alias('Hi Ragna!', ('Ragna',)))
        self.assertFalse(addressed_by_alias('NotRagna or @Ragna', ('Ragna',)))
        self.assertFalse(addressed_by_alias('он про рог', ('Рагна',)))
        self.assertFalse(addressed_by_alias('Hi Al', ('Al',)))
        msg = self.msg(self.human, 'Рагна, помоги')
        self.assertTrue(is_addressed(msg, 'MyBot', 5, self.peers, self.allowed, self.peer_ids, ('Рагна',)))
        self.assertFalse(is_addressed(self.msg(self.peer, 'Рагна, помоги'), 'MyBot', 5,
                                       self.peers, self.allowed, self.peer_ids, ('Рагна',)))

    def test_explicit_handoff_and_one_hop(self):
        out = explicit_handoff('Привет! [[handoff:FriendBot]]', self.peers, from_bot=False)
        self.assertEqual(out, '/relay@FriendBot Привет!')
        self.assertEqual(explicit_handoff('@FriendBot hi [[handoff:friendbot]]', self.peers, from_bot=False), '/relay@friendbot hi')
        self.assertEqual(explicit_handoff('@FriendBot hi', self.peers, from_bot=False), '/relay@FriendBot hi')
        self.assertEqual(explicit_handoff('Quoted @FriendBot hi', self.peers, from_bot=False), 'Quoted @FriendBot hi')
        self.assertEqual(explicit_handoff('Hi [[handoff:BadBot]]', self.peers, from_bot=False), 'Hi')
        self.assertEqual(explicit_handoff('Hi [[handoff:FriendBot]]', self.peers, from_bot=True), 'Hi')
        self.assertEqual(explicit_handoff('Hello', self.peers, from_bot=False), 'Hello')

    def test_telegram_command_mention_from_peer(self):
        self.assertTrue(is_addressed(self.msg(self.peer, '/relay@MyBot Привет'), 'MyBot', 5,
                                     self.peers, self.allowed, self.peer_ids))
        self.assertTrue(is_addressed(self.msg(self.peer, '/relay@mybot Привет'), 'MyBot', 5,
                                     self.peers, self.allowed, self.peer_ids))
        self.assertFalse(is_addressed(self.msg({**self.peer, 'id': 99}, '/relay@MyBot Привет'), 'MyBot', 5,
                                      self.peers, self.allowed, self.peer_ids))
        self.assertFalse(is_addressed(self.msg(self.peer, '/relay@OtherBot Привет'), 'MyBot', 5,
                                      self.peers, self.allowed, self.peer_ids))

    def test_group_context_does_not_trigger_ai_and_handoff_routes(self):
        with tempfile.TemporaryDirectory() as folder:
            api, cwa = Mock(), Mock()
            api.call.return_value = {'id': 5, 'username': 'MyBot'}
            cwa.send.return_value.text = 'Передаю слово. [[handoff:FriendBot]]'
            cfg = Config('secret', -123, 'existing-conversation', 'auth.json', self.peers,
                         self.allowed, self.peer_ids, state_file=str(Path(folder) / 'cursor.json'),
                         min_interval_seconds=0, bot_aliases=('Рагна',))
            with patch('builtins.print'):
                app = AgentRelay(cfg, api, cwa)
                app.process_update({'update_id': 11, 'message': self.msg(self.human, 'Обсудим планы', 1)})
                cwa.send.assert_not_called()
                app.process_update({'update_id': 12, 'message': self.msg(self.peer, 'Мнение второго бота', 2)})
                cwa.send.assert_not_called()
                app.process_update({'update_id': 13, 'message': self.msg({'id': 99}, 'чужая реплика', 3)})
                app.process_update({'update_id': 14, 'message': self.msg(self.human, 'Рагна, продолжай', 4)})
            cwa.send.assert_called_once()
            prompt = cwa.send.call_args.args[0]
            self.assertIn('Обсудим планы', prompt)
            self.assertIn('Мнение второго бота', prompt)
            self.assertNotIn('чужая реплика', prompt)
            self.assertEqual(prompt.count('Рагна, продолжай'), 1)
            posted = [x.args[1]['text'] for x in api.call.call_args_list if x.args[0] == 'sendMessage']
            self.assertEqual(posted, ['/relay@FriendBot Передаю слово.'])

    def test_bot_reply_must_not_re_handoff(self):
        with tempfile.TemporaryDirectory() as folder:
            api, cwa = Mock(), Mock()
            api.call.return_value = {'id': 5, 'username': 'MyBot'}
            cwa.send.return_value.text = '@FriendBot продолжим [[handoff:FriendBot]]'
            cfg = Config('secret', -123, 'existing-conversation', 'auth.json', self.peers,
                         self.allowed, self.peer_ids, state_file=str(Path(folder) / 'cursor.json'),
                         min_interval_seconds=0)
            with patch('builtins.print'):
                app = AgentRelay(cfg, api, cwa)
                app.process_update({'update_id': 11, 'message': self.msg(self.peer, 'Hello @MyBot', 1)})
            cwa.send.assert_called_once()
            posted = [x.args[1]['text'] for x in api.call.call_args_list if x.args[0] == 'sendMessage']
            self.assertEqual(posted, ['@\u200bFriendBot продолжим'])
            self.assertNotIn('reply_parameters', [x.args[1] for x in api.call.call_args_list if x.args[0] == 'sendMessage'][0])

    def test_opt_in_peer_reply_continues_without_explicit_mention(self):
        with tempfile.TemporaryDirectory() as folder:
            api, cwa = Mock(), Mock()
            api.call.return_value = {'id': 5, 'username': 'MyBot'}
            cwa.send.return_value.text = 'Начинаю [[handoff:FriendBot]]'
            cfg = Config('secret', -123, 'existing-conversation', 'auth.json', self.peers,
                         self.allowed, self.peer_ids, state_file=str(Path(folder) / 'cursor.json'),
                         min_interval_seconds=0, peer_reply_mode=True, peer_reply_window_seconds=900)
            with patch('builtins.print'):
                app = AgentRelay(cfg, api, cwa)
                app.process_update({'update_id': 1, 'message': self.msg(self.human, '@MyBot обсуди идею', 1)})
                self.assertEqual(app.peer_reply_id, 20)
                cwa.send.return_value.text = 'Продолжаю, коллега.'
                inbound = self.msg(self.peer, 'Я согласен. А что дальше?', 2)
                inbound['reply_to_message'] = {'from': {'id': 5, 'is_bot': True}}
                app.process_update({'update_id': 2, 'message': inbound})
            self.assertEqual(cwa.send.call_count, 2)
            outgoing = [x.args[1] for x in api.call.call_args_list if x.args[0] == 'sendMessage']
            self.assertEqual(outgoing[0]['text'], '/relay@FriendBot Начинаю')
            self.assertEqual(outgoing[-1]['text'], 'Продолжаю, коллега.')
            self.assertEqual(outgoing[-1]['reply_parameters']['message_id'], 2)

    def test_peer_plain_mention_creates_native_reply_then_continues(self):
        """Reply metadata, not literal /reply text, drives an ongoing dialogue."""
        with tempfile.TemporaryDirectory() as folder:
            api, cwa = Mock(), Mock()
            api.call.return_value = {'id': 5, 'username': 'MyBot'}
            cwa.send.return_value.text = 'Подтверждаю 2/6. [[handoff:FriendBot]]'
            cfg = Config('secret', -123, 'existing-conversation', 'auth.json',
                         self.peers, self.allowed, self.peer_ids,
                         state_file=str(Path(folder) / 'cursor.json'),
                         min_interval_seconds=0, peer_reply_mode=True,
                         peer_reply_window_seconds=900)
            with patch('builtins.print'):
                app = AgentRelay(cfg, api, cwa)
                app.process_update({'update_id': 1, 'message':
                                    self.msg(self.peer, '@MyBot Продолжай счёт', 42)})
                self.assertEqual(app.peer_reply_id, 20)
                cwa.send.return_value.text = '4/6, продолжаем.'
                reply = self.msg(self.peer, '3/6, твой ход', 43)
                reply['reply_to_message'] = {'from': {'id': 5, 'is_bot': True}}
                app.process_update({'update_id': 2, 'message': reply})
            self.assertEqual(cwa.send.call_count, 2)
            out = [c.args[1] for c in api.call.call_args_list if c.args[0] == 'sendMessage']
            self.assertEqual(len(out), 2)
            self.assertIn('Подтверждаю 2/6', out[0]['text'])
            self.assertNotIn('handoff', out[0]['text'])
            self.assertNotIn('/reply', out[0]['text'])
            self.assertEqual(out[0]['reply_parameters']['message_id'], 42)
            self.assertEqual(out[1]['reply_parameters']['message_id'], 43)

    def test_peer_mention_without_reply_mode_stays_one_off(self):
        with tempfile.TemporaryDirectory() as folder:
            api, cwa = Mock(), Mock()
            api.call.return_value = {'id': 5, 'username': 'MyBot'}
            cwa.send.return_value.text = 'Ответ [[handoff:FriendBot]]'
            cfg = Config('secret', -123, 'existing-conversation', 'auth.json',
                         self.peers, self.allowed, self.peer_ids,
                         state_file=str(Path(folder) / 'cursor.json'),
                         min_interval_seconds=0, peer_reply_mode=False)
            with patch('builtins.print'):
                app = AgentRelay(cfg, api, cwa)
                app.process_update({'update_id': 1, 'message':
                                    self.msg(self.peer, '@MyBot Привет', 42)})
            out = [c.args[1] for c in api.call.call_args_list if c.args[0] == 'sendMessage']
            self.assertEqual(len(out), 1)
            self.assertNotIn('reply_parameters', out[0])

    def test_incoming_peer_command_starts_reply_session_and_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            api, cwa = Mock(), Mock()
            api.call.return_value = {'id': 5, 'username': 'MyBot'}
            cwa.send.return_value.text = 'Ответил, обсуждаем дальше.'
            cfg = Config('secret', -123, 'existing-conversation', 'auth.json', self.peers,
                         self.allowed, self.peer_ids, state_file=str(Path(folder) / 'cursor.json'),
                         min_interval_seconds=0, peer_reply_mode=True, peer_reply_window_seconds=900)
            with patch('builtins.print'):
                app = AgentRelay(cfg, api, cwa)
                app.process_update({'update_id': 1, 'message': self.msg(self.peer, '/relay@MyBot Привет', 1)})
                self.assertEqual(app.peer_reply_id, 20)
                app.process_update({'update_id': 2, 'message': self.msg(self.human, '/stoprelay', 2)})
                self.assertIsNone(app.peer_reply_id)
                reply = self.msg(self.peer, 'Продолжим?', 3)
                reply['reply_to_message'] = {'from': {'id': 5, 'is_bot': True}}
                app.process_update({'update_id': 3, 'message': reply})
            cwa.send.assert_called_once()
            outgoing = [x.args[1] for x in api.call.call_args_list if x.args[0] == 'sendMessage']
            self.assertEqual(outgoing[0]['reply_parameters']['message_id'], 1)
            self.assertEqual(len(outgoing), 2)

    def test_peer_reply_expiration_and_non_reply_do_not_trigger(self):
        with tempfile.TemporaryDirectory() as folder:
            api, cwa = Mock(), Mock()
            api.call.return_value = {'id': 5, 'username': 'MyBot'}
            cwa.send.return_value.text = 'Ответ'
            cfg = Config('secret', -123, 'existing-conversation', 'auth.json', self.peers,
                         self.allowed, self.peer_ids, state_file=str(Path(folder) / 'cursor.json'),
                         min_interval_seconds=0, peer_reply_mode=True, peer_reply_window_seconds=60)
            with patch('builtins.print'):
                app = AgentRelay(cfg, api, cwa)
                app.process_update({'update_id': 1, 'message': self.msg(self.peer, '/relay@MyBot Привет', 1)})
                app.process_update({'update_id': 2, 'message': self.msg(self.peer, 'Просто сообщение', 2)})
                app.peer_reply_expires_at = 0
                reply = self.msg(self.peer, 'Ещё поговорим?', 3)
                reply['reply_to_message'] = {'from': {'id': 5, 'is_bot': True}}
                app.process_update({'update_id': 3, 'message': reply})
            cwa.send.assert_called_once()

    def test_model_can_end_peer_dialogue_without_triggering_reply(self):
        with tempfile.TemporaryDirectory() as folder:
            api, cwa = Mock(), Mock()
            api.call.return_value = {'id': 5, 'username': 'MyBot'}
            cwa.send.return_value.text = 'Спасибо за обсуждение. [[stop_dialogue]]'
            cfg = Config('secret', -123, 'existing-conversation', 'auth.json', self.peers,
                         self.allowed, self.peer_ids, state_file=str(Path(folder) / 'cursor.json'),
                         min_interval_seconds=0, peer_reply_mode=True, peer_reply_window_seconds=900)
            with patch('builtins.print'):
                app = AgentRelay(cfg, api, cwa)
                app.process_update({'update_id': 1, 'message': self.msg(self.peer, '/relay@MyBot Начнём', 1)})
            outgoing = [x.args[1] for x in api.call.call_args_list if x.args[0] == 'sendMessage']
            self.assertNotIn('reply_parameters', outgoing[-1])
            self.assertEqual(outgoing[-1]['text'], 'Спасибо за обсуждение.')
            self.assertIsNone(app.peer_reply_id)


if __name__ == '__main__':
    unittest.main()
