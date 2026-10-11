import concurrent.futures,http.client,pathlib,socket,sys,tempfile,threading,time,unittest
from adapter import ReadinessServer
class AdapterTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=pathlib.Path(self.tmp.name);self.marker=self.root/'ran';self.servers=[]
 def tearDown(self):
  for s,t in self.servers:s.shutdown();s.server_close();t.join()
  self.tmp.cleanup()
 def server(self,code=None,timeout=.3,slots=2):
  code=code or f"from pathlib import Path;Path({str(self.marker)!r}).write_text('yes')"
  server=ReadinessServer(('127.0.0.1',0),token='private-test-token',path='/.well-known/apex-ready',command=[sys.executable,'-c',code],timeout=timeout,slots=slots)
  t=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);t.start();self.servers.append((server,t));return server
 def request(self,s,path='/.well-known/apex-ready',token='private-test-token',method='GET',extra_token=False):
  c=http.client.HTTPConnection(*s.server_address,timeout=3);c.putrequest(method,path)
  if token is not None:c.putheader('X-APEX-Ready-Token',token)
  if extra_token:c.putheader('X-APEX-Ready-Token',token)
  c.endheaders();r=c.getresponse();out=(r.status,r.getheader('Cache-Control'),r.read());c.close();return out
 def test_authenticated_success_runs_fixed_probe(self):
  self.assertEqual(self.request(self.server()),(204,'no-store',b''));self.assertTrue(self.marker.exists())
 def test_absent_wrong_and_duplicate_tokens_never_probe(self):
  s=self.server()
  for token,duplicate in [(None,False),('bad',False),('private-test-token',True)]:self.assertEqual(self.request(s,token=token,extra_token=duplicate)[0],403)
  self.assertFalse(self.marker.exists())
 def test_noncanonical_paths_and_methods_never_probe(self):
  s=self.server()
  for path in ['/other','/.well-known/apex-ready?x=1','/.well-known/%61pex-ready','//.well-known/apex-ready']:
   self.assertEqual(self.request(s,path=path)[0],404)
  self.assertEqual(self.request(s,method='POST')[0],405);self.assertFalse(self.marker.exists())
 def test_nonzero_exit_and_missing_executable_fail_closed(self):
  s=self.server('raise SystemExit(1)');self.assertEqual(self.request(s),(503,'no-store',b''));s.command=['/nonexistent/apex-probe'];self.assertEqual(self.request(s)[0],503)
 def test_timeout_terminates_probe_and_descendant(self):
  code=f"import subprocess,time,sys;subprocess.Popen([sys.executable,'-c',\"import time,pathlib;time.sleep(.8);pathlib.Path({str(self.marker)!r}).write_text('leaked')\"]);time.sleep(3)"
  s=self.server(code,timeout=.1);start=time.monotonic();self.assertEqual(self.request(s)[0],503);self.assertLess(time.monotonic()-start,.7);time.sleep(.9);self.assertFalse(self.marker.exists())
 def test_concurrency_limit_rejects_before_spawn(self):
  code=f"from pathlib import Path;import time;Path({str(self.marker)!r}).write_text('yes');time.sleep(.4)"
  s=self.server(code,timeout=1,slots=1)
  with concurrent.futures.ThreadPoolExecutor() as pool:
   first=pool.submit(self.request,s)
   for _ in range(100):
    if self.marker.exists():break
    time.sleep(.005)
   self.assertTrue(self.marker.exists());self.assertEqual(self.request(s)[0],503);self.assertEqual(first.result()[0],204)
 def test_unsupported_method_is_empty_and_not_cached(self):
  self.assertEqual(self.request(self.server(),method='TRACE'),(501,'no-store',b''));self.assertFalse(self.marker.exists())
 def test_parser_error_is_empty_and_not_cached(self):
  s=self.server()
  with socket.create_connection(s.server_address,timeout=2) as sock:
   sock.sendall(b'GET /.well-known/apex-ready HTTP/banana\r\n\r\n')
   r=http.client.HTTPResponse(sock);r.begin();self.assertEqual((r.status,r.getheader('Cache-Control'),r.read()),(400,'no-store',b''))
  self.assertFalse(self.marker.exists())
 def test_no_cached_readiness(self):
  s=self.server();self.assertEqual(self.request(s)[0],204);s.command=[sys.executable,'-c','raise SystemExit(1)'];self.assertEqual(self.request(s)[0],503)
if __name__=='__main__':unittest.main()
