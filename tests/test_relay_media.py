import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import sys
import types
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from relay_media import (
    Incoming, artifact_filenames, attachment_info, download_attachment,
    encode_multipart, markdown_to_telegram_html, prepare_incoming, safe_filename,
    send_file, wants_generated_image, generated_image_keys, find_new_images,
    unsupported_attachment, supported_document, quoted_attachment,
    prepare_quoted_attachment, animation_info,
)


class Response(io.BytesIO):
    def geturl(self):
        return 'https://api.telegram.org/file/botTOKEN/photos/file_0.jpg'


class Tests(unittest.TestCase):
    def test_attachments(self):
        self.assertEqual(attachment_info({'photo':[{'file_id':'first','file_size':1},
                                                   {'file_id':'last','file_size':4}]}),
                         ('last','telegram-photo.jpg',4))
        self.assertIsNone(attachment_info({'sticker':{'file_id':'xyz'}}))
        self.assertEqual(safe_filename('../a.pdf'), 'attachment.bin')

    @patch('relay_media.urlopen')
    def test_download_bounded_and_host_anchored(self, urlopen):
        api=Mock()
        api.call.return_value={'file_path':'photos/file_0.jpg','file_size':4}
        urlopen.return_value.__enter__.return_value=Response(b'DATA')
        name,data=download_attachment(api,'TOKEN',{'photo':[{'file_id':'x','file_size':4}]},limit=10)
        self.assertEqual(data,b'DATA')
        self.assertTrue(urlopen.call_args.args[0].full_url.startswith('https://api.telegram.org/file/botTOKEN/'))
        api.call.return_value={'file_path':'../../foo','file_size':1}
        with self.assertRaisesRegex(ValueError,'Unsafe'):
            download_attachment(api,'TOKEN',{'photo':[{'file_id':'x','file_size':1}]})
        with self.assertRaisesRegex(ValueError,'exceeds'):
            download_attachment(api,'TOKEN',{'photo':[{'file_id':'x','file_size':20}]},limit=10)

    def test_unsolicited_animations_are_not_attachments(self):
        cases = [
            {'document': {'file_id': 'f', 'file_name': 'gif.mp4', 'mime_type': 'video/mp4'}},
            {'document': {'file_id': 'f', 'file_name': 'moving.gif', 'mime_type': 'image/gif'}},
            {'animation': {'file_id': 'f'}},
            {'video': {'file_id': 'f'}},
            {'sticker': {'file_id': 'f'}},
            {'audio': {'file_id': 'f'}},
        ]
        for item in cases:
            with self.subTest(item=item):
                self.assertTrue(unsupported_attachment(item))
                self.assertIsNone(attachment_info(item))
                with self.assertRaisesRegex(ValueError, 'No supported attachment'):
                    download_attachment(Mock(), 'TOKEN', item)
        self.assertTrue(supported_document({'file_id': 'f', 'file_name': 'source.py'}))
        self.assertFalse(unsupported_attachment({'photo': [{'file_id': 'f'}]}))
        self.assertFalse(unsupported_attachment({'voice': {'file_id': 'f'}}))

    def test_quoted_picture_and_gif_detection(self):
        photo = {'photo': [{'file_id': 'photo', 'file_size': 4}]}
        gif = {'animation': {'file_id': 'gif', 'mime_type': 'video/mp4'}}
        mp4 = {'document': {'file_id': 'clip', 'file_name': 'example.mp4',
                            'mime_type': 'video/mp4'}}
        for quoted in (photo, gif, mp4):
            self.assertIs(quoted_attachment({'text': '@MyBot что здесь?',
                                              'reply_to_message': quoted}), quoted)
        self.assertIsNone(quoted_attachment({'text': 'нет вложения'}))
        self.assertIsNone(quoted_attachment({'text': 'вопрос', 'reply_to_message':
                                              {'text': 'without media'}}))
        self.assertEqual(animation_info(mp4)[1], 'example.mp4')
        self.assertIsNone(attachment_info(mp4))

    def test_quoted_photo_forwards_media(self):
        quoted = {'photo': [{'file_id': 'source', 'file_size': 10}]}
        with patch('relay_media.download_attachment',
                   return_value=('telegram-photo.jpg', b'fake-jpeg')):
            result = prepare_quoted_attachment(Mock(), 'TOKEN', quoted, 'auth.json')
        self.assertEqual(result.media, [(b'fake-jpeg', 'telegram-photo.jpg')])
        self.assertIn('older Telegram attachment', result.prompt)

    def test_quoted_gif_extracts_bounded_frames(self):
        quoted = {'animation': {'file_id': 'g', 'file_name': 'loop.mp4'}}
        with patch('relay_media._download_selected', return_value=('loop.mp4', b'mp4-content')):
            with patch('relay_media.shutil.which', return_value='ffmpeg'):
                def fake_ffmpeg(args, **kwargs):
                    directory = Path(args[-1]).parent
                    (directory / 'sample-01.jpg').write_bytes(b'jpeg1')
                    (directory / 'sample-02.jpg').write_bytes(b'jpeg2')
                    return types.SimpleNamespace(returncode=0)
                with patch('relay_media.subprocess.run', side_effect=fake_ffmpeg):
                    incoming = prepare_quoted_attachment(Mock(), 'TOKEN', quoted, 'auth.json')
        self.assertEqual(len(incoming.media), 2)
        self.assertIn('sampled frames', incoming.prompt)
        self.assertTrue(all(len(data) > 0 for data, name in incoming.media))

    def test_prepare_text_document(self):
        with patch('relay_media.download_attachment',return_value=('source.py',b'print("hi")\n')):
            inp=prepare_incoming(Mock(),'TOKEN',{'document':{'file_id':'x','file_name':'source.py'}},'auth.json')
        self.assertIn('print("hi")',inp.prompt)
        self.assertTrue(inp.has_file)
        with patch('relay_media.download_attachment',return_value=('source.exe',b'evil')):
            with self.assertRaisesRegex(ValueError,'Unsupported'):
                prepare_incoming(Mock(),'TOKEN',{'document':{'file_id':'x','file_name':'source.exe'}},'auth.json')

    def test_prepare_image(self):
        with patch('relay_media.download_attachment',return_value=('telegram-photo.jpg',b'\xff\xd8test')):
            inp=prepare_incoming(Mock(),'TOKEN',{'photo':[{'file_id':'x'}]},'auth.json')
        self.assertEqual(inp.media,[(b'\xff\xd8test','telegram-photo.jpg')])

    def test_voice_uses_user_module(self):
        with patch('relay_media.download_attachment',return_value=('telegram-voice.ogg',b'OggS')):
            with patch('relay_media.shutil.which',return_value='/usr/bin/ffmpeg'):
                tr=Mock(return_value='Привет, Рагна')
                with patch.dict(sys.modules, {'cwa_dictate': types.SimpleNamespace(transcribe=tr)}):
                    inp=prepare_incoming(Mock(),'TOKEN',{'voice':{'file_id':'x'}},'auth.json')
        self.assertTrue(inp.is_voice)
        self.assertIn('Привет, Рагна',inp.prompt)
        self.assertEqual(tr.call_count,1)

    def test_markdown_html_safe(self):
        text='**Bold** and `a < b`\n> quote <hello>\n```python\nprint("ok")\n```\n[link](https://example.com/a)'
        result=markdown_to_telegram_html(text)
        self.assertIn('<b>Bold</b>',result)
        self.assertIn('<code>a &lt; b</code>',result)
        self.assertIn('<blockquote>quote &lt;hello&gt;</blockquote>',result)
        self.assertIn('<pre><code>',result)
        self.assertIn('<a href="https://example.com/a">link</a>',result)
        self.assertNotIn('<hello>',result)

    def test_generated_artifacts_whitelisted(self):
        names=artifact_filenames('[a](sandbox:/mnt/data/report.pdf) sandbox:/mnt/data/x.py '\
                                 'sandbox:/mnt/data/bad.exe sandbox:/mnt/data/../../secret')
        self.assertEqual(names,['report.pdf','x.py'])
        self.assertTrue(wants_generated_image('Нарисуй картинку'))
        self.assertFalse(wants_generated_image('проверь мой Python'))
        client=Mock()
        client.get_generated_images.return_value=[Mock(message_id='m1',filename='x.png',size_bytes=100),
                                                   Mock(message_id='m2',filename='new.png',size_bytes=200)]
        self.assertEqual([x.filename for x in find_new_images(client,'c',{('m1','x.png')})],['new.png'])
        self.assertEqual(len(generated_image_keys(client,'c')),2)

    @patch('relay_media.urlopen')
    def test_multipart_binary_and_no_leak(self, urlopen):
        mime,body=encode_multipart({'chat_id':-12},'voice','voice.ogg',b'\x00\x01\x02','audio/ogg')
        self.assertIn(b'\x00\x01\x02',body)
        self.assertIn(b'name="voice"',body)
        self.assertIn('multipart/form-data',mime)
        urlopen.return_value.__enter__.return_value=io.BytesIO(json.dumps({'ok':True,'result':{'message_id':42}}).encode())
        res=send_file('TOKEN','sendVoice',-12,b'OggS','voice.ogg',reply_message_id=10)
        self.assertEqual(res['message_id'],42)
        self.assertIn(b'"message_id": 10',urlopen.call_args.args[0].data)

if __name__=='__main__':
    unittest.main()