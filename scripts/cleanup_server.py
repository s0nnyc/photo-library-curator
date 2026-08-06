"""Serve a local cleanup approval page and execute only browser-approved moves."""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from apply_cleanup import apply_actions
from build_ai_cleanup import actions_for_groups


def serve(manifest_path: Path, directory: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, directory=str(directory), **kwargs)

        def send_json(self, status: HTTPStatus, payload: object) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def do_POST(self) -> None:
            if self.path != "/api/approve-cleanup":
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."}); return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 65536: raise ValueError("Invalid request size.")
                choices = json.loads(self.rfile.read(size)).get("choices", {})
                if not isinstance(choices, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in choices.items()):
                    raise ValueError("Invalid review choices.")
                plan = json.loads(manifest_path.read_text(encoding="utf-8"))
                actions = actions_for_groups(plan["groups"], choices)
                source = Path(plan["source_root"]).resolve(strict=True)
                recovery = source.parent / f"{source.name}-photo-curator-recovery"
                run_root = apply_actions(plan, actions, recovery, execute=True)
                self.send_json(HTTPStatus.OK, {"moved": len(actions), "recovery_root": str(run_root)})
            except (ValueError, FileNotFoundError, OSError, json.JSONDecodeError) as error:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})

    if not directory.joinpath("index.html").is_file():
        raise ValueError("Approval page is missing.")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Open http://{host}:{port} to approve or adjust the cleanup.")
    try: server.serve_forever()
    except KeyboardInterrupt: print("\nCleanup approval server stopped.")
    finally: server.server_close()


def main() -> None:
    project = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=project / "data" / "ai_cleanup_manifest.json")
    parser.add_argument("--directory", type=Path, default=project / "reports" / "cleanup_approval")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    serve(args.manifest, args.directory, port=args.port)


if __name__ == "__main__": main()
