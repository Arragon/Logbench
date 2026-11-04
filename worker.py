"""Immutable release worker. Run via run.py for supervised hot updates."""
import sys
import tempfile
from pathlib import Path

def self_test():
    from server.app import Application, Config
    from server.r5_schema import validate
    from server.frontend import bundle, worker_bundle, worker_names, WORKER_SYMBOLS
    from server.capabilities import collect as collect_capabilities
    import json
    code = Path(__file__).resolve().parent
    for path in [code/'worker.py', *sorted((code/'server').rglob('*.py'))]:
        compile(path.read_bytes(),str(path),'exec')
    validate(json.loads((code/'rules/cpqpsgx-default.json').read_text('utf-8')))
    for name in ('index.html','style.css','service.css','boot.js'):
        if not (code/'src'/name).is_file(): raise RuntimeError('missing '+name)
    content=bundle(code)
    if b'@@R5@@' in content or b'@@SERVICE@@' in content:raise RuntimeError('unresolved frontend placeholder')
    # Worker bundles must assemble from the shipped files, not from a dev tree.
    for name in worker_names():
        worker=worker_bundle(code,name)
        for symbol in WORKER_SYMBOLS.get(name, []):
            if symbol.encode() not in worker:
                raise RuntimeError('worker %s is missing %s' % (name, symbol))
        if len(worker) < 1500:
            raise RuntimeError('worker %s assembled to only %d bytes' % (name, len(worker)))
    with tempfile.TemporaryDirectory(prefix='logbench-check-') as tmp:
        capabilities = collect_capabilities(Path(tmp))
        if capabilities['features']['local_reading']['state'] != 'enabled':
            raise RuntimeError('local reading must never be disabled by a missing tool')
        json.dumps(capabilities)
        app=Application(Config(Path(tmp),code_root=code))
        paths = ['/', '/assets/workbench.js', '/healthz', '/api/v1/me',
                 '/r6', '/assets/r6.js', '/assets/r6/app.js', '/assets/r6/r6.css']
        paths += ['/assets/workers/%s.js' % name for name in worker_names()]
        for path in paths:
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
