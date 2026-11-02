#!/usr/bin/env python3
"""Consistent SQLite backup plus optional log library. Standard library only.
Stop writes before backing up database + trash + log files as a single logical set.
The SQLite file itself is a consistent online snapshot; surrounding files are not
transactional with that snapshot. Browser-only files require the UI backup action.
"""
from __future__ import annotations
import argparse, json, os, sqlite3, sys, tempfile, time, zipfile
from pathlib import Path
from contextlib import closing
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

def create_backup(home:Path,data_dir:Path,state_dir:Path,output:Path,*,include_data=False):
    home=Path(home).resolve();data_dir=Path(data_dir).resolve();state_dir=Path(state_dir).resolve();output=Path(output).resolve()
    if output.exists():raise ValueError('Backup destination already exists; not overwriting.')
    if data_dir in output.parents or state_dir in output.parents:raise ValueError('Store backups outside data_dir and state_dir.')
    database=state_dir/'logbench.sqlite3'
    if not database.is_file() or database.is_symlink():raise ValueError('Initialized SQLite database not found, or unsafe symlink.')
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='logbench-backup-') as d:
        snapshot=Path(d)/'logbench.sqlite3'
        # Connection context managers commit/rollback but do not close handles.
        # Explicitly close before TemporaryDirectory cleanup (also on Windows).
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as src, closing(sqlite3.connect(snapshot)) as dst:
            src.backup(dst)
        partial=output.with_name(output.name+'.partial')
        if partial.exists():raise ValueError('Backup staging file already exists.')
        try:
            with zipfile.ZipFile(partial,'x',zipfile.ZIP_DEFLATED) as archive:
                archive.write(snapshot,'state/logbench.sqlite3')
                if (home/'config.json').is_file():archive.write(home/'config.json','config.json')
                for directory,prefix in ([(data_dir,'data')] if include_data else [(data_dir/'.logbench-trash','data/.logbench-trash')]):
                    if not directory.exists():continue
                    for path in sorted(directory.rglob('*')):
                        if path.is_symlink():raise ValueError('Refusing symlink in backup: '+str(path))
                        if path.is_file():archive.write(path,prefix+'/'+path.relative_to(directory).as_posix())
                archive.writestr('manifest.json',json.dumps({'format':'logbench-server-backup-v1','createdAt':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'includesData':include_data,'browserFilesIncluded':False,'restore':'Stop service. Restore state/logbench.sqlite3 to state_dir; data (including .logbench-trash) to data_dir. Do not merge a live database WAL.'},ensure_ascii=False,indent=2))
            os.replace(partial,output)
        finally:
            if partial.exists():partial.unlink()
    return output

def main():
    parser=argparse.ArgumentParser(description='LogBench server backup (browser cache is separate)')
    parser.add_argument('--config');parser.add_argument('--include-data',action='store_true');parser.add_argument('--out',required=True)
    a=parser.parse_args()
    from server.httpd import load_config
    config,_=load_config(['--config',a.config] if a.config else [])
    print(create_backup(config.root,config.data_dir,config.state_dir,Path(a.out),include_data=a.include_data))
if __name__=='__main__':
    try:main()
    except (ValueError,OSError,sqlite3.Error) as e:print('Backup failed: '+str(e),file=sys.stderr);raise SystemExit(2)
