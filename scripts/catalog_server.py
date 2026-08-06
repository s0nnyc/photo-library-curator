"""Serve a local, read-only virtual catalogue for a photo library."""

from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
import open_clip
import torch
from PIL import Image, UnidentifiedImageError
from pillow_heif import register_heif_opener

register_heif_opener()
PAGE_SIZE = 120


def latest_scan(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT scan_id FROM scan_runs WHERE completed_at IS NOT NULL ORDER BY scan_id DESC LIMIT 1"
    ).fetchone()
    if not row:
        raise ValueError("No completed scan is available.")
    return row[0]


def catalogue_rows(database: Path, year: str, kind: str, search: str, offset: int) -> list[dict[str, object]]:
    with sqlite3.connect(database) as connection:
        scan_id = latest_scan(connection)
        clauses = ["scan_id = ?", "media_kind IN ('image', 'video')"]
        values: list[object] = [scan_id]
        if year:
            clauses.append("captured_at LIKE ?")
            values.append(f"{year}-%")
        if kind in {"image", "video"}:
            clauses.append("media_kind = ?")
            values.append(kind)
        if search:
            clauses.append("lower(relative_path) LIKE lower(?)")
            values.append(f"%{search}%")
        values.extend([PAGE_SIZE + 1, offset])
        rows = connection.execute(
            f"""
            SELECT relative_path, media_kind, captured_at, width, height, size_bytes, camera_make, camera_model
            FROM media_files WHERE {' AND '.join(clauses)}
            ORDER BY captured_at DESC NULLS LAST, relative_path
            LIMIT ? OFFSET ?
            """,
            values,
        ).fetchall()
    return [
        {
            "relative_path": row[0], "kind": row[1], "captured_at": row[2], "width": row[3], "height": row[4],
            "size_bytes": row[5], "camera": " ".join(part for part in row[6:8] if part),
        }
        for row in rows
    ]


def summary(database: Path) -> dict[str, object]:
    with sqlite3.connect(database) as connection:
        scan_id = latest_scan(connection)
        total, images, videos, dated = connection.execute(
            """
            SELECT COUNT(*), SUM(media_kind = 'image'), SUM(media_kind = 'video'), COUNT(captured_at)
            FROM media_files WHERE scan_id = ? AND media_kind IN ('image', 'video')
            """,
            [scan_id],
        ).fetchone()
        years = [row[0] for row in connection.execute(
            "SELECT DISTINCT substr(captured_at, 1, 4) FROM media_files WHERE scan_id = ? AND captured_at IS NOT NULL ORDER BY 1 DESC",
            [scan_id],
        )]
    return {"total": total, "images": images, "videos": videos, "dated": dated, "years": years}


def thumbnail(database: Path, relative_path: str) -> bytes:
    with sqlite3.connect(database) as connection:
        scan_id = latest_scan(connection)
        row = connection.execute(
            "SELECT path, media_kind FROM media_files WHERE scan_id = ? AND relative_path = ?", [scan_id, relative_path]
        ).fetchone()
    if not row or row[1] != "image":
        raise FileNotFoundError(relative_path)
    try:
        with Image.open(row[0]) as image:
            image = image.convert("RGB")
            image.thumbnail((360, 260))
            output = io.BytesIO()
            image.save(output, "JPEG", quality=82)
            return output.getvalue()
    except (OSError, UnidentifiedImageError, ValueError) as error:
        raise FileNotFoundError(relative_path) from error


class SemanticSearch:
    """Lazy, local CLIP text search over the image embeddings already in SQLite."""

    def __init__(self, database: Path) -> None:
        self.database = database
        self.model: torch.nn.Module | None = None
        self.tokenizer: object | None = None
        self.device: torch.device | None = None
        self.items: list[dict[str, object]] = []
        self.vectors: np.ndarray | None = None
        self.lock = threading.Lock()

    def load(self) -> None:
        if self.model is not None:
            return
        with sqlite3.connect(self.database) as connection:
            scan_id = latest_scan(connection)
            model_id = connection.execute(
                "SELECT model_name FROM image_embeddings WHERE scan_id = ? ORDER BY computed_at DESC LIMIT 1", [scan_id]
            ).fetchone()
            if not model_id:
                raise ValueError("No image embeddings are available. Run the analysis workflow first.")
            model_name, pretrained = model_id[0].split(":", maxsplit=1)
            rows = connection.execute(
                """
                SELECT media.relative_path, media.captured_at, media.width, media.height, media.size_bytes,
                       media.camera_make, media.camera_model, embedding.dimensions, embedding.embedding
                FROM image_embeddings AS embedding JOIN media_files AS media
                  ON media.scan_id = embedding.scan_id AND media.path = embedding.path
                WHERE embedding.scan_id = ? AND embedding.model_name = ?
                ORDER BY media.relative_path
                """,
                [scan_id, model_id[0]],
            ).fetchall()
        project = Path(__file__).resolve().parents[1]
        os.environ.setdefault("HF_HOME", str(project / ".cache" / "huggingface"))
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        model, _, _ = open_clip.create_model_and_transforms(model_name, pretrained=pretrained)
        self.model = model.to(self.device).eval()
        self.tokenizer = open_clip.get_tokenizer(model_name)
        self.items = [
            {"relative_path": row[0], "kind": "image", "captured_at": row[1], "width": row[2], "height": row[3],
             "size_bytes": row[4], "camera": " ".join(part for part in row[5:7] if part)}
            for row in rows
        ]
        self.vectors = np.vstack([np.frombuffer(row[8], dtype=np.float32, count=row[7]) for row in rows])

    def search(self, query: str, limit: int = PAGE_SIZE) -> list[dict[str, object]]:
        if not query.strip():
            return []
        with self.lock:
            self.load()
            assert self.model is not None and self.tokenizer is not None and self.device is not None and self.vectors is not None
            tokens = self.tokenizer([query]).to(self.device)
            with torch.inference_mode():
                vector = self.model.encode_text(tokens)
                vector = vector / vector.norm(dim=-1, keepdim=True)
            scores = self.vectors @ vector.float().cpu().numpy()[0]
            indexes = np.argsort(scores)[::-1][:limit]
            return [{**self.items[index], "score": round(float(scores[index]), 4)} for index in indexes]


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Photo Library Curator — catalogue</title>
<style>
body{background:#111820;color:#eaf0f5;font:16px/1.4 system-ui,sans-serif;max-width:1500px;margin:0 auto;padding:2rem}h1{margin-bottom:.2rem}.meta{color:#b8c5d1}.controls{display:flex;gap:.7rem;flex-wrap:wrap;margin:1.5rem 0}.controls input,.controls select,.controls button,#more{font:inherit;padding:.55rem}.controls input{min-width:260px}.notice{padding:1rem;background:#1d2a37;border-left:4px solid #58a6d6}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:1rem}.card{background:#1b2631;border:1px solid #3d4d5c;border-radius:.4rem;overflow:hidden}.card img,.video{width:100%;height:170px;object-fit:contain;background:#0d1218;display:block}.video{display:grid;place-items:center;color:#aebdca}.card div{padding:.6rem;overflow-wrap:anywhere}.card code,.card small{display:block;font-size:.75rem;margin-top:.25rem}.card small{color:#bac7d3}#more{display:block;margin:1.5rem auto}.hidden{display:none}
</style></head><body><h1>Virtual photo library</h1><p class="meta" id="summary">Loading catalogue…</p><div class="notice">Browse without changing your folder structure. Thumbnails are generated locally and source files remain read-only.</div>
<div class="controls"><input id="search" placeholder="Search file or folder name"><select id="year"><option value="">All years</option></select><select id="kind"><option value="">Photos and videos</option><option value="image">Photos</option><option value="video">Videos</option></select><button id="apply">Search</button></div><div class="controls"><input id="semantic" placeholder="Describe a photo, e.g. football or sunset"><button id="semantic-apply">Find with local AI</button><small id="semantic-status"></small></div><main class="grid" id="grid"></main><button id="more" class="hidden">Load more</button>
<script>
const grid=document.querySelector('#grid'),search=document.querySelector('#search'),year=document.querySelector('#year'),kind=document.querySelector('#kind'),more=document.querySelector('#more'),semantic=document.querySelector('#semantic'),semanticStatus=document.querySelector('#semantic-status');let offset=0,hasMore=false;
function card(item){const element=document.createElement('article');element.className='card';if(item.kind==='image'){const image=document.createElement('img');image.loading='lazy';image.src='/thumbnail?path='+encodeURIComponent(item.relative_path);element.append(image)}else{const video=document.createElement('div');video.className='video';video.textContent='VIDEO';element.append(video)}const text=document.createElement('div'),name=document.createElement('code');name.textContent=item.relative_path;const details=document.createElement('small');details.textContent=[item.captured_at||'No capture date',item.width&&item.height?item.width+'×'+item.height:'', (item.size_bytes/1024/1024).toFixed(1)+' MiB',item.camera,item.score!==undefined?'AI match '+item.score:''].filter(Boolean).join(' · ');text.append(name,details);element.append(text);return element}
async function load(reset=false){if(reset){offset=0;grid.replaceChildren()}const params=new URLSearchParams({offset,search:search.value,year:year.value,kind:kind.value});const response=await fetch('/api/catalog?'+params);const result=await response.json();result.items.forEach(item=>grid.append(card(item)));offset+=result.items.length;hasMore=result.has_more;more.classList.toggle('hidden',!hasMore)}
fetch('/api/summary').then(response=>response.json()).then(data=>{document.querySelector('#summary').textContent=`${data.total.toLocaleString()} files · ${data.images.toLocaleString()} photos · ${data.videos.toLocaleString()} videos · ${data.dated.toLocaleString()} with capture dates`;data.years.forEach(value=>{const option=document.createElement('option');option.value=value;option.textContent=value;year.append(option)});load(true)});document.querySelector('#apply').addEventListener('click',()=>load(true));search.addEventListener('keydown',event=>{if(event.key==='Enter')load(true)});more.addEventListener('click',()=>load());document.querySelector('#semantic-apply').addEventListener('click',async()=>{const query=semantic.value.trim();if(!query)return;semanticStatus.textContent='Loading local AI and searching…';more.classList.add('hidden');try{const response=await fetch('/api/semantic-search?q='+encodeURIComponent(query));const result=await response.json();if(!response.ok)throw new Error(result.error||'Search failed');grid.replaceChildren(...result.items.map(card));semanticStatus.textContent=`Top ${result.items.length} local AI matches for “${query}”.`}catch(error){semanticStatus.textContent=error.message}});semantic.addEventListener('keydown',event=>{if(event.key==='Enter')document.querySelector('#semantic-apply').click()});
</script></body></html>"""


def serve(database: Path, host: str = "127.0.0.1", port: int = 8767) -> None:
    semantic_search = SemanticSearch(database)
    class Handler(BaseHTTPRequestHandler):
        def send_json(self, status: HTTPStatus, payload: object) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def do_GET(self) -> None:
            request = urlparse(self.path)
            query = parse_qs(request.query)
            try:
                if request.path == "/":
                    body = PAGE.encode("utf-8")
                    self.send_response(HTTPStatus.OK); self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                elif request.path == "/api/summary": self.send_json(HTTPStatus.OK, summary(database))
                elif request.path == "/api/catalog":
                    offset = max(0, int(query.get("offset", ["0"])[0]))
                    rows = catalogue_rows(database, query.get("year", [""])[0], query.get("kind", [""])[0], query.get("search", [""])[0][:200], offset)
                    self.send_json(HTTPStatus.OK, {"items": rows[:PAGE_SIZE], "has_more": len(rows) > PAGE_SIZE})
                elif request.path == "/api/semantic-search":
                    text = query.get("q", [""])[0][:200]
                    self.send_json(HTTPStatus.OK, {"items": semantic_search.search(text)})
                elif request.path == "/thumbnail":
                    body = thumbnail(database, query["path"][0])
                    self.send_response(HTTPStatus.OK); self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                else: self.send_error(HTTPStatus.NOT_FOUND)
            except (KeyError, ValueError, FileNotFoundError) as error: self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Virtual catalogue: http://{host}:{port}")
    try: server.serve_forever()
    except KeyboardInterrupt: print("\nCatalogue server stopped.")
    finally: server.server_close()


def main() -> None:
    project = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=project / "data" / "photo_catalogue.db")
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args(); serve(args.database, port=args.port)


if __name__ == "__main__": main()
