"""R5 sources assembled on request; no build tool, CDN or Node is required at runtime."""
from pathlib import Path

def bundle(root: Path) -> bytes:
    src=root/'src'
    app=(src/'app.js').read_text('utf-8')
    app=app.replace('@@R5@@',(src/'r5.js').read_text('utf-8'))
    app=app.replace('@@SERVICE@@',(src/'service.js').read_text('utf-8'))
    return ((src/'engine.js').read_text('utf-8')+'\n'+app).encode('utf-8')
