r"""Put saves back from a backup file into the database.

    .venv\\Scripts\\python restore.py second-brain-backup-2026-10-23.json.gz          (shows what it would do)
    .venv\\Scripts\\python restore.py second-brain-backup-2026-10-23.json.gz --apply  (actually restores)
    .venv\\Scripts\\python restore.py path\to\second-brain-backup\saves --apply       (from the GitHub backup folder)

Only saves whose number (#id) is missing from the database are added; existing saves are never touched.
"""
import datetime as dt
import gzip
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
import db  # noqa: E402


def main(path, apply=False):
    if os.path.isdir(path):  # the GitHub backup: one JSON file per save
        saves = [json.load(open(os.path.join(path, f), encoding="utf-8")) for f in sorted(os.listdir(path)) if f.endswith(".json")]
        data = {"made_at": "GitHub backup folder", "count": len(saves), "saves": saves}
    else:
        data = json.loads(gzip.decompress(open(path, "rb").read()))
    db.setup()
    existing = {r[0] for r in db.all_saves()}
    missing = [s for s in data["saves"] if s["id"] not in existing]
    print(f"Backup from {data['made_at']}: {data['count']} saves. Missing from the database now: {len(missing)}.")
    if not apply:
        print("Nothing changed. Add --apply to restore them.")
        return
    with db._connect() as c:
        for s in missing:
            c.execute(
                """insert into saves (id, created_at, source_type, source_url, title, topic, summary, key_points,
                                      content, status, embedding)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (s["id"], dt.datetime.fromisoformat(s["created_at"]), s["source_type"], s["source_url"], s["title"],
                 s["topic"], s["summary"], s["key_points"], s["content"], s["status"], db._vec(s["embedding"])))
        # keep new save numbers after the highest restored one
        c.execute("select setval(pg_get_serial_sequence('saves', 'id'), (select max(id) from saves))")
    print(f"Restored {len(missing)} saves.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
    else:
        main(sys.argv[1], "--apply" in sys.argv)
