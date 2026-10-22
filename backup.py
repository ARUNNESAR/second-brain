"""Backups of all saves, kept outside Supabase (the free plan has no backups of its own).

- Daily: a compressed JSON file is committed to a private GitHub repo. Git keeps every day's version.
  Needs BACKUP_GITHUB_TOKEN (fine-grained token, Contents read/write on that repo only) and BACKUP_REPO.
- Weekly: the same file is sent to Arun in Telegram (done in bot.py), so there's a second copy.
"""
import base64
import datetime as dt
import gzip
import json
import os
import urllib.error
import urllib.request

import db

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def make_file():
    """All saves (including their search vectors, so a restore needs no AI calls) as gzipped JSON bytes."""
    rows = db.all_for_backup()
    data = {
        "made_at": dt.datetime.now(IST).isoformat(),
        "count": len(rows),
        "saves": [{
            "id": r[0], "created_at": r[1].isoformat(), "source_type": r[2], "source_url": r[3], "title": r[4],
            "topic": r[5], "summary": r[6], "key_points": r[7], "content": r[8], "status": r[9],
            "embedding": [round(float(x), 6) for x in r[10].to_list()] if r[10] is not None else None,
        } for r in rows],
    }
    return gzip.compress(json.dumps(data, ensure_ascii=False).encode("utf-8")), len(rows)


def _github(method, path, body=None):
    token = os.environ["BACKUP_GITHUB_TOKEN"].strip()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{os.environ['BACKUP_REPO'].strip()}/contents/{path}",
        data=json.dumps(body).encode() if body else None, method=method,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "User-Agent": "second-brain-backup"})
    return json.load(urllib.request.urlopen(req, timeout=60))


def _put(path, content, message):
    try:
        sha = _github("GET", path).get("sha")  # existing file: GitHub needs its id to replace it
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        sha = None
    body = {"message": message, "content": base64.b64encode(content).decode()}
    if sha:
        body["sha"] = sha
    _github("PUT", path, body)


def to_github():
    """Daily backup. Returns the number of saves backed up, or None if GitHub backup isn't set up."""
    if not os.environ.get("BACKUP_GITHUB_TOKEN") or not os.environ.get("BACKUP_REPO"):
        return None
    content, n = make_file()
    today = dt.datetime.now(IST)
    _put("second-brain-latest.json.gz", content, f"Backup {today:%Y-%m-%d}: {n} saves")
    _put(f"monthly/second-brain-{today:%Y-%m}.json.gz", content, f"Monthly copy {today:%Y-%m}")
    return n
