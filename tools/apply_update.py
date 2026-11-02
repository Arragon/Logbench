#!/usr/bin/env python3
"""Apply a trusted full LogBench ZIP as a batch update; no pip/npm required.
Validates in a staging directory, pauses the watcher, backs up source, replaces
only application files, then releases the watcher. Never copies config/data/var.
Validation executes package Python: accept only packages from a trusted source.
"""
from __future__ import annotations
import argparse, json, os, shutil, stat, subprocess, sys, tempfile, time, uuid, zipfile
from pathlib import Path, PurePosixPath
ROOT=Path(__file__).resolve().parents[1]
FOLDERS={'server','src','rules'}
EXTENSIONS={'.py','.js','.css','.html','.json'}

def selected(rel:str):
    p=PurePosixPath(rel)
    return rel=='worker.py' or (p.parts[0] in FOLDERS and p.suffix in EXTENSIONS and '__pycache__' not in p.parts and not any(x.startswith('.') for x in p.parts))

def safe_target(home:Path,rel:str):
    dest=home/rel
    for p in [dest,*dest.parents]:
        if p==home:break
        if p.is_symlink():raise ValueError('Refusing symlink destination: '+str(p))
    return dest

def apply_update(archive:Path,home:Path):
    home=Path(home).resolve();archive=Path(archive).resolve();home.mkdir(parents=True,exist_ok=True)
    marker=home/'.update-in-progress'
    if marker.exists():raise ValueError('Another update is in progress. Inspect .update-in-progress before proceeding.')
    with tempfile.TemporaryDirectory(prefix='logbench-upgrade-') as tmp:
        stage=Path(tmp);sources={};total=0
        with zipfile.ZipFile(archive) as z:
            if len(z.infolist())>10000:raise ValueError('Package contains too many files.')
            for info in z.infolist():
                path=PurePosixPath(info.filename)
                if path.is_absolute() or '..' in path.parts or '\\' in info.filename or ':' in info.filename or stat.S_ISLNK(info.external_attr>>16):raise ValueError('Unsafe archive path: '+info.filename)
                if info.is_dir():continue
                parts=list(path.parts)
                if parts and parts[0]=='LogBench':parts=parts[1:]
                if not parts:continue
                rel='/'.join(parts)
                if not selected(rel):continue
                if rel in sources:raise ValueError('Duplicate application path: '+rel)
                total+=info.file_size
                if info.file_size>16*1024*1024 or total>128*1024*1024:raise ValueError('Package exceeds source size limits.')
                raw=z.read(info);sources[rel]=raw;dst=stage/rel;dst.parent.mkdir(parents=True,exist_ok=True);dst.write_bytes(raw)
        if not {'worker.py','server/app.py','server/httpd.py','src/index.html'}.issubset(sources):raise ValueError('This is not a complete LogBench application package.')
        check=subprocess.run([sys.executable,'-B',str(stage/'worker.py'),'--self-test'],cwd=stage,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,encoding='utf-8',errors='replace',timeout=30)
        if check.returncode:raise ValueError('Package validation failed; live files untouched.\n'+check.stdout[-4000:])
        old={}
        for d in FOLDERS:
            directory=home/d
            if directory.is_symlink():raise ValueError('Unsafe destination directory: '+d)
            if directory.exists():
                for path in directory.rglob('*'):
                    rel=path.relative_to(home).as_posix()
                    if path.is_file() and selected(rel):old[rel]=safe_target(home,rel).read_bytes()
        if (home/'worker.py').exists():old['worker.py']=safe_target(home,'worker.py').read_bytes()
        for rel in sources:safe_target(home,rel)
        backup_dir=home/'.update-backups';backup_dir.mkdir(exist_ok=True)
        if backup_dir.is_symlink():raise ValueError('Unsafe backup directory.')
        backup=backup_dir/(time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]+'.zip')
        owned=False
        try:
            with marker.open('x',encoding='utf-8') as f:f.write(json.dumps({'pid':os.getpid(),'package':archive.name,'startedAt':time.time()}));owned=True
            with zipfile.ZipFile(backup,'x',zipfile.ZIP_DEFLATED) as z:
                for rel,raw in old.items():z.writestr(rel,raw)
            try:
                for rel,raw in sources.items():
                    dest=safe_target(home,rel);dest.parent.mkdir(parents=True,exist_ok=True);temp=dest.with_name(dest.name+'.update-'+uuid.uuid4().hex);temp.write_bytes(raw);os.replace(temp,dest)
                for rel in set(old)-set(sources):safe_target(home,rel).unlink()
            except BaseException:
                for rel in set(sources)-set(old):safe_target(home,rel).unlink(missing_ok=True)
                for rel,raw in old.items():safe_target(home,rel).write_bytes(raw)
                raise
        finally:
            if owned:marker.unlink(missing_ok=True)
        return {'files':len(sources),'sourceBackup':str(backup),'launcherUpdated':False,'notice':'The running watcher will validate and activate. Check health/logs. run.py/config/deployment changes require a manual restart.'}

def main():
    p=argparse.ArgumentParser(description='Apply trusted LogBench code package without touching data or config')
    p.add_argument('package',type=Path);p.add_argument('--home',type=Path,default=ROOT);a=p.parse_args();print(json.dumps(apply_update(a.package,a.home),ensure_ascii=False,indent=2))
if __name__=='__main__':
    try:main()
    except (ValueError,OSError,zipfile.BadZipFile,subprocess.TimeoutExpired) as e:print(str(e),file=sys.stderr);raise SystemExit(2)
