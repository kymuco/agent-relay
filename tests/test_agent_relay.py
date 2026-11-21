import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_relay import AgentRelay, Config, Cursor, ensure_auth_file, is_addressed, make_prompt, neutralize_peer_mentions, numeric_ids, split_for_telegram, usernames
from relay_media import Incoming


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.human = {'id': 10, 'first_name': 'Human', 'is_bot': False}
        self.peer = {'id': 20, 'username': 'FriendBot', 'is_bot': True}
        self.message = {'chat': {'id': -123, 'type': 'supergroup'}, 'message_id': 7, 'from': self.human, 'text': 'hey @MyBot!'}
        self.peers = usernames('FriendBot')
        self.allowed = frozenset({10})
        self.peer_ids = frozenset({20})

    def test_mentions_and_replies(self):
        self.assertTrue(is_addressed(self.message, 'MyBot', 5, self.peers, self.allowed, self.peer_ids))
        self.message['text'] = 'normal message'
        self.assertFalse(is_addressed(self.message, 'MyBot', 5, self.peers, self.allowed, self.peer_ids))
        self.message['reply_to_message'] = {'from': {'id': 5}}
        self.assertTrue(is_addressed(self.message, 'MyBot', 5, self.peers, self.allowed, self.peer_ids))

    def test_bot_must_be_allowlisted_and_explicit(self):
        self.message['from'] = self.peer
        self.message['text'] = 'Hi @MyBot'
        self.assertTrue(is_addressed(self.message, 'MyBot', 5, self.peers, self.allowed, self.peer_ids))
        self.assertFalse(is_addressed(self.message, 'MyBot', 5, frozenset(), self.allowed, self.peer_ids))
        self.message['text'] = 'No mention'
        self.message['reply_to_message'] = {'from': {'id': 5}}
        self.assertFalse(is_addressed(self.message, 'MyBot', 5, self.peers, self.allowed, self.peer_ids))

    def test_anonymous_and_wrong_user_ignored(self):
        self.message['from'] = {'id': 30, 'is_bot': False}
        self.assertFalse(is_addressed(self.message, 'MyBot', 5, self.peers, self.allowed, self.peer_ids))
        self.message['from'] = self.human
        self.message['sender_chat'] = {'id': -123}
        self.assertFalse(is_addressed(self.message, 'MyBot', 5, self.peers, self.allowed, self.peer_ids))

    def test_bot_id_cannot_be_spoofed_by_username(self):
        self.message['from'] = {'id': 77, 'username': 'FriendBot', 'is_bot': True}
        self.assertFalse(is_addressed(self.message, 'MyBot', 5, self.peers, self.allowed, self.peer_ids))

    def test_bad_ids_rejected(self):
        for item in ('alice', '-1', '12,bad', '0'):
            with self.assertRaises(ValueError):
                numeric_ids(item, 'ALLOWED_USER_IDS')
        self.assertEqual(numeric_ids('10, 20', 'ALLOWED_USER_IDS'), frozenset({10,20}))

    def test_private_chat_ignored_even_for_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            api, cwa = Mock(), Mock()
            api.call.return_value = {'id': 5, 'username': 'MyBot'}
            cfg = Config('secret', -123, 'existing-conversation', 'auth.json', self.peers,
                         self.allowed, self.peer_ids, state_file=str(Path(folder) / 'cursor.json'))
            with patch('builtins.print'):
                app = AgentRelay(cfg, api, cwa)
                app.process_update({'update_id': 15, 'message': {**self.message, 'chat': {'id': 10, 'type': 'private'}}})
                app.process_update({'update_id': 16, 'message': {**self.message, 'chat': {'id': -123, 'type': 'private'}}})
            cwa.send.assert_not_called()

    def test_manual_auth_raw_session_json(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'auth_data.json'
            path.write_text('{"accessToken":"local-secret","sessionToken":"local-secret"}', encoding='utf-8')
            ensure_auth_file(str(path))
            self.assertEqual(path.read_text(encoding='utf-8'), '{"accessToken":"local-secret","sessionToken":"local-secret"}')

    def test_manual_auth_existing_cwa_cookie_format(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'auth_data.json'
            path.write_text('{"accessToken":"local-secret","cookies":{"__Secure-next-auth.session-token.0":"cookie"}}', encoding='utf-8')
            ensure_auth_file(str(path))

    def test_manual_auth_missing_and_malformed_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'auth_data.json'
            with self.assertRaisesRegex(ValueError, 'missing'):
                ensure_auth_file(str(path))
            for raw in ('not json', '{}', '{"accessToken":"secret"}', '[]'):
                path.write_text(raw, encoding='utf-8')
                with self.assertRaises(ValueError):
                    ensure_auth_file(str(path))

    def test_one_hop_and_utf16_split(self):
        value = neutralize_peer_mentions('Hi @FriendBot /ask@friendbot', self.peers)
        self.assertNotIn('@FriendBot', value)
        self.assertNotIn('@friendbot', value)
        text = '🧠' * 2400
        parts = split_for_telegram(text)
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(len(part.encode('utf-16-le')) // 2 <= 3900 for part in parts))

    def test_prompt_indicates_public_group(self):
        prompt = make_prompt(self.message, self.peers)
        self.assertIn('PUBLIC group', prompt)
        self.assertIn('Human', prompt)
        self.assertIn('@friendbot', prompt)

    def test_cursor_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'cursor.json')
            cursor = Cursor(path)
            cursor.advance(4)
            cursor.advance(3)
            self.assertEqual(Cursor(path).offset, 4)

    def test_one_cwa_send_no_replay_and_correct_conversation(self):
        with tempfile.TemporaryDirectory() as folder:
            api, cwa = Mock(), Mock()
            api.call.return_value = {'id': 5, 'username': 'MyBot'}
            cwa.send.return_value.text = 'ok @FriendBot'
            cfg = Config('secret', -123, 'existing-conversation', 'auth.json', self.peers, self.allowed, self.peer_ids,
                         state_file=str(Path(folder) / 'cursor.json'), min_interval_seconds=0)
            with patch('builtins.print'):
                app = AgentRelay(cfg, api, cwa)
                event = {'update_id': 11, 'message': self.message}
                app.process_update(event)
                app.process_update(event)
            cwa.send.assert_called_once()
            self.assertEqual(cwa.send.call_args.kwargs['conversation'], 'existing-conversation')
            send_calls = [c for c in api.call.call_args_list if c.args[0] == 'sendMessage']
            self.assertEqual(len(send_calls), 1)
            self.assertEqual(Cursor(cfg.state_file).offset, 12)

    def test_no_retry_after_cwa_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            api, cwa = Mock(), Mock()
            api.call.return_value = {'id': 5, 'username': 'MyBot'}
            cwa.send.side_effect = TimeoutError('potentially submitted')
            cfg = Config('secret', -123, 'existing-conversation', 'auth.json', self.peers, self.allowed, self.peer_ids,
                         state_file=str(Path(folder) / 'cursor.json'), min_interval_seconds=0)
            with patch('builtins.print'):
                app = AgentRelay(cfg, api, cwa)
                event = {'update_id': 11, 'message': self.message}
                app.process_update(event)
                app.process_update(event)
            cwa.send.assert_called_once()
            self.assertEqual(Cursor(cfg.state_file).offset, 12)

    def test_other_group_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            api, cwa = Mock(), Mock()
            api.call.return_value = {'id': 5, 'username': 'MyBot'}
            cfg = Config('secret', -123, 'existing-conversation', 'auth.json', self.peers, self.allowed, self.peer_ids,
                         state_file=str(Path(folder) / 'cursor.json'))
            with patch('builtins.print'):
                app = AgentRelay(cfg, api, cwa)
                app.process_update({'update_id': 1, 'message': {**self.message, 'chat': {'id': -999}}})
            cwa.send.assert_not_called()
    def test_unaddressed_gif_mp4_is_ignored_without_cwa(self):
        with tempfile.TemporaryDirectory() as folder:
            api, cwa = Mock(), Mock()
            api.call.return_value = {'id': 5, 'username': 'MyBot'}
            cfg = Config('secret', -123, 'existing-conversation', 'auth.json',
                         self.peers, self.allowed, self.peer_ids,
                         state_file=str(Path(folder) / 'cursor.json'),
                         min_interval_seconds=0, media_enabled=True, media_auto_reply=True)
            with patch('builtins.print'):
                app = AgentRelay(cfg, api, cwa)
                for update_id, media in (
                    (11, {'document': {'file_id': 'f', 'file_name': 'gif.mp4',
                                       'mime_type': 'video/mp4'}}),
                    (12, {'animation': {'file_id': 'f'}}),
                    (13, {'video': {'file_id': 'f'}}),
                ):
                    app.process_update({'update_id': update_id,
                                        'message': {'chat': {'id': -123, 'type': 'group'},
                                                    'message_id': update_id,
                                                    'from': self.human, **media}})
            cwa.send.assert_not_called()
            self.assertEqual([x for x in api.call.call_args_list
                              if x.args[0] == 'sendMessage'], [])

    def test_addressed_mp4_returns_error_without_cwa(self):
        with tempfile.TemporaryDirectory() as folder:
            api, cwa = Mock(), Mock()
            api.call.return_value = {'id': 5, 'username': 'MyBot'}
            cfg = Config('secret', -123, 'existing-conversation', 'auth.json',
                         self.peers, self.allowed, self.peer_ids,
                         state_file=str(Path(folder) / 'cursor.json'),
                         min_interval_seconds=0, media_enabled=True, media_auto_reply=True)
            with patch('builtins.print'):
                app = AgentRelay(cfg, api, cwa)
                app.process_update({'update_id': 11,
                                    'message': {'chat': {'id': -123, 'type': 'group'},
                                                'message_id': 22, 'from': self.human,
                                                'caption': '@MyBot распознай GIF',
                                                'document': {'file_id': 'f',
                                                             'file_name': 'gif.mp4'}}})
            cwa.send.assert_not_called()
            messages = [x.args[1] for x in api.call.call_args_list
                        if x.args[0] == 'sendMessage']
            self.assertEqual(len(messages), 1)
            self.assertIn('не поддерживается', messages[0]['text'])

    def test_addressed_reply_to_photo_passes_image_to_cwa(self):
        with tempfile.TemporaryDirectory() as folder:
            api, cwa = Mock(), Mock()
            api.call.return_value = {'id': 5, 'username': 'MyBot'}
            cwa.send.return_value.text = 'Это фотография кота.'
            cfg = Config('secret', -123, 'existing-conversation', 'auth.json',
                         self.peers, self.allowed, self.peer_ids,
                         state_file=str(Path(folder) / 'cursor.json'),
                         min_interval_seconds=0, media_enabled=True,
                         media_auto_reply=False)
            quoted = {'from': {'id': 11}, 'photo': [{'file_id': 'photo'}]}
            with patch('agent_relay.prepare_quoted_attachment',
                       return_value=Incoming(prompt='quoted photo',
                                             media=[(b'jpeg', 'image.jpg')],
                                             has_file=True)):
                with patch('builtins.print'):
                    app = AgentRelay(cfg, api, cwa)
                    app.process_update({'update_id': 11, 'message': {
                        'chat': {'id': -123, 'type': 'group'},
                        'message_id': 20, 'from': self.human,
                        'text': '@MyBot что на картинке?',
                        'reply_to_message': quoted}})
            self.assertIn('quoted photo', cwa.send.call_args.args[0])
            self.assertEqual(cwa.send.call_args.kwargs['media'], [(b'jpeg', 'image.jpg')])

    def test_unaddressed_reply_to_gif_does_not_trigger_cwa(self):
        with tempfile.TemporaryDirectory() as folder:
            api, cwa = Mock(), Mock()
            api.call.return_value = {'id': 5, 'username': 'MyBot'}
            cfg = Config('secret', -123, 'existing-conversation', 'auth.json',
                         self.peers, self.allowed, self.peer_ids,
                         state_file=str(Path(folder) / 'cursor.json'),
                         min_interval_seconds=0, media_enabled=True,
                         media_auto_reply=True)
            with patch('builtins.print'):
                app = AgentRelay(cfg, api, cwa)
                app.process_update({'update_id': 11, 'message': {
                    'chat': {'id': -123, 'type': 'group'},
                    'message_id': 20, 'from': self.human, 'text': 'класс!',
                    'reply_to_message': {'animation': {'file_id': 'gif'}}}})
            cwa.send.assert_not_called()



if __name__ == '__main__':
    unittest.main()
