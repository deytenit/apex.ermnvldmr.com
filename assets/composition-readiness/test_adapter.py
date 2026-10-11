import concurrent.futures,http.client,pathlib,socket,sys,tempfile,threading,time,unittest
from adapter import ReadinessServer
class AdapterTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=pathlib.Path(self.tmp.name);self.marker=self.root/'ran';self.servers=[]
 def tearDown(self):
  for s,t in self.servers:s.shutdown();s.server_close();t.join()
  self.tmp.cleanup()
 def server(self,code=None,timeout=.3,slots=2,**options):
  code=code or f"from pathlib import Path;Path({str(self.marker)!r}).write_text('yes')"
  server=ReadinessServer(('127.0.0.1',0),token='private-test-token',path='/.well-known/apex-ready',command=[sys.executable,'-c',code],timeout=timeout,slots=slots,**options)
  t=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);t.start();self.servers.append((server,t));return server
 def request(self,s,path='/.well-known/apex-ready',token='private-test-token',method='GET',extra_token=False):
  c=http.client.HTTPConnection(*s.server_address,timeout=3);c.putrequest(method,path)
  if token is not None:c.putheader('X-APEX-Ready-Token',token)
  if extra_token:c.putheader('X-APEX-Ready-Token',token)
  c.endheaders();r=c.getresponse();out=(r.status,r.getheader('Cache-Control'),r.read());c.close();return out
 def test_authenticated_success_runs_fixed_probe(self):
  self.assertEqual(self.request(self.server()),(204,'no-store',b''));self.assertTrue(self.marker.exists())
 def test_configured_three_seconds_allows_a_probe_longer_than_default(self):
  code=f"import time;from pathlib import Path;time.sleep(1.6);Path({str(self.marker)!r}).write_text('completed')"
  s=self.server(code,timeout=3,single_flight=True)
  self.assertEqual(self.request(s),(204,'no-store',b''));self.assertEqual(self.marker.read_text(),'completed')
 def test_timeout_over_three_seconds_is_rejected_before_probe(self):
  with self.assertRaises(ValueError):self.server(timeout=3.01)
  self.assertFalse(self.marker.exists())
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
 def overlapping_requests(self,s,count=4):
  barrier=threading.Barrier(count)
  def one():
   barrier.wait(timeout=2);start=time.monotonic();result=self.request(s);return result,time.monotonic()-start
  with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:return list(pool.map(lambda _:one(),range(count)))
 def test_browser_asset_burst_connects_and_shares_one_slow_probe(self):
  code=f"import pathlib,time;p=pathlib.Path({str(self.marker)!r});f=p.open('a');f.write('probe\\n');f.close();time.sleep(.8)"
  s=self.server(code,timeout=3,slots=16,single_flight=True)
  process_request=s.process_request
  def delayed_accept(request,address):
   time.sleep(.01)
   process_request(request,address)
  s.process_request=delayed_accept
  barrier=threading.Barrier(32)
  def one(_):
   barrier.wait(timeout=2)
   c=http.client.HTTPConnection(*s.server_address,timeout=.5)
   try:
    c.connect();c.sock.settimeout(3)
    c.request('GET','/.well-known/apex-ready',headers={'X-APEX-Ready-Token':'private-test-token'})
    r=c.getresponse();return r.status,r.read()
   finally:c.close()
  with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:results=list(pool.map(one,range(32)))
  self.assertEqual(results,[(204,b'')]*32)
  self.assertEqual(self.marker.read_text().splitlines(),['probe'])
 def test_single_flight_shares_success_and_sequential_request_reprobes(self):
  code=f"import pathlib,time;p=pathlib.Path({str(self.marker)!r});f=p.open('a');f.write('probe\\n');f.close();time.sleep(.2)"
  s=self.server(code,timeout=1,slots=1,single_flight=True)
  results=self.overlapping_requests(s)
  self.assertEqual([r for r,_ in results],[(204,'no-store',b'')]*4)
  self.assertEqual(self.marker.read_text().splitlines(),['probe'])
  self.assertEqual(self.request(s)[0],204)
  self.assertEqual(self.marker.read_text().splitlines(),['probe','probe'])
 def test_single_flight_shares_failure_without_caching_it(self):
  code=f"import pathlib,time;p=pathlib.Path({str(self.marker)!r});f=p.open('a');f.write('probe\\n');f.close();time.sleep(.2);raise SystemExit(1)"
  s=self.server(code,timeout=1,slots=4,single_flight=True)
  self.assertEqual([r for r,_ in self.overlapping_requests(s)],[(503,'no-store',b'')]*4)
  self.assertEqual(self.marker.read_text().splitlines(),['probe'])
  s.command=[sys.executable,'-c','pass'];self.assertEqual(self.request(s)[0],204)
 def test_single_flight_timeout_wakes_waiters_and_kills_descendant(self):
  code=f"import subprocess,time,sys;subprocess.Popen([sys.executable,'-c',\"import time,pathlib;time.sleep(.8);pathlib.Path({str(self.marker)!r}).write_text('leaked')\"]);time.sleep(3)"
  s=self.server(code,timeout=.25,slots=1,single_flight=True)
  results=self.overlapping_requests(s)
  self.assertEqual([r for r,_ in results],[(503,'no-store',b'')]*4)
  self.assertTrue(all(.15<elapsed<.7 for _,elapsed in results),results)
  time.sleep(.9);self.assertFalse(self.marker.exists())
 def test_single_flight_shutdown_wakes_waiters_and_rejects_new_requests(self):
  leaked=self.root/'descendant'
  child=f"import time,pathlib;pathlib.Path({str(self.marker)!r}).write_text('started');time.sleep(.8);pathlib.Path({str(leaked)!r}).write_text('leaked')"
  code=f"import subprocess,time,sys;subprocess.Popen([sys.executable,'-c',{child!r}]);time.sleep(3)"
  s=self.server(code,timeout=1.5,slots=1,single_flight=True)
  with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
   futures=[pool.submit(self.request,s) for _ in range(4)]
   for _ in range(100):
    if self.marker.exists():break
    time.sleep(.005)
   self.assertTrue(self.marker.exists());start=time.monotonic();s.server_close()
   self.assertEqual([f.result()[0] for f in futures],[503]*4);self.assertLess(time.monotonic()-start,.7)
  self.assertFalse(s.probe());time.sleep(.9);self.assertFalse(leaked.exists())
 def test_single_flight_late_waiter_uses_remaining_deadline(self):
  code=f"import pathlib,time;p=pathlib.Path({str(self.marker)!r});f=p.open('a');f.write('probe\\n');f.close();time.sleep(3)"
  s=self.server(code,timeout=.4,slots=1,single_flight=True)
  with concurrent.futures.ThreadPoolExecutor() as pool:
   first=pool.submit(self.request,s)
   for _ in range(100):
    if self.marker.exists():break
    time.sleep(.005)
   self.assertTrue(self.marker.exists());time.sleep(.2);start=time.monotonic()
   self.assertEqual(self.request(s)[0],503);self.assertLess(time.monotonic()-start,.3)
   self.assertEqual(first.result()[0],503)
  self.assertEqual(self.marker.read_text().splitlines(),['probe'])
 def test_single_flight_connection_limit_remains_bounded(self):
  s=self.server(slots=1,single_flight=True);connections=[]
  try:
   for _ in range(6):
    sock=socket.create_connection(s.server_address,timeout=2);sock.sendall(b'GET /.well-known/apex-ready HTTP/1.1\r\n');connections.append(sock)
   self.assertEqual(self.request(s),(503,'no-store',b''));self.assertFalse(self.marker.exists())
  finally:
   for sock in connections:sock.close()
 def test_single_flight_still_authenticates_before_probe(self):
  s=self.server(single_flight=True)
  for token,duplicate in [(None,False),('bad',False),('private-test-token',True)]:self.assertEqual(self.request(s,token=token,extra_token=duplicate)[0],403)
  self.assertFalse(self.marker.exists())
 def test_single_flight_requires_explicit_boolean(self):
  for value in [0,1,None,'true']:
   with self.subTest(value=value),self.assertRaises(ValueError):self.server(single_flight=value)
 def wait_status(self,s,status):
  deadline=time.monotonic()+2
  while time.monotonic()<deadline:
   if self.request(s)[0]==status:return
   time.sleep(.005)
  self.fail(f'Readiness did not become {status}')
 def test_background_probe_does_not_block_requests_or_repeat_per_request(self):
  release=self.root/'release'
  code=f"from pathlib import Path;import time;p=Path({str(self.marker)!r});p.write_text(p.read_text()+'x' if p.exists() else 'x');r=Path({str(release)!r});\nwhile not r.exists():time.sleep(.005)"
  s=self.server(code,timeout=1,refresh_interval=2,max_age=3)
  start=time.monotonic();self.assertEqual(self.request(s)[0],503);self.assertLess(time.monotonic()-start,.2)
  release.touch();self.wait_status(s,204)
  for _ in range(10):self.assertEqual(self.request(s),(204,'no-store',b''))
  self.assertEqual(self.marker.read_text(),'x')
  for token in [None,'bad']:self.assertEqual(self.request(s,token=token)[0],403)
 def test_background_failure_withdraws_success_and_recovers(self):
  healthy=self.root/'healthy';healthy.touch()
  code=f"from pathlib import Path;raise SystemExit(0 if Path({str(healthy)!r}).exists() else 1)"
  s=self.server(code,timeout=.2,refresh_interval=.1,max_age=.5)
  self.wait_status(s,204);healthy.unlink();self.wait_status(s,503)
  healthy.touch();self.wait_status(s,204)
 def test_background_stopped_refresher_expires_success(self):
  s=self.server('pass',timeout=.1,refresh_interval=.1,max_age=.25)
  self.wait_status(s,204);s.refresh_stop.set();s.refresh_thread.join(1)
  self.wait_status(s,503)
 def test_background_shutdown_kills_probe_and_descendant(self):
  leaked=self.root/'leaked'
  child=f"import pathlib,time;pathlib.Path({str(self.marker)!r}).touch();time.sleep(.7);pathlib.Path({str(leaked)!r}).touch()"
  s=self.server(f"import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',{child!r}]);time.sleep(5)",timeout=1,refresh_interval=.1,max_age=2)
  deadline=time.monotonic()+1
  while not self.marker.exists() and time.monotonic()<deadline:time.sleep(.005)
  self.assertTrue(self.marker.exists());s.server_close()
  self.assertFalse(s.probe());self.assertFalse(s.refresh_thread.is_alive());time.sleep(.8);self.assertFalse(leaked.exists())
 def test_background_configuration_rejects_unbounded_or_short_freshness(self):
  for options in [{'refresh_interval':True},{'refresh_interval':-1},{'refresh_interval':6},{'refresh_interval':float('nan')},{'max_age':11},{'max_age':True},{'max_age':float('inf')},{'refresh_interval':1,'max_age':1}]:
   with self.subTest(options=options),self.assertRaises(ValueError):self.server(**options)
if __name__=='__main__':unittest.main()
