"""Talks to the Supabase (Postgres) database: save notes, search them by meaning."""
import os
import re

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector

_SCHEMA = os.path.join(os.path.dirname(__file__), "schema.sql")


def _connect(vectors=True):
    # Built from the project ID + password in .env, so the user only had to paste the password.
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgres"):
        conn = psycopg.connect(url, connect_timeout=15)
    else:
        ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", url).group(1)
        conn = psycopg.connect(
            host=os.environ.get("DB_HOST", "aws-0-ap-northeast-2.pooler.supabase.com"),
            port=5432, dbname="postgres", user=f"postgres.{ref}",
            password=os.environ["DB_PASSWORD"].strip(), sslmode="require", connect_timeout=15,
        )
    if vectors:
        register_vector(conn)
    return conn


def _vec(e):
    return Vector(e) if e is not None else None


def setup():
    with _connect(vectors=False) as c, open(_SCHEMA, encoding="utf-8") as f:
        c.execute(f.read())


def ping():
    with _connect() as c:
        c.execute("select 1")


def add(source_type, source_url, title, topic, summary, key_points, content, embedding, status="done"):
    with _connect() as c:
        return c.execute(
            """insert into saves (source_type, source_url, title, topic, summary, key_points, content, embedding, status)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s) returning id""",
            (source_type, source_url, title, topic, summary, key_points, content, _vec(embedding), status),
        ).fetchone()[0]


def topics():
    with _connect() as c:
        return [r[0] for r in c.execute(
            "select topic from saves where topic is not null group by topic order by count(*) desc limit 30")]


def pending():
    with _connect() as c:
        return c.execute(
            "select id, source_type, source_url, content from saves where status = 'pending' order by id").fetchall()


def update_note(save_id, title, topic, summary, key_points, content, embedding):
    with _connect() as c:
        c.execute(
            """update saves set title=%s, topic=%s, summary=%s, key_points=%s, content=%s, embedding=%s, status='done'
               where id=%s""",
            (title, topic, summary, key_points, content, _vec(embedding), save_id),
        )


def find_url(url):
    with _connect() as c:
        return c.execute("select id, title, status from saves where source_url = %s order by status = 'done' desc limit 1", (url,)).fetchone()


def search(embedding, limit=6):
    """Closest saves by meaning. Returns rows with a similarity score (1 = same meaning)."""
    with _connect() as c:
        return c.execute(
            """select id, created_at, source_type, source_url, title, topic, summary, key_points, content,
                      1 - (embedding <=> %s) as score
               from saves where embedding is not null
               order by embedding <=> %s limit %s""",
            (Vector(embedding), Vector(embedding), limit),
        ).fetchall()


def get(save_id):
    with _connect() as c:
        return c.execute(
            """select id, created_at, source_type, source_url, title, topic, summary, key_points, content
               from saves where id = %s""", (save_id,)).fetchone()


def since(days):
    """Saves from the last N days (for the weekly recap)."""
    with _connect() as c:
        return c.execute(
            """select id, created_at, source_type, source_url, title, topic, summary, key_points
               from saves where status = 'done' and created_at > now() - make_interval(days => %s)
               order by created_at""", (days,)).fetchall()


def random_older(days):
    """One random save older than N days, to bring back an old lesson before it's forgotten."""
    with _connect() as c:
        return c.execute(
            """select id, created_at, source_type, source_url, title, topic, summary, key_points
               from saves where status = 'done' and created_at <= now() - make_interval(days => %s)
               order by random() limit 1""", (days,)).fetchone()


def recent(limit=10):
    with _connect() as c:
        return c.execute(
            "select id, created_at, source_type, title, topic, status from saves order by created_at desc limit %s",
            (limit,),
        ).fetchall()


def all_saves():
    with _connect() as c:
        return c.execute(
            """select id, created_at, source_type, source_url, title, topic, summary, key_points, content, status
               from saves order by created_at"""
        ).fetchall()


# ---------- queue: links wait here (status 'pending') and are saved one by one in the background ----------

QUEUE_TYPES = ("youtube", "instagram", "x", "linkedin", "reddit", "link")


def next_queued():
    """Oldest link that is due to be (re)tried."""
    with _connect() as c:
        return c.execute(
            """select id, source_type, source_url, content from saves
               where status = 'pending' and next_try <= now() and source_type = any(%s)
               order by next_try, id limit 1""", (list(QUEUE_TYPES),)).fetchone()


def postpone(save_id, minutes, max_attempts=6):
    """Try again later; after max_attempts mark it failed. Returns the new status."""
    with _connect() as c:
        return c.execute(
            """update saves set attempts = attempts + 1, next_try = now() + make_interval(mins => %s),
                      status = case when attempts + 1 >= %s then 'failed' else 'pending' end
               where id = %s returning status""", (minutes, max_attempts, save_id)).fetchone()[0]


def retry_all():
    """Put every pending/failed link back at the front of the queue."""
    with _connect() as c:
        return c.execute(
            """update saves set status = 'pending', attempts = 0, next_try = now()
               where status in ('pending', 'failed') and source_type = any(%s) returning id""",
            (list(QUEUE_TYPES),)).rowcount


def retry_one(save_id):
    with _connect() as c:
        c.execute("update saves set status = 'pending', attempts = 0, next_try = now() where id = %s", (save_id,))


def find(words, embedding, limit=8):
    """Saves matching some words: title/topic keyword matches first, then closest by meaning."""
    with _connect() as c:
        rows = c.execute(
            """select id, title, topic, created_at, status from saves
               where title ilike %s or topic ilike %s or source_url ilike %s
               order by created_at desc limit %s""", (f"%{words}%",) * 3 + (limit,)).fetchall()
        seen = {r[0] for r in rows}
        if len(rows) < limit:
            more = c.execute(
                """select id, title, topic, created_at, status from saves where embedding is not null
                   and 1 - (embedding <=> %s) > 0.55 order by embedding <=> %s limit %s""",
                (_vec(embedding), _vec(embedding), limit)).fetchall()
            rows += [r for r in more if r[0] not in seen][: limit - len(rows)]
        return rows


def topic_counts():
    with _connect() as c:
        return c.execute("""select topic, count(*) from saves where status = 'done' and topic is not null
                            group by topic order by count(*) desc limit 30""").fetchall()


def by_topic(topic, limit=15):
    with _connect() as c:
        return c.execute("""select id, title, topic, created_at, status from saves where topic = %s
                            order by created_at desc limit %s""", (topic, limit)).fetchall()


def set_status(save_id, status):
    with _connect() as c:
        c.execute("update saves set status = %s where id = %s", (status, save_id))


def queue_size():
    with _connect() as c:
        return c.execute("select count(*) from saves where status = 'pending'").fetchone()[0]


def pick_tip():
    """One older save for the daily reminder: the one shown least recently (never shown first)."""
    with _connect() as c:
        row = c.execute(
            """select id, created_at, source_type, source_url, title, topic, summary, key_points from saves
               where status = 'done' and created_at < now() - interval '12 hours'
               order by last_shown nulls first, random() limit 1""").fetchone()
        if row:
            c.execute("update saves set last_shown = now() where id = %s", (row[0],))
        return row


def add_correction(save_id, text):
    """Arun's correction becomes the first key point, so search and answers see it."""
    with _connect() as c:
        return c.execute(
            """update saves set key_points = array_prepend(%s, coalesce(key_points, '{}'))
               where id = %s returning title, topic, summary, key_points""",
            (f"CORRECTION by Arun: {text}", save_id)).fetchone()


def set_embedding(save_id, embedding):
    with _connect() as c:
        c.execute("update saves set embedding = %s where id = %s", (_vec(embedding), save_id))


def delete(save_id):
    with _connect() as c:
        return c.execute("delete from saves where id = %s returning title", (save_id,)).fetchone()
