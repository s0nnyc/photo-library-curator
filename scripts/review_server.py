"""Serve the local visual review page and save human decisions to SQLite.

This server binds to localhost by default.  It never reads or writes the source
photo library; its only write is the project's catalogue database.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ALLOWED_DECISIONS = {
    "keep_all",
    "likely_duplicate",
    "delete_all_but_one",
    "delete_group",
    "ignore",
    "needs_repair",
    "review_later",
}
GROUP_ID = re.compile(r"^[0-9a-f]{16}$")
SCHEMA = """
CREATE TABLE IF NOT EXISTS visual_review_decisions (
    scan_id INTEGER NOT NULL,
    group_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('keep_all', 'likely_duplicate', 'delete_all_but_one', 'delete_group', 'ignore', 'needs_repair', 'review_later')),
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scan_id, group_id)
);

CREATE TABLE IF NOT EXISTS visual_review_confirmations (
    scan_id INTEGER PRIMARY KEY,
    group_count INTEGER NOT NULL,
    confirmed_at TEXT NOT NULL
);
"""


def initialise_database(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        existing = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'visual_review_decisions'"
        ).fetchone()
        if existing and "delete_group" not in existing[0]:
            # SQLite cannot add a value to a CHECK constraint. Preserve prior
            # review decisions while rebuilding this project-owned table.
            connection.execute("ALTER TABLE visual_review_decisions RENAME TO visual_review_decisions_previous")
            connection.executescript(SCHEMA)
            connection.execute(
                """
                INSERT INTO visual_review_decisions (scan_id, group_id, decision, note, updated_at)
                SELECT scan_id, group_id, decision, note, updated_at FROM visual_review_decisions_previous
                """
            )
            connection.execute("DROP TABLE visual_review_decisions_previous")
        connection.executescript(SCHEMA)


def save_decision(database: Path, scan_id: int, group_id: str, decision: str, note: str) -> dict[str, object]:
    """Persist or clear one review-only group decision."""
    if not GROUP_ID.fullmatch(group_id):
        raise ValueError("Invalid group identifier.")
    if len(note) > 1000:
        raise ValueError("Notes may be at most 1,000 characters.")
    with sqlite3.connect(database) as connection:
        # Any saved edit makes a previous review confirmation stale.
        connection.execute("DELETE FROM visual_review_confirmations WHERE scan_id = ?", [scan_id])
        if not decision:
            connection.execute(
                "DELETE FROM visual_review_decisions WHERE scan_id = ? AND group_id = ?", [scan_id, group_id]
            )
            return {"deleted": True}
        if decision not in ALLOWED_DECISIONS:
            raise ValueError("Invalid decision.")
        updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        connection.execute(
            """
            INSERT INTO visual_review_decisions (scan_id, group_id, decision, note, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(scan_id, group_id) DO UPDATE SET
              decision = excluded.decision, note = excluded.note, updated_at = excluded.updated_at
            """,
            [scan_id, group_id, decision, note, updated_at],
        )
    return {"deleted": False, "updated_at": updated_at}


def confirm_review(database: Path, scan_id: int, group_ids: list[str]) -> dict[str, object]:
    """Record that every visible group has a deliberate saved choice."""
    if not group_ids or len(group_ids) != len(set(group_ids)) or any(not GROUP_ID.fullmatch(group_id) for group_id in group_ids):
        raise ValueError("Invalid review groups.")
    placeholders = ", ".join("?" for _ in group_ids)
    with sqlite3.connect(database) as connection:
        saved_count = connection.execute(
            f"SELECT COUNT(*) FROM visual_review_decisions WHERE scan_id = ? AND group_id IN ({placeholders})",
            [scan_id, *group_ids],
        ).fetchone()[0]
        if saved_count != len(group_ids):
            raise ValueError("Every group needs a saved choice before confirmation.")
        confirmed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        connection.execute(
            """
            INSERT INTO visual_review_confirmations (scan_id, group_count, confirmed_at)
            VALUES (?, ?, ?)
            ON CONFLICT(scan_id) DO UPDATE SET
              group_count = excluded.group_count, confirmed_at = excluded.confirmed_at
            """,
            [scan_id, len(group_ids), confirmed_at],
        )
    return {"confirmed_at": confirmed_at, "group_count": len(group_ids)}


def decisions_for_scan(database: Path, scan_id: int) -> dict[str, dict[str, str]]:
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT group_id, decision, note, updated_at FROM visual_review_decisions WHERE scan_id = ?", [scan_id]
        ).fetchall()
    return {row[0]: {"decision": row[1], "note": row[2], "updated_at": row[3]} for row in rows}


def confirmation_for_scan(database: Path, scan_id: int) -> dict[str, object]:
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT group_count, confirmed_at FROM visual_review_confirmations WHERE scan_id = ?", [scan_id]
        ).fetchone()
    return {"group_count": row[0], "confirmed_at": row[1]} if row else {}


def make_handler(database: Path, directory: Path) -> type[SimpleHTTPRequestHandler]:
    class ReviewHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, directory=str(directory), **kwargs)

        def send_json(self, status: HTTPStatus, payload: object) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            request = urlparse(self.path)
            if request.path not in {"/api/decisions", "/api/confirmation"}:
                return super().do_GET()
            try:
                scan_id = int(parse_qs(request.query)["scan_id"][0])
                if scan_id < 1:
                    raise ValueError
                response = decisions_for_scan(database, scan_id) if request.path == "/api/decisions" else confirmation_for_scan(database, scan_id)
                self.send_json(HTTPStatus.OK, response)
            except (KeyError, ValueError):
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "A positive scan_id is required."})

        def do_POST(self) -> None:
            endpoint = urlparse(self.path).path
            if endpoint not in {"/api/decisions", "/api/confirmation"}:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if not 0 < content_length <= 8192:
                    raise ValueError("Invalid request size.")
                payload = json.loads(self.rfile.read(content_length))
                scan_id = int(payload["scan_id"])
                if scan_id < 1:
                    raise ValueError("Invalid scan identifier.")
                if endpoint == "/api/decisions":
                    result = save_decision(
                        database, scan_id, str(payload["group_id"]), str(payload.get("decision", "")), str(payload.get("note", ""))
                    )
                else:
                    group_ids = payload["group_ids"]
                    if not isinstance(group_ids, list) or not all(isinstance(group_id, str) for group_id in group_ids):
                        raise ValueError("group_ids must be a list of group identifiers.")
                    result = confirm_review(database, scan_id, group_ids)
                self.send_json(HTTPStatus.OK, result)
            except (ValueError, KeyError, json.JSONDecodeError) as error:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})

        def log_message(self, format: str, *args: object) -> None:
            print(f"{self.address_string()} - {format % args}")

    return ReviewHandler


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=project_root / "data" / "photo_catalogue.db")
    parser.add_argument("--directory", type=Path, default=project_root / "reports" / "visual_review")
    parser.add_argument("--host", default="127.0.0.1", help="Local bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not args.directory.joinpath("index.html").is_file():
        parser.error("Review page is missing. Run scripts/build_visual_review.py first.")
    initialise_database(args.database)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.database, args.directory))
    print(f"Review server: http://{args.host}:{args.port}")
    print("Only the project database is writable. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nReview server stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
