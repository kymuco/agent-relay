"""Human emergency stop and active-worker invalidation tests."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_control import ControlState
from agent_relay import AgentRelay, Config


class ControlTests(unittest.TestCase):
    def config(self, folder):
        return Config('secret', -123, 'existing', 'auth.json',
                      frozenset({'friendbot'}), frozenset({10}), frozenset({20}),
                      state_file=str(Path(folder)/'cursor.json'),
                      control_file=str(Path(folder)/'control.json'),
                      min_interval_seconds=0, peer_reply_mode=True)

    def msg(self, text, mid, actor=None):
        return {'chat': {'id': -123, 'type': 'supergroup'}, 'message_id': mid,
                'text': text, 'from': actor or {
                    'id': 10, 'is_bot': False, 'first_name': 'Owner'}}

    def test_durable_stop_resume_and_fail_closed_corruption(self):
        with tempfile.TemporaryDirectory() as folder:
            target = str(Path(folder)/'control.json')
            c = ControlState(target)
            old = c.generation()
            self.assertFalse(c.paused)
            self.assertTrue(c.stop())
            self.assertFalse(c.permits(old))
            self.assertTrue(ControlState(target).paused)
            self.assertFalse(c.stop())
            self.assertTrue(c.resume())
            self.assertFalse(c.permits(old))
            self.assertFalse(ControlState(target).paused)
            Path(target).write_text('malformed', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'Control state unreadable'):
                ControlState(target)

    def test_only_allowlisted_human_can_control(self):
        with tempfile.TemporaryDirectory() as folder:
            api, cwa = Mock(), Mock()
            api.call.return_value = {'id': 5, 'username': 'MyBot'}
            with patch('builtins.print'):
                app = AgentRelay(self.config(folder), api, cwa)
                outsider = {'id': 99, 'is_bot': False}
                bot = {'id': 20, 'is_bot': True, 'username': 'FriendBot'}
                app.process_update({'update_id': 1, 'message': self.msg('/stop@MyBot', 1, outsider)})
                app.process_update({'update_id': 2, 'message': self.msg('/stop@MyBot', 2, bot)})
                self.assertFalse(app.control.paused)
                app.process_update({'update_id': 3, 'message': self.msg('/stop@MyBot', 3)})
                self.assertTrue(app.control.paused)
                app.process_update({'update_id': 4, 'message': self.msg('@MyBot hello', 4)})
                app.process_update({'update_id': 5, 'message': self.msg('/resume@MyBot', 5)})
            cwa.send.assert_not_called()
            self.assertFalse(app.control.paused)
            posts = [x.args[1]['text'] for x in api.call.call_args_list
                     if x.args[0] == 'sendMessage']
            self.assertEqual(len(posts), 2)
            self.assertIn('Остановлен', posts[0])
            self.assertIn('Продолжаю', posts[1])

    def test_stop_during_blocked_cwa_suppresses_old_response_even_after_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            api, cwa = Mock(), Mock()
            api.call.return_value = {'id': 5, 'username': 'MyBot'}
            entered, release = threading.Event(), threading.Event()
            def blocked_send(*args, **kwargs):
                entered.set()
                if not release.wait(timeout=5):
                    raise TimeoutError('test did not release CWA')
                return Mock(text='This answer must not appear')
            cwa.send.side_effect = blocked_send
            with patch('builtins.print'):
                app = AgentRelay(self.config(folder), api, cwa)
                worker = threading.Thread(
                    target=lambda: app.process_update({
                        'update_id': 1, 'message': self.msg('@MyBot question', 1)}))
                worker.start()
                self.assertTrue(entered.wait(timeout=3))
                app.process_update({'update_id': 2, 'message': self.msg('/stop@MyBot', 2)})
                self.assertTrue(app.control.paused)
                app.process_update({'update_id': 3, 'message': self.msg('/resume@MyBot', 3)})
                release.set()
                worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(cwa.send.call_count, 1)
            posts = [x.args[1]['text'] for x in api.call.call_args_list
                     if x.args[0] == 'sendMessage']
            self.assertEqual(len(posts), 2)
            self.assertTrue(all('answer' not in item.lower() for item in posts))
            self.assertFalse(app.control.paused)


if __name__ == '__main__':
    unittest.main()
