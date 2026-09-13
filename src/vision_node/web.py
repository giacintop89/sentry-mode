"""Read-only local hardware dashboard, explicitly enabled through `serve`."""

import json
import logging
import signal
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from vision_node.config import Settings
from vision_node.hardware.status import inspect_hardware

PAGE = b"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vision Node</title><style>
body{background:#101b22;color:#edf5f5;font:17px system-ui;margin:0;padding:8vw}
main{max-width:850px;margin:auto}h1{font-size:48px;margin-bottom:8px}
p{color:#a8bdc7}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
gap:16px;margin:40px 0}.card{padding:24px;background:#1a2c37;border-radius:12px}
.card strong{display:block;margin-top:16px;color:#f2bf74}.ok strong{color:#76dab1}
small{color:#a8bdc7}button{background:#76dab1;border:0;padding:12px 20px;border-radius:8px}
</style><main><small>LOCAL HARDWARE RUNTIME / PHASE 0</small><h1>Vision Node</h1>
<p>Camera, audio and network readiness for your physical AI node.</p>
<div id="status" class="grid" aria-live="polite">Checking hardware...</div>
<button onclick="refresh()">Refresh status</button><p id="updated"></p>
<small>Network indicates a local IPv4 route. Media stays on this device.</small></main>
<script>async function refresh(){try{const r=await fetch('/api/status');
if(!r.ok)throw Error('Status unavailable');const s=await r.json();
const root=document.getElementById('status');root.replaceChildren();
for(const n of ['camera','microphone','speaker','network']){
const card=document.createElement('div');const ok=s[n+'_available'];
card.className='card'+(ok?' ok':'');card.textContent=n[0].toUpperCase()+n.slice(1);
const value=document.createElement('strong');value.textContent=ok?'OK':'Unavailable';
card.append(value);root.append(card);}
document.getElementById('updated').textContent='Last checked '+new Date().toLocaleTimeString();
}catch(e){document.getElementById('updated').textContent=e.message;}}
refresh();setInterval(refresh,15000);</script></html>"""


def serve(config: Settings, host: str = "0.0.0.0", port: int = 8083) -> None:
    lock = threading.Lock()
    cached = None
    checked = 0.0

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal cached, checked
            if self.path == "/":
                content, content_type = PAGE, "text/html; charset=utf-8"
            elif self.path == "/api/status":
                with lock:
                    if cached is None or time.monotonic() - checked > 10:
                        cached = asdict(inspect_hardware(config))
                        checked = time.monotonic()
                    content = json.dumps(cached).encode()
                content_type = "application/json"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, format, *args):
            logging.getLogger(__name__).info(format, *args)

    stopped = threading.Event()
    previous = {}

    def stop(signum, frame):
        stopped.set()

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, stop)
        with ThreadingHTTPServer((host, port), Handler) as server:
            server.timeout = 0.5
            logging.getLogger(__name__).info("Serving Vision Node on %s:%s", host, port)
            while not stopped.is_set():
                server.handle_request()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        logging.shutdown()
