"""Voice-only vs text+voice delivery for a shared Telegram agent."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_relay import (AgentRelay, Config, SPOKEN_REPLY_GUIDANCE,
                         reply_delivery_mode)
from relay_media import Incoming


class VoiceDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.human = {'id': 10, 'first_name': 'Human', 'is_bot': False}
        self.peer = {'id': 20, 'username': 'FriendBot', 'is_bot': True}
        self.peers = frozenset({'friendbot'})
        self.allowed = frozenset({10})
        self.peer_ids = frozenset({20})

    def msg(self, text, *, mid=31, actor=None, **attachment):
        return {'chat': {'id': -123, 'type': 'supergroup'}, 'message_id': mid,
                'from': actor or self.human, 'text': text, **attachment}

    def run_message(self, text, *, mode='on_voice', is_voice=False,
                    actor=None, speech_error=None):
        with tempfile.TemporaryDirectory() as folder:
            api, cwa = Mock(), Mock()
            api.call.return_value = {'id': 5, 'username': 'MyBot'}
            cwa.send.return_value.text = 'Привет! Всё хорошо, а у тебя как дела?'
            cfg = Config('secret', -123, 'existing-conversation', 'auth.json',
                         self.peers, self.allowed, self.peer_ids,
                         state_file=str(Path(folder) / 'cursor.json'),
                         min_interval_seconds=0, media_enabled=True,
                         media_auto_reply=True, voice_reply_mode=mode)
            attachment = {'voice': {'file_id': 'fake-voice'}} if is_voice else {}
            message = self.msg(text, actor=actor, **attachment)
            prepared = Incoming(prompt='Распознано: привет', is_voice=True, has_file=True)
            with patch('agent_relay.prepare_incoming', return_value=prepared), \
                 patch('agent_relay.synthesize_voice', return_value=b'OggS') as tts, \
                 patch('agent_relay.send_file') as send_media, \
                 patch('builtins.print'):
                if speech_error:
                    tts.side_effect = speech_error
                app = AgentRelay(cfg, api, cwa)
                app.process_update({'update_id': 1, 'message': message})
                return {
                    'text_calls': [c.args[1] for c in api.call.call_args_list
                                   if c.args[0] == 'sendMessage'],
                    'send_media': send_media.call_args_list,
                    'tts_calls': tts.call_args_list,
                    'prompt': cwa.send.call_args.args[0] if cwa.send.called else '',
                    'cwa_calls': cwa.send.call_count,
                }

    def test_voice_command_delivers_only_voice(self):
        outcome = self.run_message('@MyBot /voice Привет, как дела?')
        self.assertEqual(outcome['cwa_calls'], 1)
        self.assertEqual(outcome['text_calls'], [])
        self.assertEqual(len(outcome['send_media']), 1)
        self.assertEqual(outcome['send_media'][0].args[1], 'sendVoice')
        self.assertIn('spoken reply delivery', outcome['prompt'])
        self.assertIn('Speak naturally', outcome['prompt'])
        self.assertIn('Do not mention voice synthesis', outcome['prompt'])

    def test_both_command_delivers_both(self):
        outcome = self.run_message('@MyBot /both Привет')
        self.assertEqual(len(outcome['text_calls']), 1)
        self.assertEqual(len(outcome['send_media']), 1)
        self.assertEqual(outcome['send_media'][0].args[1], 'sendVoice')
        self.assertIn('spoken reply delivery', outcome['prompt'])

    def test_incoming_voice_defaults_to_voice_only(self):
        outcome = self.run_message('', is_voice=True)
        self.assertEqual(outcome['text_calls'], [])
        self.assertEqual(len(outcome['send_media']), 1)
        self.assertIn('Распознано:', outcome['prompt'])

    def test_normal_text_is_still_text_only(self):
        outcome = self.run_message('@MyBot Привет')
        self.assertEqual(len(outcome['text_calls']), 1)
        self.assertEqual(len(outcome['send_media']), 0)
        self.assertNotIn(SPOKEN_REPLY_GUIDANCE, outcome['prompt'])

    def test_voice_failure_falls_back_to_text(self):
        outcome = self.run_message('@MyBot /voice Привет', speech_error=RuntimeError('tts offline'))
        self.assertEqual(outcome['cwa_calls'], 1)
        self.assertEqual(len(outcome['text_calls']), 1)
        self.assertEqual(len(outcome['send_media']), 0)

    def test_voice_off_disables_voice_commands(self):
        outcome = self.run_message('@MyBot /voice Привет', mode='off')
        self.assertEqual(len(outcome['text_calls']), 1)
        self.assertEqual(len(outcome['send_media']), 0)
        self.assertNotIn(SPOKEN_REPLY_GUIDANCE, outcome['prompt'])

    def test_always_mode_text_and_voice_by_default(self):
        outcome = self.run_message('@MyBot Привет', mode='always')
        self.assertEqual(len(outcome['text_calls']), 1)
        self.assertEqual(len(outcome['send_media']), 1)

    def test_peer_bot_does_not_speak_even_with_always(self):
        outcome = self.run_message('/relay@MyBot hi', mode='always', actor=self.peer)
        self.assertEqual(len(outcome['text_calls']), 1)
        self.assertEqual(len(outcome['send_media']), 0)

    def test_precise_mode_tokens_and_targeted_commands(self):
        f = lambda text, incoming=False, mode='on_voice': reply_delivery_mode(
            text, 'MyBot', incoming_voice=incoming, voice_reply_mode=mode,
            from_bot=False, media_enabled=True)
        self.assertEqual(f('@MyBot /voice@MyBot Привет'), 'voice')
        self.assertEqual(f('@MyBot /both@MyBot Привет'), 'both')
        self.assertEqual(f('@MyBot /voice@OtherBot Привет'), 'text')
        self.assertEqual(f('@MyBot /voiceover привет'), 'text')
        self.assertEqual(f('@MyBot /both Привет', incoming=True), 'both')
        self.assertEqual(f('/voice Привет', mode='always'), 'voice')
        self.assertEqual(f('/both Привет', mode='always'), 'both')


if __name__ == '__main__':
    unittest.main()
