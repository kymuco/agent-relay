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