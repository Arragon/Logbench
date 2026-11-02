#!/usr/bin/env python3
"""LogBench launcher. Atomic snapshots + validated rolling process replacement.

Only application files are watched. data/, var/, config.json and this launcher are
not replaced or reloaded in-process. Code updates have a short bounded restart gap.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

ROOT=Path(__file__).resolve().parent
SOURCE_DIRS=('server','src','rules')
EXTENSIONS={'.py','.js','.css','.html','.json'}


def source_snapshot(root):
    paths=[root/'worker.py']
    for dirname in SOURCE_DIRS:
        directory=root/dirname
        if not directory.is_dir():raise ValueError('Missing application directory: '+dirname)
        paths.extend(p for p in directory.rglob('*') if p.is_file() and p.suffix in EXTENSIONS and '__pycache__' not in p.parts and not any(x.startswith('.') for x in p.relative_to(root).parts))
    data={};digest=hashlib.sha256()
    for path in sorted(paths):
        if path.is_symlink():raise ValueError('Application symlinks are not accepted: '+str(path))
        raw=path.read_bytes()
        if len(raw)>16*1024*1024:raise ValueError('Application source file too large: '+str(path))
        rel=path.relative_to(root).as_posix();data[rel]=raw
        digest.update(rel.encode());digest.update(b'\0');digest.update(hashlib.sha256(raw).digest())
    return digest.hexdigest()[:20],data


def make_release(runtime, ident, files):
    target=runtime/'releases'/ident
    if target.is_dir():return target
    stage=runtime/'releases'/(ident+'.partial')
    if stage.exists():shutil.rmtree(stage)
    stage.mkdir(parents=True)
    try:
        for rel,raw in files.items():
            path=stage/rel;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
        os.replace(stage,target)
    except BaseException:
        shutil.rmtree(stage,ignore_errors=True);raise
    return target


def environment(root,ident):
    env=os.environ.copy();env.update(LOGBENCH_HOME=str(root),LOGBENCH_RELEASE=ident,LOGBENCH_SUPERVISED='1',PYTHONDONTWRITEBYTECODE='1',PYTHONUNBUFFERED='1')
    return env


def preflight(release, root):
    env=environment(root,release.name)
    try:
        result=subprocess.run([sys.executable,'-B',str(release/'worker.py'),'--self-test'],cwd=release,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=20,text=True,encoding='utf-8',errors='replace')
        return result.returncode==0,result.stdout[-6000:]
    except (OSError,subprocess.TimeoutExpired) as exc:return False,str(exc)


def launch(release,args,root):
    kw={'start_new_session':True} if os.name!='nt' else {'creationflags':subprocess.CREATE_NEW_PROCESS_GROUP}
    return subprocess.Popen([sys.executable,'-B',str(release/'worker.py'),*args],cwd=root,env=environment(root,release.name),stdin=subprocess.PIPE,text=True,encoding='utf-8',**kw)


def stop_child(child,timeout=30):
    if child is None or child.poll() is not None:return
    try:child.stdin.write('stop\n');child.stdin.flush()
    except (OSError,BrokenPipeError,ValueError):
        try:child.terminate()
        except OSError:pass
    try:child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        print('[update] graceful stop timed out; terminating worker',flush=True)
        child.kill();child.wait(5)
    try:child.stdin.close()
    except Exception:pass


def healthy(child,config,ident,timeout=10):
    bind=config.host
    connect='127.0.0.1' if bind in ('0.0.0.0','') else '::1' if bind=='::' else bind
    target='['+connect+']' if ':' in connect else connect
    allowed=config.allowed_hosts[0];host='['+allowed+']' if ':' in allowed else allowed
    url=f'http://{target}:{config.port}/healthz'
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if child.poll() is not None:return False
        try:
            with opener.open(urllib.request.Request(url,headers={'Host':host+':'+str(config.port)}),timeout=.5) as response:
                payload=json.load(response)
                if payload.get('release')==ident and payload.get('pid')==child.pid:return True
        except Exception:pass
        time.sleep(.12)
    return False


class InstanceLock:
    def __init__(self,path):
        path.parent.mkdir(parents=True,exist_ok=True)
        self.file=path.open('a+b');self.file.seek(0)
        if os.name=='nt':
            import msvcrt
            self.file.write(b' ');self.file.flush();self.file.seek(0)
            msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
    def close(self):self.file.close()


def without_bootstrap(args):
    out=[];skip=False
    for arg in args:
        if skip:skip=False;continue
        if arg=='--admin-ip':skip=True;continue
        if arg.startswith('--admin-ip=') or arg=='--local-identity':continue
        out.append(arg)
    return out


def supervise(args,config,settle,interval):
    runtime=config.state_dir/'runtime';runtime.mkdir(parents=True,exist_ok=True)
    try:lock=InstanceLock(runtime/'supervisor.lock')
    except OSError:
        print('Another LogBench launcher holds this state directory.',file=sys.stderr);return 2
    flags={'stop':False}
    def mark(*_):flags['stop']=True
    for sig in (signal.SIGINT,signal.SIGTERM):
        try:signal.signal(sig,mark)
        except (OSError,ValueError):pass
    child=None;good=None;seen=None;pending=None;failed=set();live_args=list(args);last_crash=0;crashes=0
    try:
        ident,files=source_snapshot(ROOT);release=make_release(runtime,ident,files);ok,report=preflight(release,ROOT)
        if not ok:print('[startup] validation failed\n'+report,file=sys.stderr);return 2
        child=launch(release,live_args,ROOT)
        if not healthy(child,config,ident):stop_child(child);print('[startup] worker failed health check',file=sys.stderr);return 2
        good=release;seen=ident;live_args=without_bootstrap(live_args)
        print(f'[ready] http://{config.host}:{config.port} | release {ident}',flush=True)
        print('[update] watching server/, src/, rules/, worker.py; state and data are excluded',flush=True)
        (runtime/'current.json').write_text(json.dumps({'release':ident,'pid':child.pid}),encoding='utf-8')
        while not flags['stop']:
            if child.poll() is not None:
                if time.monotonic()-last_crash>30:crashes=0
                last_crash=time.monotonic();crashes+=1
                if crashes>3:print('[runtime] repeated crashes; stopping',file=sys.stderr);return 3
                print('[runtime] restarting the last healthy release',flush=True);child=launch(good,live_args,ROOT)
                if not healthy(child,config,good.name):return 3
            if (ROOT/'.update-in-progress').exists():pending=None;time.sleep(interval);continue
            try:ident,files=source_snapshot(ROOT)
            except (OSError,ValueError):time.sleep(interval);continue
            now=time.monotonic()
            if ident==seen or ident in failed:pending=None
            elif pending is None or pending[0]!=ident:pending=(ident,now)
            elif now-pending[1]>=settle:
                candidate=make_release(runtime,ident,files)
                ok,report=preflight(candidate,ROOT)
                # Refuse a stale snapshot when files changed again while validating.
                try:still=source_snapshot(ROOT)[0]==ident and not (ROOT/'.update-in-progress').exists()
                except (OSError,ValueError):still=False
                if not still:pending=None;continue
                if not ok:
                    failed.add(ident);print('[update] rejected; old worker retained\n'+report,flush=True)
                else:
                    print('[update] activating '+ident,flush=True);stop_child(child)
                    if flags['stop']:break
                    new=launch(candidate,live_args,ROOT)
                    if healthy(new,config,ident):
                        child=new;good=candidate;seen=ident
                        (runtime/'current.json').write_text(json.dumps({'release':ident,'pid':child.pid}),encoding='utf-8')
                        print('[update] active '+ident,flush=True)
                    else:
                        stop_child(new);failed.add(ident)
                        print('[update] startup failed; rolling back to '+good.name,flush=True)
                        child=launch(good,live_args,ROOT)
                        if not healthy(child,config,good.name):return 4
                pending=None
            time.sleep(interval)
        return 0
    finally:
        stop_child(child);lock.close()


def main(argv=None):
    if sys.version_info < (3,9,11):
        print('LogBench requires Python 3.9.11 or newer.',file=sys.stderr);return 2
    argv=list(sys.argv[1:] if argv is None else argv)
    parser=argparse.ArgumentParser(add_help=False)
    parser.add_argument('--no-reload',action='store_true')
    parser.add_argument('--settle',type=float,default=1.5)
    parser.add_argument('--poll',type=float,default=.5)
    options,args=parser.parse_known_args(argv)
    from server.httpd import load_config,main as direct
    if '--help' in args or '-h' in args:
        print('Launcher options: --no-reload (disable updates), --settle SECONDS (default 1.5), --poll SECONDS (default .5).')
    config,_=load_config(args)
    if options.no_reload or '--init-only' in args:return direct(args)
    if options.settle<.2 or options.poll<.1:raise ValueError('settle >= .2 and poll >= .1 required')
    return supervise(args,config,options.settle,options.poll)

if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,OSError) as exc:
        print('LogBench: '+str(exc),file=sys.stderr);raise SystemExit(2)
