"""Loopback-only gallery QA fixtures; never reads production config or data."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import time
from urllib.parse import parse_qs, urlsplit

STATIC = Path(__file__).resolve().parents[1] / 'src/webui_static'
MEMBER = {'name': '预览成员', 'display': '预览成员', 'group': 'hinatazaka',
          'msg_photos': 4, 'blog_photos': 0, 'total_photos': 4, 'count': 4}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        if path.startswith('/api/archive/media/'):
            self.reply(b'<svg xmlns="http://www.w3.org/2000/svg" width="480" height="640">'
                       b'<rect width="480" height="640" fill="#7294a7"/>'
                       b'<text x="60" y="300" fill="white" font-size="32">Gallery fixture</text></svg>', 'image/svg+xml')
            return
        if path.startswith('/api/'):
            result = {'ok': True}
            if path == '/api/auth/me':
                result.update(auth_enabled=True, user={'username': 'preview', 'role': 'user'})
            elif path.endswith('/gallery_years'):
                time.sleep(1.5)  # Deliberately slow metadata; photos should start first.
                result.update(years=[{'year': 2026, 'count': 4}], total=4)
            elif path.endswith('/members') or path.endswith('/gallery_members'):
                result.update(members=[MEMBER])
            elif path.endswith('/gallery'):
                qs = parse_qs(parsed.query)
                photos = [] if qs.get('page', ['1'])[0] != '1' else [
                    {'id': str(i), 'source': 'message', 'member_name': '预览成员',
                     'member_dir': '预览成员', 'published_at': '2026-10-18T01:00:00Z',
                     'url': f'/api/archive/media/fixture-{i}.svg', 'w': 480, 'h': 640}
                    for i in range(4)
                ]
                result.update(photos=photos, total=4, has_more=False)
            elif path.endswith('/blog_groups'):
                result.update(groups=[])
            self.reply(json.dumps(result, ensure_ascii=False).encode(), 'application/json')
            return
        rel = 'archive.html' if path in ('/', '/archive') else path.removeprefix('/static/')
        target = (STATIC / rel).resolve()
        if not target.is_relative_to(STATIC) or not target.is_file():
            self.send_error(404)
            return
        mime = {'.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css',
                '.svg': 'image/svg+xml'}.get(target.suffix, 'application/octet-stream')
        self.reply(target.read_bytes(), mime)

    def reply(self, data, mime):
        self.send_response(200)
        self.send_header('Content-Type', mime + ('; charset=utf-8' if mime.startswith(('text/', 'application/')) else ''))
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1', 18765), Handler).serve_forever()
