"""Standalone synthetic lock holder; no application import before its guard."""
from pathlib import Path
import hashlib,json,os,sys,time

assert sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode
source=Path(sys.argv[1]).resolve();root=Path(sys.argv[2]).resolve()
assert root.is_dir() and source.exists()
runtime=Path(sys.base_prefix).resolve();blocked=[]
os.environ.clear();os.environ.update(SystemRoot='C:\\Windows',WINDIR='C:\\Windows',HOME=str(root),LOCAL_AI_STATE_ROOT=str(root))
def inside(value,roots):
    if isinstance(value,int):return True
    path=Path(os.fsdecode(value)).resolve()
    return any(path==r or path.is_relative_to(r) for r in roots)
def deny(event):blocked.append(event);raise PermissionError('private child boundary: '+event)
def audit(event,values):
    if event=='open':
        path,mode,flags=values
        if isinstance(path,int):return
        writing=(isinstance(mode,str) and any(c in mode for c in 'wax+')) or flags&(os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND)
        if not inside(path,[root] if writing else [root,source,runtime,Path(__file__).resolve()]):deny(event)
    elif event in {'socket.connect','socket.bind','socket.getaddrinfo','subprocess.Popen','os.system','os.chown'}:deny(event)
    elif event in {'os.mkdir','os.remove','os.rmdir','os.chmod'}:
        if not inside(values[0],[root]):deny(event)
    elif event in {'os.rename','os.link','os.symlink'}:
        if not all(inside(v,[root]) for v in values[:2]):deny(event)
sys.addaudithook(audit);sys.path.insert(0,str(source))
from local_ai_stack.coordination import ollama_inference_lock
entered=False
with ollama_inference_lock(lock_path=root/'lock',timeout_s=1):
    entered=True;(root/'ready').write_text('held',encoding='utf-8')
    deadline=time.monotonic()+5
    while not (root/'release').exists():
        if time.monotonic()>deadline:raise TimeoutError('fixture release absent')
        time.sleep(.01)
(root/'child-result.json').write_text(json.dumps({'native_lock_entered':entered,'blocked':blocked,'guard_before_import':True,'python':sys.version}),encoding='utf-8')
raise SystemExit(bool(blocked))
