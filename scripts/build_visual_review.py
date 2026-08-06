"""Build a local thumbnail review page for visually similar image groups."""

from __future__ import annotations

import argparse
import hashlib
import html
import shutil
import sqlite3
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image, UnidentifiedImageError
from pillow_heif import register_heif_opener

register_heif_opener()


class DisjointSet:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, index: int) -> int:
        while self.parent[index] != index:
            self.parent[index] = self.parent[self.parent[index]]
            index = self.parent[index]
        return index

    def union(self, first: int, second: int) -> None:
        first_root, second_root = self.find(first), self.find(second)
        if first_root != second_root:
            self.parent[second_root] = first_root


def group_id(items: list[dict[str, object]], component: list[int]) -> str:
    """Return a stable identifier for a visual group in one scan."""
    paths = sorted(str(items[index]["relative_path"]) for index in component)
    digest = hashlib.sha256("\0".join(paths).encode("utf-8")).hexdigest()
    return digest[:16]


def latest_scan_and_model(connection: sqlite3.Connection) -> tuple[int, str]:
    scan = connection.execute(
        "SELECT scan_id FROM scan_runs WHERE completed_at IS NOT NULL ORDER BY scan_id DESC LIMIT 1"
    ).fetchone()
    if not scan:
        raise ValueError("No completed scan is available.")
    model = connection.execute(
        "SELECT model_name FROM image_embeddings WHERE scan_id = ? ORDER BY computed_at DESC LIMIT 1", [scan[0]]
    ).fetchone()
    if not model:
        raise ValueError("No image embeddings are available. Run embed_images.py first.")
    return scan[0], model[0]


def candidates(database: Path, threshold: float) -> tuple[int, str, list[dict[str, object]], list[tuple[int, int, float]]]:
    with sqlite3.connect(database) as connection:
        scan_id, model_name = latest_scan_and_model(connection)
        rows = connection.execute("""
            SELECT media.path, media.relative_path, media.sha256, media.captured_at,
                   media.width, media.height, media.size_bytes, media.extension,
                   embedding.dimensions, embedding.embedding
            FROM image_embeddings AS embedding
            JOIN media_files AS media
              ON media.scan_id = embedding.scan_id AND media.path = embedding.path
            WHERE embedding.scan_id = ? AND embedding.model_name = ?
            ORDER BY media.relative_path
        """, [scan_id, model_name]).fetchall()

    items = [
        {
            "path": row[0], "relative_path": row[1], "sha256": row[2], "captured_at": row[3],
            "width": row[4], "height": row[5], "size_bytes": row[6], "extension": row[7],
            "dimensions": row[8], "embedding": row[9],
        }
        for row in rows
    ]
    vectors = np.vstack([np.frombuffer(item["embedding"], dtype=np.float32, count=item["dimensions"]) for item in items])
    scores = vectors @ vectors.T
    indexes_a, indexes_b = np.where((scores >= threshold) & np.triu(np.ones(scores.shape, dtype=bool), 1))
    pairs = [
        (int(index_a), int(index_b), float(scores[index_a, index_b]))
        for index_a, index_b in zip(indexes_a, indexes_b, strict=True)
        if items[index_a]["sha256"] != items[index_b]["sha256"]
    ]
    return scan_id, model_name, items, pairs


def create_thumbnail(source: Path, destination: Path) -> bool:
    try:
        with Image.open(source) as image:
            image = image.convert("RGB")
            image.thumbnail((280, 220))
            canvas = Image.new("RGB", (280, 220), "#1d1d1d")
            canvas.paste(image, ((280 - image.width) // 2, (220 - image.height) // 2))
            canvas.save(destination, "JPEG", quality=85)
        return True
    except (OSError, UnidentifiedImageError, ValueError):
        return False


def build_review(database: Path, output: Path, threshold: float) -> None:
    scan_id, model_name, items, pairs = candidates(database, threshold)
    groups = DisjointSet(len(items))
    for first, second, _ in pairs:
        groups.union(first, second)

    members: dict[int, list[int]] = defaultdict(list)
    for first, second, _ in pairs:
        members[groups.find(first)].extend((first, second))
    components = [sorted(set(component)) for component in members.values()]
    components = sorted(components, key=lambda component: (-len(component), items[component[0]]["relative_path"]))

    if output.exists():
        shutil.rmtree(output)
    thumbs = output / "thumbs"
    thumbs.mkdir(parents=True)

    scores_by_group: dict[int, list[float]] = defaultdict(list)
    for first, second, score in pairs:
        scores_by_group[groups.find(first)].append(score)

    sections: list[str] = []
    thumbnail_count = 0
    for group_number, component in enumerate(components, start=1):
        group_root = groups.find(component[0])
        stable_group_id = group_id(items, component)
        cards: list[str] = []
        for item_number, item_index in enumerate(component, start=1):
            item = items[item_index]
            thumbnail_name = f"group-{group_number:03d}-{item_number:02d}.jpg"
            thumbnail_path = thumbs / thumbnail_name
            if create_thumbnail(Path(item["path"]), thumbnail_path):
                thumbnail_count += 1
                preview = f'<img src="thumbs/{thumbnail_name}" alt="{html.escape(str(item["relative_path"]))}">'
            else:
                preview = '<div class="unavailable">Thumbnail unavailable</div>'
            captured = item["captured_at"] or "No capture date"
            cards.append(f"""
                <article class="card">
                  {preview}
                  <code>{html.escape(str(item["relative_path"]))}</code>
                  <small>{html.escape(str(captured))}</small>
                </article>
            """)
        group_scores = scores_by_group[group_root]
        sections.append(f"""
          <section class="group" data-group-id="{stable_group_id}" data-member-count="{len(component)}">
            <h2>Group {group_number} <span>{len(component)} images · similarity {min(group_scores):.4f}–{max(group_scores):.4f}</span></h2>
            <p>These may be burst shots, edited copies, or near-duplicates. Review visually; do not delete automatically.</p>
            <div class="grid">{''.join(cards)}</div>
            <div class="decision-panel">
              <p class="decision-question">Click one option. You can change it any time.</p>
              <input class="decision" type="hidden" value="">
              <div class="choice-list" role="group" aria-label="Review decision">
                <button class="choice" type="button" data-decision="keep_all"><strong>Keep all</strong><span>Do nothing.</span></button>
                <button class="choice" type="button" data-decision="delete_all_but_one"><strong>Keep best</strong><span>Keep the best file automatically.</span></button>
                <button class="choice" type="button" data-decision="delete_group"><strong>Remove all</strong><span>Mark every file for later removal.</span></button>
              </div>
              <span class="decision-status" aria-live="polite"></span>
            </div>
          </section>
        """)

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Photo Library Curator — visual review</title>
<style>
body {{ background:#121212; color:#e8e8e8; font:16px/1.4 system-ui,sans-serif; margin:0 auto; max-width:1500px; padding:2rem; }}
h1 {{ margin-bottom:.2rem; }} .meta {{ color:#bdbdbd; }}
.notice {{ background:#2a2316; border-left:4px solid #f0aa3c; padding:1rem; margin:1.5rem 0; }}
.group {{ border-top:1px solid #444; padding:1.4rem 0; }} h2 {{ font-size:1.2rem; }} h2 span {{ color:#bdbdbd; font-size:.9rem; font-weight:normal; }}
.group p {{ color:#bdbdbd; margin-top:0; }} .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:1rem; }}
.card {{ background:#202020; padding:.5rem; border-radius:.35rem; overflow-wrap:anywhere; }} img,.unavailable {{ display:block; width:100%; height:220px; object-fit:contain; background:#1d1d1d; }}
.unavailable {{ display:grid; place-items:center; color:#aaa; }} code {{ display:block; font-size:.75rem; margin-top:.45rem; }} small {{ color:#aaa; display:block; margin-top:.25rem; }}
.decision-panel {{ display:flex; flex-wrap:wrap; align-items:end; gap:.75rem; margin-top:1rem; padding:1rem; background:#1b2430; border-radius:.35rem; }}
.decision-question {{ width:100%; margin:0; color:#fff; font-weight:600; }} .choice-list {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:.5rem; width:100%; }}
.choice {{ text-align:left; border:1px solid #596a7b; border-radius:.35rem; color:#edf2f7; background:#263544; padding:.7rem; cursor:pointer; }} .choice:hover,.choice.selected {{ background:#36536d; border-color:#9bc8ea; }}
.choice strong,.choice span {{ display:block; }} .choice span {{ color:#c6d1dc; font-size:.8rem; margin-top:.2rem; }} .decision-panel label {{ display:grid; gap:.25rem; font-size:.85rem; color:#c6d1dc; }} .decision-panel input,.decision-panel button {{ font:inherit; padding:.45rem; }}
.note-label {{ flex:1 1 280px; }} .decision-status {{ color:#b9d8b0; font-size:.9rem; }}
</style></head><body>
<h1>Visual similarity review</h1>
<p class="meta">Scan {scan_id} · Model {html.escape(model_name)} · Threshold {threshold:.3f}</p>
<div class="notice"><strong>Review only.</strong> This page was generated from local visual embeddings. It neither moves nor deletes source files.</div>
<div class="notice"><strong>Saving decisions:</strong> start <code>uv run python scripts/review_server.py</code>, then open <code>http://127.0.0.1:8765</code>. Decisions are stored only in this project's catalogue.</div>
<p>{len(components)} groups from {len(pairs)} non-identical visual-similarity pairs.</p>
{''.join(sections) if sections else '<p>No visual groups met the threshold.</p>'}
<section class="review-completion">
  <h2>Cleanup summary</h2>
  <p id="cleanup-summary">Loading saved choices…</p>
  <p class="completion-note">Selections save immediately. Nothing is removed until a future, separate cleanup action.</p>
</section>
<script>
const scanId = {scan_id};
const panels = [...document.querySelectorAll('.group')];
const setStatus = (panel, message) => panel.querySelector('.decision-status').textContent = message;
const cleanupSummary = document.querySelector('#cleanup-summary');
const setChoice = (panel, decision) => {{
  panel.querySelector('.decision').value = decision;
  panel.querySelectorAll('.choice').forEach(choice => {{
    const selected = choice.dataset.decision === decision;
    choice.classList.toggle('selected', selected);
    choice.setAttribute('aria-pressed', selected);
  }});
}};
const updateSummary = () => {{
  const count = decision => panels.filter(panel => panel.querySelector('.decision').value === decision).length;
  const keepBest = count('delete_all_but_one');
  const removeAll = count('delete_group');
  const removals = panels.reduce((total, panel) => {{
    const choice = panel.querySelector('.decision').value;
    const members = Number(panel.dataset.memberCount);
    return total + (choice === 'delete_group' ? members : choice === 'delete_all_but_one' ? members - 1 : 0);
  }}, 0);
  cleanupSummary.textContent = `${{removals}} photos marked for later removal · ${{keepBest}} groups keep best · ${{removeAll}} groups remove all.`;
}};
const savePanel = async (panel) => {{
  const response = await fetch('/api/decisions', {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{scan_id: scanId, group_id: panel.dataset.groupId,
      decision: panel.querySelector('.decision').value, note: ''}}),
  }});
  if (!response.ok) throw new Error('save failed');
  return response.json();
}};
if (location.protocol === 'file:') {{
  document.querySelectorAll('.choice').forEach(button => button.disabled = true);
  panels.forEach(panel => setStatus(panel, 'Open through the local review server to save.'));
  cleanupSummary.textContent = 'Open through the local review server to see saved choices.';
}} else {{
  fetch(`/api/decisions?scan_id=${{scanId}}`).then(response => response.ok ? response.json() : Promise.reject()).then(decisions => {{
    panels.forEach(panel => {{
      const saved = decisions[panel.dataset.groupId];
      if (!saved) return;
      setChoice(panel, saved.decision);
      setStatus(panel, `Saved ${{saved.updated_at}}`);
    }});
    updateSummary();
  }}).catch(() => {{
    panels.forEach(panel => setStatus(panel, 'Local review server is unavailable.'));
    cleanupSummary.textContent = 'Local review server is unavailable.';
  }});
  panels.forEach(panel => panel.querySelectorAll('.choice').forEach(choice => choice.addEventListener('click', async () => {{
    setChoice(panel, choice.dataset.decision);
    setStatus(panel, 'Saving choice…');
    try {{
      const saved = await savePanel(panel);
      setStatus(panel, `Saved ${{saved.updated_at}}`);
      updateSummary();
    }} catch (error) {{ setStatus(panel, 'Could not save. Is the local review server running?'); }}
  }})));
}}
</script>
</body></html>"""
    (output / "index.html").write_text(page, encoding="utf-8")
    print(f"Wrote {len(components)} groups, {len(pairs)} pairs, and {thumbnail_count} thumbnails to {output / 'index.html'}")


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=project_root / "data" / "photo_catalogue.db")
    parser.add_argument("--output", type=Path, default=project_root / "reports" / "visual_review")
    parser.add_argument("--threshold", type=float, default=0.995)
    args = parser.parse_args()
    if not 0 < args.threshold <= 1:
        parser.error("--threshold must be greater than 0 and at most 1")
    build_review(args.database, args.output, args.threshold)


if __name__ == "__main__":
    main()
