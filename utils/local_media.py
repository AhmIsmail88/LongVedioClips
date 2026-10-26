"""Stable localhost media servers with byte ranges and benign disconnect handling."""
import http.server, threading, re
from pathlib import Path
from urllib.parse import quote

_servers={}
_lock=threading.Lock()

class MediaHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self,*args):pass
    def handle(self):
        try:super().handle()
        except (ConnectionResetError,BrokenPipeError,ConnectionAbortedError):pass
    def finish(self):
        try:super().finish()
        except (ConnectionResetError,BrokenPipeError,ConnectionAbortedError):pass
    def send_head(self):
        self._range=None
        range_header=self.headers.get("Range")
        if not range_header:return super().send_head()
        path=Path(self.translate_path(self.path))
        if not path.is_file():return super().send_head()
        size=path.stat().st_size
        match=re.fullmatch(r"bytes=(\d*)-(\d*)",range_header)
        if not match or not any(match.groups()):
            self.send_error(400,"Invalid byte range");return None
        first,last=match.groups()
        start=int(first) if first else max(0,size-int(last))
        end=min(size-1,int(last)) if first and last else size-1
        if start>=size or end<start:
            self.send_response(416);self.send_header("Content-Range",f"bytes */{size}")
            self.send_header("Content-Length","0");self.end_headers();return None
        source=path.open("rb");source.seek(start);self._range=(start,end)
        self.send_response(206)
        self.send_header("Content-Type",self.guess_type(str(path)))
        self.send_header("Accept-Ranges","bytes")
        self.send_header("Content-Range",f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length",str(end-start+1));self.end_headers()
        return source
    def copyfile(self,source,outputfile):
        if self._range is None:return super().copyfile(source,outputfile)
        remaining=self._range[1]-self._range[0]+1
        while remaining:
            chunk=source.read(min(65536,remaining))
            if not chunk:break
            outputfile.write(chunk);remaining-=len(chunk)

def port_for(directory):
    root=str(Path(directory).resolve())
    with _lock:
        if root not in _servers:
            from functools import partial
            server=http.server.ThreadingHTTPServer(("127.0.0.1",0),partial(MediaHandler,directory=root))
            server.daemon_threads=True
            _servers[root]=server
            threading.Thread(target=server.serve_forever,daemon=True).start()
        return _servers[root].server_port

def media_url(path):
    path=Path(path)
    return f"http://127.0.0.1:{port_for(path.parent)}/{quote(path.name)}"

def disconnect_handler(previous=None):
    def handler(loop,context):
        exc=context.get("exception")
        callback=getattr(context.get("handle"),"_callback",None)
        name=getattr(callback,"__qualname__","")
        if isinstance(exc,ConnectionResetError) and getattr(exc,"winerror",None)==10054 and name.endswith("_ProactorBasePipeTransport._call_connection_lost"):
            return
        if previous:previous(loop,context)
        else:loop.default_exception_handler(context)
    return handler
