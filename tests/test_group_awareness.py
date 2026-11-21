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


if __name__ == '__main__':
    unittest.main()
