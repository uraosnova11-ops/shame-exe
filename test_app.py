import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('YURI_DATA_DIR',str(Path(__file__).resolve().parent/'work'/'test-runtime'))
import app

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
            s=app.settings(c);s.update(auto=True,telegram=True,allowed_hours='0-24',test_sent=True)
            c.execute('UPDATE settings SET body=? WHERE id=1',(json.dumps(s,ensure_ascii=False),))
            self.run_job(c,'analyze',cid)
            self.assertEqual(c.execute('SELECT status FROM companies WHERE id=?',(cid,)).fetchone()['status'],'проверена')
            self.run_job(c,'demo',cid)
            r=c.execute('SELECT * FROM companies WHERE id=?',(cid,)).fetchone()
            self.assertIn('НЕЗАВИСИМАЯ', (app.DATA/'demos'/r['demo_path'].split('/')[-1]).read_text(encoding='utf-8'))
            c.execute("UPDATE companies SET channel='telegram',telegram_username='@test_bakery',chat_verified=1,consent=1,contact_evidence='Сайт компании' WHERE id=?",(cid,))
            calls=[]
            def fake_send(channel,recipient,body,image_path=None):
                calls.append((recipient,body))
                return ('42','123')
            with patch.object(app,'send',fake_send):
                self.run_job(c,'send',cid)
                self.run_job(c,'send',cid)
            self.assertEqual(len(calls),1)
            self.assertEqual(calls[0][0],'@test_bakery')
            self.assertIn('Тестовая пекарня',calls[0][1])
            self.assertEqual(c.execute("SELECT state FROM messages WHERE direction='out'").fetchone()['state'],'sent')
            app.inbound(c,'telegram','123','стоп','3')
            self.assertEqual(c.execute('SELECT status FROM companies WHERE id=?',(cid,)).fetchone()['status'],'отказ')
    def test_no_consent_no_send(self):
        with app.db() as c:
            cid=app.add_company(c,{'name':'Тест','category':'салон красоты'})
            s=app.settings(c);s.update(auto=True,telegram=True,allowed_hours='0-24')
            c.execute('UPDATE settings SET body=? WHERE id=1',(json.dumps(s,ensure_ascii=False),))
            self.run_job(c,'demo',cid)
            c.execute("UPDATE companies SET channel='telegram',telegram_username='@test_contact',chat_verified=1 WHERE id=?",(cid,))
            with self.assertRaisesRegex(ValueError,'разрешения'):
                self.run_job(c,'send',cid)
            self.assertFalse(c.execute('SELECT * FROM messages').fetchone())

if __name__=='__main__': unittest.main()
