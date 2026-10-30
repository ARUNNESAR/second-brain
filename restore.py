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
        data = {"made_at": "GitHub backup folder", "count": len(saves), "saves": saves, "you": {}}
        you_dir = os.path.join(os.path.dirname(os.path.abspath(path)), "you")  # profile + journey, next to saves/
        for name in ("settings", "journal"):
            f = os.path.join(you_dir, f"{name}.json")
            if os.path.exists(f):
                data["you"][name] = json.load(open(f, encoding="utf-8"))
    else:
        data = json.loads(gzip.decompress(open(path, "rb").read()))
    db.setup()
    existing = {r[0] for r in db.all_saves()}
    missing = [s for s in data["saves"] if s["id"] not in existing]
    you = data.get("you") or {}
    settings = {k: v for k, v in (you.get("settings") or {}).items() if db.get_setting(k) is None}
    with db._connect() as c:
        journal_empty = c.execute("select count(*) from journal").fetchone()[0] == 0
    journal = (you.get("journal") or []) if journal_empty else []
    print(f"Backup from {data['made_at']}: {data['count']} saves. Missing from the database now: {len(missing)}.")
    print(f"Also missing: {len(settings)} settings (your profile etc.), {len(journal)} journey entries.")
    if not apply:
        print("Nothing changed. Add --apply to restore them.")
        return
    with db._connect() as c:
        for s in missing:
            c.execute(
                """insert into saves (id, created_at, source_type, source_url, title, topic, summary, key_points,
                                      content, status, embedding, proven, creator)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (s["id"], dt.datetime.fromisoformat(s["created_at"]), s["source_type"], s["source_url"], s["title"],
                 s["topic"], s["summary"], s["key_points"], s["content"], s["status"], db._vec(s["embedding"]),
                 s.get("proven", False), s.get("creator", "")))
        # keep new save numbers after the highest restored one
        c.execute("select setval(pg_get_serial_sequence('saves', 'id'), (select max(id) from saves))")
    for k, v in settings.items():
        db.set_setting(k, v)
    if journal:
        import brain  # only needed here: the journey's search vectors are rebuilt, not stored in the backup
        vecs = brain.embed_many([j["situation"] for j in journal])
        with db._connect() as c:
            for j, v in zip(journal, vecs):
                c.execute("""insert into journal (id, at, situation, advice, save_ids, embedding, result, outcome)
                             values (%s, %s, %s, %s, %s, %s, %s, %s)""",
                          (j["id"], dt.datetime.fromisoformat(j["at"]), j["situation"], j["advice"], j["save_ids"],
                           db._vec(v), j["result"], j["outcome"]))
            c.execute("select setval(pg_get_serial_sequence('journal', 'id'), (select max(id) from journal))")
    print(f"Restored {len(missing)} saves, {len(settings)} settings, {len(journal)} journey entries. "
          "Mentor moves for restored saves are rebuilt automatically overnight.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
    else:
        main(sys.argv[1], "--apply" in sys.argv)
