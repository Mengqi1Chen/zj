import base64
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch,Mock
from apx240.public_server import PublicConfig,PublicServer,PublicHandler,RequestBudget

ENV={'APX_PUBLIC_ORIGIN':'https://demo.example','APX_ACCESS_USER':'reviewer','APX_ACCESS_PASSWORD':'test-only-random-password-12345'}

class PublicAccess(unittest.TestCase):
    def setUp(self):
        p=patch.dict('os.environ',ENV);p.start();self.addCleanup(p.stop)
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.path=Path(tmp.name)/'budget.db'
        self.server=PublicServer(('127.0.0.1',0),PublicHandler)
        self.server.config=PublicConfig();self.server.budget=RequestBudget(str(self.path),1,2)
        self.server.sessions=Mock();self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.addCleanup(self.stop)
    def stop(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
    def request(self,path='/',method='GET',authorized=True,origin=None,host='demo.example'):
        headers={'Host':host,'Content-Type':'application/json'}
        if authorized:headers['Authorization']='Basic '+base64.b64encode((ENV['APX_ACCESS_USER']+':'+ENV['APX_ACCESS_PASSWORD']).encode()).decode()
        if origin:headers['Origin']=origin
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        try:
            conn.request(method,path,body='{}' if method=='POST' else None,headers=headers)
            resp=conn.getresponse();return resp.status,resp.getheader('WWW-Authenticate'),resp.read()
        finally:conn.close()
    def test_root_requires_password(self):
        status,challenge,_=self.request(authorized=False);self.assertEqual(status,401);self.assertIn('Basic',challenge)
        self.assertEqual(self.request()[0],200)
    def test_authenticated_reads_work_through_proxy_host_rewrite(self):
        self.assertEqual(self.request(host='internal-render-proxy')[0],200)
        self.assertEqual(self.request('/manual.pdf',host='internal-render-proxy')[0],200)
    def test_proxy_host_rewrite_does_not_relax_write_origin_check(self):
        self.assertEqual(self.request('/api/sessions','POST',host='internal-render-proxy')[0],403)
    def test_health_requires_password(self):self.assertEqual(self.request('/health',authorized=False)[0],401)
    def test_manual_requires_password_and_serves_snapshot(self):
        self.assertEqual(self.request('/manual.pdf',authorized=False)[0],401)
        status,_,body=self.request('/manual.pdf');self.assertEqual(status,200);self.assertTrue(body.startswith(b'%PDF'))
    def test_write_without_password_never_calls_session(self):
        self.assertEqual(self.request('/api/sessions','POST',False)[0],401);self.server.sessions.create.assert_not_called()
    def test_cross_origin_rejected_before_budget(self):
        self.assertEqual(self.request('/api/sessions','POST',origin='https://wrong.example')[0],403)
        self.assertEqual(self.request('/api/sessions','POST')[0],400)
    def test_budget_persists_and_returns_json(self):
        self.assertEqual(self.request('/api/sessions','POST')[0],400)
        self.server.budget=RequestBudget(str(self.path),1,2)
        status,_,body=self.request('/api/sessions','POST');self.assertEqual(status,429);self.assertIn('error',json.loads(body))
    def test_missing_password_fails_configuration(self):
        with patch.dict('os.environ',{'APX_ACCESS_PASSWORD':''}),self.assertRaises(ValueError):PublicConfig()
    def test_api_key_cannot_be_access_password(self):
        with patch.dict('os.environ',{'APX_LLM_KEY':ENV['APX_ACCESS_PASSWORD']}),self.assertRaises(ValueError):PublicConfig()
    def test_http_origin_not_allowed(self):
        with patch.dict('os.environ',{'APX_PUBLIC_ORIGIN':'http://demo.example'}),self.assertRaises(ValueError):PublicConfig()
    def test_daily_limit_survives_hour_change(self):
        b=RequestBudget(str(self.path),1,2)
        self.assertTrue(b.consume(0));self.assertFalse(b.consume(1));self.assertTrue(b.consume(3600));self.assertFalse(b.consume(7200));self.assertTrue(b.consume(86400))
    def test_render_origin_and_port(self):
        with patch.dict('os.environ',{'APX_PUBLIC_ORIGIN':'','RENDER_EXTERNAL_URL':'https://apx-test.onrender.com','PORT':'10000'}):
            cfg=PublicConfig();self.assertEqual(cfg.host,'apx-test.onrender.com');self.assertEqual(cfg.port,10000)
    def test_invalid_render_port_rejected(self):
        with patch.dict('os.environ',{'PORT':'-1'}),self.assertRaises(ValueError):PublicConfig()
    def test_readiness_does_not_expose_configuration(self):
        with patch('apx240.public_server.configuration_status',return_value='configured'):
            status,_,body=self.request('/ready',authorized=False)
        self.assertEqual(status,200);self.assertEqual(json.loads(body),{'status':'ready'})
    def test_readiness_fails_for_missing_model(self):
        with patch('apx240.public_server.configuration_status',return_value='disabled'):
            self.assertEqual(self.request('/ready',authorized=False)[0],503)
