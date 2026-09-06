"""Bounded local fallback through the actual runtime and OS inference lock."""
import hashlib
import json
import socketserver
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from local_ai_stack import runtime as rt


def policy(*models):
    return {'schema_version':'local-ai-stack/fallback-v1','models':list(models)}


def transport(calls, models, generate=None):
    def request(base,method,path,payload,deadline):
        calls.append({'base':base,'method':method,'path':path,'payload':payload,'deadline':deadline})
        if path=='/api/version':return {'version':'synthetic'}
        if path=='/api/tags':return {'models':[{'name':m} for m in models]}
        return generate(payload) if generate else {'model':payload['model'],'done':True,'response':'synthetic answer','eval_count':2}
    return request


def test_first_absent_falls_back_to_exact_installed_model(tmp_path):
    calls=[]
    result=rt.run(base='http://localhost:11434',model='first:1',prompt='synthetic',
                  fallback=policy('first:1','second:1'),lock_path=tmp_path/'lock',
                  transport=transport(calls,['second:1']))
    assert result['schema_version']=='local-ai-stack/runtime-v2'
    assert result['status']=='completed' and result['model_observed']=='second:1'
    assert result['model_requested']=='first:1' and result['requested_model_installed'] is False
    assert result['attempts'][0]['status']=='not_installed'
    assert [c['payload']['model'] for c in calls if c['method']=='POST']==['second:1']
    assert len({c['deadline'] for c in calls})==1


def test_unknown_policy_refused_before_any_transport():
    with pytest.raises(ValueError,match='fallback'):
        rt.run(base='http://localhost:11434',model='first:1',prompt='synthetic',
               fallback={'schema_version':'unknown','models':['first:1']},
               transport=lambda *a:pytest.fail('invalid policy contacted runtime'))


def test_mismatched_response_aborts_without_next_model(tmp_path):
    calls=[]
    result=rt.run(base='http://localhost:11434',model='first:1',prompt='synthetic',
                  fallback=policy('first:1','second:1'),lock_path=tmp_path/'lock',
                  transport=transport(calls,['first:1','second:1'],lambda _:{'done':True,'model':'wrong:1','response':'wrong','eval_count':2}))
    assert result['status']=='blocked' and result['response'] is None
    assert result['attempts'][1]['status']=='not_attempted'
    assert len([c for c in calls if c['method']=='POST'])==1


@contextmanager
def http_fixture(monkeypatch, behavior, models=('first:1','second:1')):
    """Real HTTP parser/socket/body/timer; only port routing is fixture-local."""
    calls=[]
    class Server(ThreadingHTTPServer):
        daemon_threads=True
        def server_bind(self):
            socketserver.TCPServer.server_bind(self)
            self.server_name='private-fixture';self.server_port=self.socket.getsockname()[1]
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def do_GET(self):
            calls.append((self.command,self.path,None))
            self.respond(200,{'version':'synthetic-http'} if self.path=='/api/version' else {'models':[{'name':m} for m in models]})
        def do_POST(self):
            length=int(self.headers['Content-Length']);assert 0<length<40000
            payload=json.loads(self.rfile.read(length));calls.append((self.command,self.path,payload))
            behavior(self,payload)
        def respond(self,status,value,*,delay=0,extra_length=0,raw=None,extra_headers=()):
            body=json.dumps(value).encode() if raw is None else raw
            self.send_response(status);self.send_header('Content-Length',str(len(body)+extra_length))
            self.send_header('Connection','close')
            for key,value in extra_headers:self.send_header(key,value)
            self.end_headers()
            if delay:time.sleep(delay)
            try:self.wfile.write(body);self.wfile.flush()
            except (BrokenPipeError,ConnectionResetError):pass
            self.close_connection=True
    server=Server(('127.0.0.1',0),Handler)
    native_connection=rt.http.client.HTTPConnection
    def connection(host,port,*args,**kwargs):
        assert host=='127.0.0.1' and port==11434
        return native_connection('127.0.0.1',server.server_port,*args,**kwargs)
    monkeypatch.setattr(rt.http.client,'HTTPConnection',connection)
    worker=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);worker.start()
    try:yield calls
    finally:server.shutdown();server.server_close();worker.join(timeout=1)


def execute(tmp_path,**kwargs):
    return rt.run(base='http://localhost:11434',model='first:1',prompt='synthetic fixture prompt',
                  fallback=policy('first:1','second:1'),lock_path=tmp_path/'lock',**kwargs)


@pytest.mark.parametrize('status',[404,429,500,503])
def test_real_terminal_error_then_second_success(monkeypatch,tmp_path,status):
    def behavior(server,payload):
        if payload['model']=='first:1':server.respond(status,{'error':'private synthetic diagnostic'})
        else:server.respond(200,{'model':'second:1','done':True,'response':'local answer','eval_count':2})
    with http_fixture(monkeypatch,behavior) as calls:
        result=execute(tmp_path)
    assert result['status']=='completed' and result['model_observed']=='second:1'
    assert [a['status'] for a in result['attempts']]==['terminal_rejection','completed']
    assert result['attempts'][0]['http_status']==status
    assert 'private synthetic diagnostic' not in json.dumps(result)
    assert [c[1] for c in calls]==['/api/version','/api/tags','/api/generate','/api/generate']
    assert [c[2]['model'] for c in calls if c[0]=='POST']==['first:1','second:1']


@pytest.mark.parametrize('case',['wrong-model','truncated-error','truncated-success','malformed-error','empty-error','redirect','auth','ambiguous-framing'])
def test_real_uncertain_or_unauthorized_response_aborts(monkeypatch,tmp_path,case):
    def behavior(server,payload):
        if case=='wrong-model':server.respond(200,{'model':'other:1','done':True,'response':'no','eval_count':1})
        elif case=='truncated-error':server.respond(503,{'error':'synthetic'},extra_length=5)
        elif case=='truncated-success':server.respond(200,{'model':'first:1','done':True,'response':'incomplete framing','eval_count':1},extra_length=5)
        elif case=='malformed-error':server.respond(503,None,raw=b'{')
        elif case=='empty-error':server.respond(503,{'error':''})
        elif case=='redirect':server.respond(302,{'error':'synthetic'})
        elif case=='auth':server.respond(401,{'error':'synthetic'})
        else:server.respond(503,{'error':'synthetic'},extra_headers=[('Transfer-Encoding','chunked')])
    with http_fixture(monkeypatch,behavior) as calls:result=execute(tmp_path)
    assert result['status']=='blocked' and result['inference_completed'] is False and result['response'] is None
    assert len([c for c in calls if c[0]=='POST'])==1
    assert result['attempts'][1]['status']=='not_attempted'


def test_real_body_timeout_never_starts_another_inference(monkeypatch,tmp_path):
    with http_fixture(monkeypatch,lambda s,p:s.respond(503,{'error':'synthetic'},delay=.3)) as calls:
        result=execute(tmp_path,timeout=.08)
    assert result['status']=='timeout' and result['response'] is None
    assert len([c for c in calls if c[0]=='POST'])==1
    assert result['attempts'][1]['status']=='not_attempted'
    assert result['elapsed_s']<.4


def test_real_closed_connection_never_retries(monkeypatch,tmp_path):
    def behavior(server,payload):server.close_connection=True
    with http_fixture(monkeypatch,behavior) as calls:result=execute(tmp_path)
    assert result['status']=='blocked' and result['response'] is None
    assert len([c for c in calls if c[0]=='POST'])==1


def test_real_first_absent_and_busy_native_lock(monkeypatch,tmp_path):
    from local_ai_stack.coordination import ollama_inference_lock
    with http_fixture(monkeypatch,lambda *a:pytest.fail('busy lock made POST'),models=['second:1']) as calls:
        with ollama_inference_lock(lock_path=tmp_path/'lock'):
            result=execute(tmp_path,timeout=.04)
    assert result['status']=='blocked' and result['error_code']=='OllamaBusy'
    assert result['attempts'][0]['status']=='not_installed'
    assert result['attempts'][1]['generation_attempted'] is False
    assert [c[0] for c in calls]==['GET','GET']


@pytest.mark.parametrize('bad',[
    {},{'schema_version':'unknown','models':['first:1']},
    {'schema_version':'local-ai-stack/fallback-v1','models':[]},
    {'schema_version':'local-ai-stack/fallback-v1','models':['first:1']*2},
    {'schema_version':'local-ai-stack/fallback-v1','models':['first:1','a','b','c','d']},
    {'schema_version':'local-ai-stack/fallback-v1','models':['second:1','first:1']},
    {'schema_version':'local-ai-stack/fallback-v1','models':['first:1',True]},
    {'schema_version':'local-ai-stack/fallback-v1','models':['first:1','space tag']},
    {'schema_version':'local-ai-stack/fallback-v1','models':['first:1'],'endpoint':'http://outside.invalid'},
])
def test_policy_ambiguity_refused_before_runtime(bad,tmp_path):
    with pytest.raises(ValueError,match='fallback'):
        rt.run(base='http://localhost:11434',model='first:1',prompt='synthetic',fallback=bad,
               transport=lambda *a:pytest.fail('invalid policy used transport'))


def test_duplicate_policy_json_and_limits(tmp_path):
    from local_ai_stack.fallback import load_policy
    path=tmp_path/'policy.json'
    for raw in ['{"models":[],"models":[]}','{"x":NaN}','['*1500+']'*1500,' '*8193]:
        path.write_text(raw)
        with pytest.raises(ValueError,match='fallback'):load_policy(path)


def test_remaining_budget_is_not_refreshed_for_next_model(monkeypatch,tmp_path):
    clock=[100.0];monkeypatch.setattr(rt.time,'monotonic',lambda:clock[0]);calls=[]
    def generate(_):
        clock[0]+=3
        raise rt.TerminalHTTPRejection(503)
    result=execute(tmp_path,timeout=2,transport=transport(calls,['first:1','second:1'],generate))
    assert result['status']=='timeout' and result['response'] is None
    assert len([c for c in calls if c['method']=='POST'])==1
    assert result['attempts'][0]['status']=='terminal_rejection'
    assert result['attempts'][1]['status']=='timeout' and not result['attempts'][1]['generation_attempted']
    assert len({c['deadline'] for c in calls})==1


def test_one_native_lock_spans_both_generations(monkeypatch,tmp_path):
    calls=[];contexts=[];native=rt.ollama_inference_lock
    @contextmanager
    def observed_lock(**kwargs):
        with native(**kwargs):
            contexts.append('acquired')
            try:yield
            finally:contexts.append('released')
    monkeypatch.setattr(rt,'ollama_inference_lock',observed_lock)
    def generate(payload):
        assert contexts==['acquired']
        if payload['model']=='first:1':raise rt.TerminalHTTPRejection(503)
        return {'model':'second:1','done':True,'response':'synthetic','eval_count':1}
    result=execute(tmp_path,transport=transport(calls,['first:1','second:1'],generate))
    assert result['status']=='completed' and contexts==['acquired','released']


def test_doctor_rejects_policy_without_any_runtime_call():
    with pytest.raises(ValueError,match='fallback'):
        rt.run(base='http://localhost:11434',model='first:1',fallback=policy('first:1'),transport=lambda *a:pytest.fail('doctor fallback called runtime'))


def test_real_other_process_lock_prevents_all_generation(monkeypatch,tmp_path):
    module_path=rt.__file__
    source=Path(module_path.split('.whl')[0]+'.whl') if '.whl' in module_path else Path(module_path).parents[1]
    helper=Path(__file__).with_name('fallback_lock_child.py')
    worker=subprocess.Popen([sys.executable,'-I','-S','-B',str(helper),str(source),str(tmp_path)],
                            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        deadline=time.monotonic()+4
        while not (tmp_path/'ready').exists():
            if worker.poll() is not None or time.monotonic()>deadline:
                out,err=worker.communicate(timeout=1);pytest.fail('private lock holder failed: '+out+err)
            time.sleep(.01)
        with http_fixture(monkeypatch,lambda *a:pytest.fail('external lock permitted POST'),models=['second:1']) as calls:
            result=execute(tmp_path,timeout=.08)
        assert result['error_code']=='OllamaBusy' and result['status']=='blocked'
        assert result['attempts'][0]['status']=='not_installed'
        assert all(c[0]=='GET' for c in calls)
    finally:
        (tmp_path/'release').write_text('release',encoding='utf-8')
        try:out,err=worker.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            worker.terminate();out,err=worker.communicate(timeout=2)
        assert worker.returncode==0,(out,err)
    proof=json.loads((tmp_path/'child-result.json').read_text())
    assert proof['native_lock_entered'] and not proof['blocked']


def test_both_terminal_rejections_keep_all_attempt_errors(monkeypatch,tmp_path):
    with http_fixture(monkeypatch,lambda s,p:s.respond(503,{'error':'synthetic'})) as calls:
        result=execute(tmp_path)
    assert result['status']=='blocked' and result['response'] is None
    assert [a['status'] for a in result['attempts']]==['terminal_rejection','terminal_rejection']
    assert all(a['http_status']==503 for a in result['attempts'])
    assert len(calls)==4


def test_policy_is_owned_and_linked_to_receipt(tmp_path):
    value=policy('first:1','second:1');calls=[];native=transport(calls,['second:1'])
    def request(*args):
        value['models'].append('injected:1')
        return native(*args)
    result=rt.run(base='http://localhost:11434',model='first:1',prompt='synthetic',fallback=value,
                  lock_path=tmp_path/'lock',transport=request)
    assert result['policy']['models']==['first:1','second:1']
    expected=hashlib.sha256(json.dumps(result['policy'],sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
    assert result['policy_sha256']==expected and result['status']=='completed'
    unsigned={k:v for k,v in result.items() if k!='evidence_sha256'}
    assert result['evidence_sha256']==hashlib.sha256(json.dumps(unsigned,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def test_public_cli_executes_fallback_and_missing_policy_is_refusal(monkeypatch,tmp_path,capsys):
    from local_ai_stack.cli import main
    file=tmp_path/'policy.json';file.write_text(json.dumps(policy('first:1','second:1')))
    def behavior(server,payload):
        if payload['model']=='first:1':server.respond(503,{'error':'synthetic'})
        else:server.respond(200,{'done':True,'model':'second:1','response':'synthetic','eval_count':1})
    with http_fixture(monkeypatch,behavior):
        assert main(['infer','--model','first:1','--prompt','synthetic','--fallback',str(file),'--lock',str(tmp_path/'lock')])==0
    result=json.loads(capsys.readouterr().out)
    assert result['status']=='completed' and result['model_observed']=='second:1'
    assert main(['infer','--model','first:1','--prompt','synthetic','--fallback',str(tmp_path/'missing.json')])==2
    assert json.loads(capsys.readouterr().out)['status']=='blocked'


def retained(project,package,filename='core.py'):
    import importlib.util
    path=Path(__file__).resolve().parents[1]/'packages'/project/'src'/package/filename
    spec=importlib.util.spec_from_file_location('retained_'+package,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


@pytest.mark.parametrize('mask',range(8))
def test_order_and_availability_match_actual_declared_routing_source(tmp_path,mask):
    router=retained('model-fallback-proxy','model_fallback_proxy','__init__.py')
    fleet=retained('ollama-fleet-manager','ollama_fleet_manager')
    models=['first','second','third'];available=[m for i,m in enumerate(models) if mask&(1<<i)]
    # The source handles supplied declarations only. Its quota/capability fields
    # are neutral fixed fixture values, never claimed as measured by runtime.
    route=router.route({'request':{'context_tokens':0,'capabilities':[]},'models':[
        {'name':m,'order':i,'status':'healthy' if m in available else 'down',
         'remaining_requests':1,'context_limit':1,'capabilities':[]} for i,m in enumerate(models)]})
    result=rt.run(base='http://localhost:11434',model='first',prompt='synthetic',
                  fallback=policy(*models),lock_path=tmp_path/'lock',transport=transport([],available))
    assert result['model_observed']==route['selected']
    assert (result['status']=='completed')==route['ok']
    health=fleet.evaluate({'model':'synthetic','node_count':len(models),'ready_nodes':len(available)})
    assert (health['status']=='passed')==(mask==7)
    if 0<mask<7:assert result['status']=='completed' and health['status']=='failed'


def test_all_absent_real_inventory_never_creates_lock(monkeypatch,tmp_path):
    with http_fixture(monkeypatch,lambda *a:pytest.fail('absent model generated'),models=[]) as calls:
        result=execute(tmp_path)
    assert result['status']=='blocked' and not result['inference_completed']
    assert all(a['status']=='not_installed' for a in result['attempts'])
    assert len(calls)==2 and not (tmp_path/'lock').exists()


def test_four_candidates_only_stop_on_first_completed_response(tmp_path):
    models=['first:1','second:1','third:1','fourth:1'];calls=[]
    def generate(payload):
        if payload['model']!='fourth:1':raise rt.TerminalHTTPRejection(503)
        return {'model':'fourth:1','done':True,'response':'synthetic','eval_count':1}
    result=rt.run(base='http://localhost:11434',model='first:1',prompt='synthetic',
                  fallback=policy(*models),lock_path=tmp_path/'lock',transport=transport(calls,models,generate))
    assert result['status']=='completed' and result['model_observed']=='fourth:1'
    assert [c['payload']['model'] for c in calls if c['method']=='POST']==models
    assert len({c['deadline'] for c in calls})==1


def test_lock_release_error_never_leaves_global_completion(monkeypatch,tmp_path):
    native=rt.ollama_inference_lock
    @contextmanager
    def failing_release(**kwargs):
        with native(**kwargs):yield
        raise OSError('synthetic release error')
    monkeypatch.setattr(rt,'ollama_inference_lock',failing_release)
    result=execute(tmp_path,transport=transport([],['first:1']))
    assert result['status']=='blocked' and result['response'] is None and result['inference_completed'] is False
    assert result['attempts'][0]['status']=='completed'


def test_v1_truncated_response_never_completes(monkeypatch,tmp_path):
    def behavior(server,payload):
        server.respond(200,{'model':'first:1','done':True,'response':'synthetic truncated','eval_count':1},extra_length=5)
    with http_fixture(monkeypatch,behavior) as calls:
        result=rt.run(base='http://localhost:11434',model='first:1',prompt='synthetic',lock_path=tmp_path/'lock')
    assert result['schema_version']=='local-ai-stack/runtime-v1'
    assert result['status']=='blocked' and result['response'] is None and result['inference_completed'] is False
    assert len([c for c in calls if c[0]=='POST'])==1
