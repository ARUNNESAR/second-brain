"""Backups of all saves, kept outside Supabase (the free plan has no backups of its own).

- Daily: every save is a small JSON file (saves/00021.json) in a private GitHub repo. Each night ONE commit
  uploads only new or changed saves and removes deleted ones, so the repo grows only by what you actually save
  (~20-40 MB a year), and git history still lets you go back to any day.
  Needs BACKUP_GITHUB_TOKEN (fine-grained token, Contents read/write on that repo only) and BACKUP_REPO.
- Also his profile/settings and his journey (you/settings.json, you/journal.json).
- Weekly: one compressed file with everything is sent to Arun in Telegram (bot.py), as a second copy elsewhere.
"""
import datetime as dt
import gzip
import hashlib
import json
import os
import urllib.error
import urllib.request

import db

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _records():
    """Every save as a dict (search vectors included, so a restore needs no AI calls)."""
    return [{
        "id": r[0], "created_at": r[1].isoformat(), "source_type": r[2], "source_url": r[3], "title": r[4],
        "topic": r[5], "summary": r[6], "key_points": r[7], "content": r[8], "status": r[9],
        "embedding": [round(float(x), 6) for x in r[10].to_list()] if r[10] is not None else None,
        "proven": r[11], "creator": r[12],
    } for r in db.all_for_backup()]


def _you():
    """His profile/settings and his journey, as backup files."""
    settings, journal = db.backup_you()
    return {"you/settings.json": settings, "you/journal.json": journal}


def make_file():
    """Everything in one gzipped JSON file (for Telegram and /backup). Returns (bytes, count)."""
    saves = _records()
    data = {"made_at": dt.datetime.now(IST).isoformat(), "count": len(saves), "saves": saves,
            "you": {p.split("/")[1][:-5]: v for p, v in _you().items()}}
    return gzip.compress(json.dumps(data, ensure_ascii=False).encode("utf-8")), len(saves)


# ---------- GitHub (one commit a day with only the changes) ----------

def _api(method, path, body=None):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{os.environ['BACKUP_REPO'].strip()}/{path}",
        data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={"Authorization": f"Bearer {os.environ['BACKUP_GITHUB_TOKEN'].strip()}",
                 "Accept": "application/vnd.github+json", "User-Agent": "second-brain-backup"})
    return json.load(urllib.request.urlopen(req, timeout=60))


def _blob_sha(content: bytes):
    """The id git gives a file's content, so we can tell which saves changed without downloading anything."""
    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()


def to_github():
    """Daily backup. Returns (new_or_changed, removed, total), or None if GitHub backup isn't set up."""
    if not os.environ.get("BACKUP_GITHUB_TOKEN") or not os.environ.get("BACKUP_REPO"):
        return None
    files = {f"saves/{s['id']:05d}.json": json.dumps(s, ensure_ascii=False, indent=1).encode("utf-8")
             for s in _records()}
    n_saves = len(files)
    files.update({p: json.dumps(v, ensure_ascii=False, indent=1).encode("utf-8") for p, v in _you().items()})

    try:
        head = _api("GET", "git/ref/heads/main")["object"]["sha"]
        base_tree = _api("GET", f"git/commits/{head}")["tree"]["sha"]
        current = {t["path"]: t["sha"] for t in _api("GET", f"git/trees/{base_tree}?recursive=1")["tree"]
                   if t["type"] == "blob"}
    except urllib.error.HTTPError as e:
        if e.code not in (404, 409):  # 404/409 = brand-new empty repo
            raise
        head, base_tree, current = None, None, {}

    changed = {p: c for p, c in files.items() if current.get(p) != _blob_sha(c)}
    removed = [p for p in current if p.startswith("saves/") and p not in files]
    if not changed and not removed:
        return 0, 0, n_saves

    tree = [{"path": p, "mode": "100644", "type": "blob", "content": c.decode("utf-8")} for p, c in changed.items()]
    tree += [{"path": p, "mode": "100644", "type": "blob", "sha": None} for p in removed]
    body = {"tree": tree}
    if base_tree:
        body["base_tree"] = base_tree
    new_tree = _api("POST", "git/trees", body)["sha"]
    msg = f"Backup {dt.datetime.now(IST):%Y-%m-%d}: {len(changed)} new/changed, {len(removed)} removed, {n_saves} saves"
    commit = _api("POST", "git/commits", {"message": msg, "tree": new_tree, "parents": [head] if head else []})["sha"]
    if head:
        _api("PATCH", "git/refs/heads/main", {"sha": commit})
    else:
        _api("POST", "git/refs", {"ref": "refs/heads/main", "sha": commit})
    return len(changed), len(removed), n_saves
