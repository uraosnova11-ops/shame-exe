import json
import os
import tempfile
import unittest
import io
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('YURI_DATA_DIR',str(Path(__file__).resolve().parent/'work'/'test-runtime'))
import app
import vk_user

class FlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.old=(app.DATA,app.DB,app.PUBLIC_URL)
        app.DATA=Path(self.tmp.name);app.DB=app.DATA/'test.sqlite3';app.PUBLIC_URL='https://demo.example'
        app.init()
    def tearDown(self):
        app.DATA,app.DB,app.PUBLIC_URL=self.old
        self.tmp.cleanup()
    def run_job(self,c,kind,cid):
        app.perform(c,{'kind':kind,'company_id':cid})
    def test_end_to_end_with_test_transport(self):
        with app.db() as c:
            cid=app.add_company(c,{'name':'Тестовая пекарня','category':'пекарня','address':'Ростов-на-Дону, Тестовая 1','source':'import'})
            s=app.settings(c);s.update(auto=True,vk=True,allowed_hours='0-24',test_sent=True)
            c.execute('UPDATE settings SET body=? WHERE id=1',(json.dumps(s,ensure_ascii=False),))
            self.run_job(c,'analyze',cid)
            self.assertEqual(c.execute('SELECT status FROM companies WHERE id=?',(cid,)).fetchone()['status'],'проверена')
            self.run_job(c,'demo',cid)
            r=c.execute('SELECT * FROM companies WHERE id=?',(cid,)).fetchone()
            self.assertIn('НЕЗАВИСИМАЯ', (app.DATA/'demos'/r['demo_path'].split('/')[-1]).read_text(encoding='utf-8'))
            c.execute("UPDATE companies SET channel='vk',chat_id='-123',chat_verified=1,consent=1,contact_evidence='Сайт компании' WHERE id=?",(cid,))
            calls=[]
            def fake_send(channel,recipient,body,image_path=None):
                calls.append((recipient,body))
                return ('42','-123')
            with patch.object(app,'send',fake_send),patch.object(app.urllib.request,'urlopen',return_value=io.BytesIO('НЕЗАВИСИМАЯ КОНЦЕПЦИЯ'.encode())):
                self.run_job(c,'send',cid)
                self.run_job(c,'send',cid)
            self.assertEqual(len(calls),1)
            self.assertEqual(calls[0][0],'-123')
            self.assertIn('Тестовая пекарня',calls[0][1])
            self.assertEqual(c.execute("SELECT state FROM messages WHERE direction='out'").fetchone()['state'],'sent')
            app.inbound(c,'vk','-123','стоп','3')
            self.assertEqual(c.execute('SELECT status FROM companies WHERE id=?',(cid,)).fetchone()['status'],'отказ')
    def test_no_consent_no_send(self):
        with app.db() as c:
            cid=app.add_company(c,{'name':'Тест','category':'салон красоты'})
            s=app.settings(c);s.update(auto=True,vk=True,allowed_hours='0-24')
            c.execute('UPDATE settings SET body=? WHERE id=1',(json.dumps(s,ensure_ascii=False),))
            self.run_job(c,'demo',cid)
            c.execute("UPDATE companies SET channel='vk',chat_id='-234',chat_verified=1 WHERE id=?",(cid,))
            with self.assertRaisesRegex(ValueError,'разрешения'):
                self.run_job(c,'send',cid)
            self.assertFalse(c.execute('SELECT * FROM messages').fetchone())

    def test_vk_community_only_and_rate_limit(self):
        with patch.object(vk_user,'call',return_value=[{'id':123,'name':'Тестовая пекарня'}]):
            peer,name=vk_user.community('test-token','https://vk.com/club123')
        self.assertEqual((peer,name),(-123,'Тестовая пекарня'))
        with self.assertRaisesRegex(vk_user.VKError,'сообщество'):
            vk_user.send('test-token',123,'тест')
        with patch.object(vk_user.urllib.request,'urlopen',return_value=io.BytesIO(b'{"error":{"error_code":6}}')):
            with self.assertRaises(vk_user.VKPaused):
                vk_user.send('test-token',-123,'тест')
        with patch.object(vk_user,'upload_message_photo',return_value='photo1_2'),patch.object(vk_user,'call',return_value=52) as api:
            self.assertEqual(vk_user.send('test-token',-123,'пример','demo.png'),'52')
            self.assertEqual(api.call_args.kwargs['attachment'],'photo1_2')

if __name__=='__main__': unittest.main()

