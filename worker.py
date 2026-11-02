"""Immutable release worker. Run via run.py for supervised hot updates."""
import sys
import tempfile
from pathlib import Path

def self_test():
    from server.app import Application, Config
    from server.r5_schema import validate
    from server.frontend import bundle
    import json
    code = Path(__file__).resolve().parent
    for path in [code/'worker.py', *sorted((code/'server').rglob('*.py'))]:
        compile(path.read_bytes(),str(path),'exec')
    validate(json.loads((code/'rules/cpqpsgx-default.json').read_text('utf-8')))
    for name in ('index.html','style.css','service.css','boot.js'):
        if not (code/'src'/name).is_file(): raise RuntimeError('missing '+name)
    content=bundle(code)
    if b'@@R5@@' in content or b'@@SERVICE@@' in content:raise RuntimeError('unresolved frontend placeholder')
    with tempfile.TemporaryDirectory(prefix='logbench-check-') as tmp:
        app=Application(Config(Path(tmp),code_root=code))
        for path in ('/','/assets/workbench.js','/healthz','/api/v1/me'):
            result=app.handle('GET',path,{'Host':'localhost'})
            try:
                if result.status!=200:raise RuntimeError('preflight failed: '+path)
            finally:result.close()
    return 0

def entry():
    if '--self-test' in sys.argv:return self_test()
    from server.httpd import main
    return main(sys.argv[1:])

if __name__=='__main__':
    raise SystemExit(entry())
