"""Build the local approval page for an automatic cleanup recommendation."""

from __future__ import annotations

import html
import json
import shutil
from pathlib import Path

from build_visual_review import create_thumbnail


def build_page(manifest_path: Path, output: Path) -> None:
    plan = json.loads(manifest_path.read_text(encoding="utf-8"))
    if output.exists():
        shutil.rmtree(output)
    thumbs = output / "thumbs"
    thumbs.mkdir(parents=True)

    thumbnail_number = 0
    sections: list[str] = []
    for group_number, group in enumerate(plan["groups"], start=1):
        cards: list[str] = []
        for member in group["members"]:
            thumbnail_number += 1
            thumbnail_name = f"{thumbnail_number:04d}.jpg"
            if create_thumbnail(Path(member["path"]), thumbs / thumbnail_name):
                preview = f'<img src="thumbs/{thumbnail_name}" alt="{html.escape(member["relative_path"])}">'
            else:
                preview = '<div class="unavailable">Preview unavailable</div>'
            dimensions = f'{member.get("width")}×{member.get("height")}' if member.get("width") and member.get("height") else ""
            cards.append(f"""
              <button class="media" data-path="{html.escape(member['path'])}" type="button">
                {preview}
                <strong class="member-status"></strong>
                <code>{html.escape(member['relative_path'])}</code>
                <small>{dimensions} · {int(member['size_bytes']) / 1024**2:.1f} MiB</small>
              </button>
            """)
        sections.append(f"""
          <section class="group" data-group-id="{group['id']}">
            <h2>Group {group_number} <span>{html.escape(group['kind'])}</span></h2>
            <div class="group-controls">
              <button class="keep-all" type="button">Keep all</button>
              <button class="use-suggestion" type="button">Use suggested keeper</button>
              <button class="remove-all" type="button">Remove all</button>
              <small>In review mode, click any photo to make it the keeper.</small>
            </div>
            <div class="media-grid">{''.join(cards)}</div>
          </section>
        """)

    embedded_groups = json.dumps(plan["groups"]).replace("</", "<\\/")
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Photo Library Curator — approve cleanup</title>
<style>
body {{ background:#111820; color:#edf2f7; font:16px/1.4 system-ui,sans-serif; max-width:1500px; margin:0 auto; padding:2rem; }}
h1 {{ margin-bottom:.25rem; }} .meta,.group h2 span,small {{ color:#b8c5d1; }} .notice {{ padding:1rem; border-left:4px solid #e5a63d; background:#292316; margin:1rem 0; }}
.actions {{ display:flex; gap:.75rem; flex-wrap:wrap; align-items:center; padding:1rem 0; position:sticky; top:0; background:#111820; z-index:1; }} button {{ font:inherit; cursor:pointer; }} #approve {{ background:#2f855a; color:white; border:0; padding:.75rem 1rem; border-radius:.35rem; }} #review {{ background:#34495e; color:white; border:0; padding:.75rem 1rem; border-radius:.35rem; }}
.group {{ border-top:1px solid #3e4d5c; padding:1.5rem 0; }} .group h2 {{ font-size:1.1rem; }} .group-controls {{ display:none; gap:.5rem; align-items:center; margin:.6rem 0; }} body.review .group-controls {{ display:flex; flex-wrap:wrap; }} .group-controls button {{ padding:.4rem .6rem; }}
.media-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(250px,1fr)); gap:1rem; }} .media {{ position:relative; text-align:left; color:inherit; border:2px solid #465563; border-radius:.4rem; background:#1d2833; padding:.45rem; overflow-wrap:anywhere; }}
.media img,.unavailable {{ width:100%; height:190px; display:block; object-fit:contain; background:#0f141a; }} .unavailable {{ display:grid; place-items:center; color:#9aa8b5; }} .media code,.media small,.member-status {{ display:block; margin-top:.35rem; font-size:.75rem; }}
.media.keeper {{ border-color:#4bb57c; }} .media.removal {{ border-color:#d66a6a; opacity:.88; }} .media.keeper .member-status {{ color:#72df9f; }} .media.removal .member-status {{ color:#ff9595; }} body.review .media {{ cursor:pointer; }}
</style></head><body>
<h1>Recommended cleanup</h1>
<p class="meta">Local AI recommendation · scan {plan['scan_id']} · threshold {plan['visual_similarity_threshold']:.3f}</p>
<div class="notice"><strong>Nothing has changed.</strong> Green is kept; red moves to a recoverable folder only after approval.</div>
<div class="actions"><button id="approve" type="button">Approve suggested cleanup</button><button id="review" type="button">Review / change suggestion</button><strong id="summary"></strong></div>
<p id="message"></p>
{''.join(sections)}
<script>
const groups = {embedded_groups};
const choices = Object.fromEntries(groups.map(group => [group.id, group.recommended_keeper]));
let reviewMode = false;
const summary = document.querySelector('#summary'); const message = document.querySelector('#message'); const approve = document.querySelector('#approve');
function update() {{
  let count = 0, bytes = 0;
  groups.forEach(group => {{
    const selected = choices[group.id]; const section = document.querySelector(`[data-group-id="${{group.id}}"]`);
    section.querySelectorAll('.media').forEach(card => {{
      const keep = selected !== 'keep_all' && selected !== 'delete_all' && card.dataset.path === selected;
      const remove = selected === 'delete_all' || (selected !== 'keep_all' && !keep);
      card.classList.toggle('keeper', keep); card.classList.toggle('removal', remove);
      card.querySelector('.member-status').textContent = keep ? 'KEEP' : remove ? 'MOVE TO RECOVERY' : 'KEEP';
    }});
    if (selected === 'delete_all') group.members.forEach(item => {{ count++; bytes += item.size_bytes; }});
    else if (selected !== 'keep_all') group.members.filter(item => item.path !== selected).forEach(item => {{ count++; bytes += item.size_bytes; }});
  }});
  summary.textContent = `${{count}} files · ${{(bytes / 1024 / 1024).toFixed(1)}} MiB marked for recovery`;
  approve.textContent = reviewMode ? 'Approve adjusted cleanup' : 'Approve suggested cleanup';
}}
document.querySelector('#review').addEventListener('click', () => {{ reviewMode = true; document.body.classList.add('review'); message.textContent = 'Review mode: click a photo to keep it, or choose Keep all. Then approve the adjusted cleanup.'; update(); }});
document.querySelectorAll('.group').forEach(section => {{
  const id = section.dataset.groupId; const group = groups.find(item => item.id === id);
  section.querySelector('.keep-all').addEventListener('click', () => {{ choices[id] = 'keep_all'; update(); }});
  section.querySelector('.use-suggestion').addEventListener('click', () => {{ choices[id] = group.recommended_keeper; update(); }});
  section.querySelector('.remove-all').addEventListener('click', () => {{ choices[id] = 'delete_all'; update(); }});
  section.querySelectorAll('.media').forEach(card => card.addEventListener('click', () => {{ if (reviewMode) {{ choices[id] = card.dataset.path; update(); }} }}));
}});
approve.addEventListener('click', async () => {{
  if (!confirm('Move the marked files to the recovery folder? They will not be permanently deleted.')) return;
  approve.disabled = true; message.textContent = 'Validating files and moving them to recovery…';
  try {{
    const response = await fetch('/api/approve-cleanup', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{choices}})}});
    const result = await response.json(); if (!response.ok) throw new Error(result.error || 'Approval failed');
    message.textContent = `Moved ${{result.moved}} files to ${{result.recovery_root}}. Undo log created there.`;
  }} catch (error) {{ message.textContent = `Nothing further moved: ${{error.message}}`; approve.disabled = false; }}
}});
update();
</script></body></html>"""
    (output / "index.html").write_text(page, encoding="utf-8")
    print(f"Approval page: {output / 'index.html'}")
