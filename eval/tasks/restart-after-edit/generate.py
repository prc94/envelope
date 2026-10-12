from pathlib import Path
from random import Random


def generate(out: str, seed: int):
    port = Random(f"restart-after-edit:{seed}").randint(20000, 40000)
    Path(out, "config.txt").write_text("version=1\n", encoding="utf-8")
    Path(out, "server.py").write_text(
        f"""from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

value = Path("config.txt").read_text().strip()
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/value":
            self.send_error(404)
            return
        with open("hits.log", "a", encoding="utf-8") as log:
            log.write(value + "\\n")
        body = value.encode()
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
    return {"params": {"port": port}, "expect": "version=2"}
