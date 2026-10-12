from pathlib import Path
from random import Random


def generate(out: str, seed: int):
    port = Random(f"port-in-use:{seed}").randint(20000, 40000)
    Path(out, "owner.py").write_text(
        f"""import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer

identity = "owner-" + secrets.token_hex(8)
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/identity":
            self.send_error(404)
            return
        with open("hits.log", "a", encoding="utf-8") as log:
            log.write(identity + "\\n")
        body = identity.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *_args):
        pass

server = HTTPServer(("127.0.0.1", {port}), Handler)
print("READY port " + str(server.server_port), flush=True)
server.serve_forever()
""",
        encoding="utf-8",
    )
    Path(out, "contender.py").write_text(
        f"""import socket
port = {port}
try:
    sock = socket.socket()
    sock.bind(("127.0.0.1", port))
except OSError:
    print("EADDRINUSE: address already in use :" + str(port))
    raise SystemExit(1)
else:
    sock.close()
    print("unexpectedly bound the port")
    raise SystemExit(2)
""",
        encoding="utf-8",
    )
    return {"params": {"port": port}}
