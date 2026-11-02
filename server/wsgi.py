"""Optional WSGI deployment: waitress-serve --call server.wsgi:create_application.

Install Waitress only on a connected build machine; this module itself has no
third-party dependency. Configure LOGBENCH_CONFIG and bootstrap users once with CLI.
"""
from http import HTTPStatus
from .app import Application, MAX_JSON, Response, compact
from .httpd import load_config, response_headers


def create_application():
    config, _ = load_config([])
    if config.trusted_proxies:
        raise ValueError('WSGI 模式请通过 WSGI 服务配置 trusted_proxy，并将 LogBench trusted_proxies 留空，避免二次解释代理地址。')
    return WSGI(Application(config))


class WSGI:
    def __init__(self, application):
        self.application = application

    def __call__(self, environ, start_response):
        headers = {key[5:].replace('_', '-').lower(): value for key, value in environ.items() if key.startswith('HTTP_')}
        headers['content-type'] = environ.get('CONTENT_TYPE', '')
        raw_length = environ.get('CONTENT_LENGTH') or '0'
        if not raw_length.isdigit() or int(raw_length) > MAX_JSON:
            response = Response(413, {'error': {'code': 'BODY_TOO_LARGE', 'message': '请求内容超过限制。', 'details': {}}})
        else:
            length = int(raw_length)
            body = environ['wsgi.input'].read(length) if length else b''
            target = environ.get('PATH_INFO', '/')
            if environ.get('QUERY_STRING'):
                target += '?' + environ['QUERY_STRING']
            if len(body) != length:
                response = Response(400, {'error': {'code': 'INCOMPLETE_BODY', 'message': '请求内容不完整。', 'details': {}}})
            else:
                # In WSGI deployments the configured WSGI server resolves trusted proxy
                # headers into REMOTE_ADDR; LogBench trusted_proxies is deliberately empty.
                response = self.application.handle(environ['REQUEST_METHOD'], target, headers, body, environ.get('REMOTE_ADDR', ''))
        output = compact(response.value).encode('utf-8') if response.value is not None and not response.stream else b''
        final = response_headers(response)
        final.setdefault('Content-Type', 'application/json; charset=utf-8')
        final['Content-Length'] = str(response.length if response.stream else len(output))
        start_response(str(response.status) + ' ' + HTTPStatus(response.status).phrase, list(final.items()))
        def stream():
            try:
                if environ['REQUEST_METHOD'] == 'HEAD':
                    return
                if response.stream:
                    remaining = response.length
                    while remaining:
                        chunk = response.stream.read(min(131072, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        yield chunk
                elif output:
                    yield output
            finally:
                response.close()
        return stream()
