from pathlib import Path
from random import Random


def generate(out: str, seed: int):
    rng = Random(f"dev-server-probe:{seed}")
    port = rng.randint(20000, 40000)
    script = f"""import time
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer

answer = "value-" + secrets.token_hex(8)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/value":
            self.send_error(404)
            return
        with open("hits.log", "a", encoding="utf-8") as log:
            log.write("GET /value " + answer + "\\n")
        body = answer.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *_args):
        pass

server = HTTPServer(("127.0.0.1", {port}), Handler)
time.sleep(1)
print("READY port " + str(server.server_port), flush=True)
server.serve_forever()
"""
    Path(out, "server.py").write_text(script, encoding="utf-8")
    return {"params": {"port": port}}
