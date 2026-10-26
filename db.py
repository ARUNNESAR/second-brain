"""Talks to the Supabase (Postgres) database: save notes, search them by meaning."""
import os
import re
import threading

import psycopg
import psycopg.conninfo
from psycopg_pool import ConnectionPool
from pgvector import Vector
from pgvector.psycopg import register_vector

_SCHEMA = os.path.join(os.path.dirname(__file__), "schema.sql")


def _conninfo():
    # Built from the project ID + password in .env, so the user only had to paste the password.
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgres"):
        return url
    ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", url).group(1)
    return psycopg.conninfo.make_conninfo(
        host=os.environ.get("DB_HOST", "aws-0-ap-northeast-2.pooler.supabase.com"),
        port=5432, dbname="postgres", user=f"postgres.{ref}",
        password=os.environ["DB_PASSWORD"].strip(), sslmode="require", connect_timeout=15)


def _configure(conn):
    register_vector(conn)
    conn.execute("set timezone to 'Asia/Kolkata'")  # dates shown to Arun are India time
    conn.commit()


_pool = None
_pool_lock = threading.Lock()


def _connect(vectors=True):
    """A connection from a small shared pool. Supabase's free pooler allows only 15 clients at once,
    so we keep at most 6 open and make extra requests wait their turn instead of failing."""
    global _pool
    if not vectors:  # only for setup, before the vector type may exist
        return psycopg.connect(_conninfo())
    with _pool_lock:
        if _pool is None:
            _pool = ConnectionPool(_conninfo(), min_size=1, max_size=int(os.environ.get("DB_POOL_SIZE", "6")),
                                   configure=_configure, check=ConnectionPool.check_connection,
                                   timeout=60, open=True)
    return _pool.connection()


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
        row = c.execute(
            """update saves set attempts = attempts + 1, next_try = now() + make_interval(mins => %s),
                      status = case when attempts + 1 >= %s then 'failed' else 'pending' end
               where id = %s returning status""", (minutes, max_attempts, save_id)).fetchone()
        return row[0] if row else None  # None: the save was deleted meanwhile


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


def _like(words):
    """ILIKE pattern that treats % and _ as normal characters."""
    return "%" + words.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def find(words, embedding, limit=8):
    """Saves matching some words: title/topic keyword matches first, then closest by meaning."""
    with _connect() as c:
        rows = c.execute(
            """select id, title, topic, created_at, status from saves
               where title ilike %s or topic ilike %s or source_url ilike %s
               order by created_at desc limit %s""", (_like(words),) * 3 + (limit,)).fetchall()
        seen = {r[0] for r in rows}
        if len(rows) < limit:
            # meaning matches only when one clearly stands out (nonsense words score ~0.62 against everything)
            more = c.execute(
                """select id, title, topic, created_at, status, 1 - (embedding <=> %s) as score from saves
                   where embedding is not null order by embedding <=> %s limit %s""",
                (_vec(embedding), _vec(embedding), limit)).fetchall()
            if more and more[0][5] >= 0.65:
                bar = max(0.62, more[0][5] - 0.05)
                rows += [r[:5] for r in more if r[5] >= bar and r[0] not in seen][: limit - len(rows)]
        return rows


def topic_counts():
    with _connect() as c:
        return c.execute("""select topic, count(*) from saves where status = 'done' and topic is not null
                            group by topic order by count(*) desc limit 30""").fetchall()


def by_topic(topic, limit=15):
    with _connect() as c:
        # topic may be cut short (Telegram buttons hold 64 bytes), so match it as a prefix
        return c.execute("""select id, title, topic, created_at, status from saves where topic like %s
                            order by created_at desc limit %s""", (_like(topic)[1:], limit)).fetchall()


def similar(embedding, exclude_id, threshold=0.9):
    """An existing save that means almost the same thing (likely a duplicate), or None."""
    with _connect() as c:
        return c.execute(
            """select id, title from saves where id <> %s and embedding is not null
               and 1 - (embedding <=> %s) > %s order by embedding <=> %s limit 1""",
            (exclude_id, _vec(embedding), threshold, _vec(embedding))).fetchone()


def get_setting(key, default=None):
    with _connect() as c:
        row = c.execute("select value from settings where key = %s", (key,)).fetchone()
        return row[0] if row else default


def set_setting(key, value):
    with _connect() as c:
        c.execute("insert into settings (key, value) values (%s, %s) on conflict (key) do update set value = excluded.value",
                  (key, value))


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


# ---------- chunks: find any sentence of any save ----------

def replace_chunks(save_id, chunks):
    """chunks: list of (text, embedding). Replaces the save's old chunks in one go."""
    with _connect() as c:
        c.execute("delete from chunks where save_id = %s", (save_id,))
        for i, (text, vec) in enumerate(chunks):
            c.execute("insert into chunks (save_id, idx, text, embedding) values (%s, %s, %s, %s)",
                      (save_id, i, text, _vec(vec)))


def unindexed_saves():
    with _connect() as c:
        return [r[0] for r in c.execute(
            """select s.id from saves s where s.status = 'done'
               and not exists (select 1 from chunks k where k.save_id = s.id) order by s.id""")]


CHUNK_WEIGHT = 0.98  # a chunk must match about as well as a summary to lift a save (tuned on 66 test questions)
WORD_BONUS = 0.01    # small boost when the exact words appear (names, tools, terms)
PROVEN_BONUS = 0.02  # tips Arun tried and marked "worked for me"


def hybrid_search(query, embedding, limit=6, pool=60):
    """Best saves for a question. Each save is scored by the BETTER of:
      - how well its summary matches (by meaning), and
      - how well its single best chunk matches (finds details deep inside long videos),
    plus a small bonus if the exact words appear in it. Tested: details found 90% vs 65% before,
    summary-level questions unchanged. Same row shape as search(); 'content' = the best-matching passages."""
    v = _vec(embedding)
    with _connect() as c:
        summary = dict(c.execute(
            """select id, 1 - (embedding <=> %s) from saves where embedding is not null and status = 'done'
               order by embedding <=> %s limit %s""", (v, v, pool)).fetchall())
        best_chunk, chunk_text = {}, {}
        for sid, text, sim in c.execute(
                "select save_id, text, 1 - (embedding <=> %s) from chunks order by embedding <=> %s limit %s",
                (v, v, pool * 3)):
            if sid not in best_chunk:
                best_chunk[sid], chunk_text[sid] = sim, text
        word_text = {}
        for sid, text in c.execute(
                """with q as (select to_tsquery('english', replace(plainto_tsquery('english', %s)::text, '&', '|')) as q)
                   select save_id, text from chunks, q where tsv @@ q.q
                   order by ts_rank_cd(tsv, q.q, 32) desc limit 30""", (query,)):
            word_text.setdefault(sid, text)
        score = {sid: max(summary.get(sid, 0), best_chunk.get(sid, 0) * CHUNK_WEIGHT)
                      + (WORD_BONUS if sid in word_text else 0)
                 for sid in set(summary) | set(best_chunk)}
        for (sid,) in c.execute("select id from saves where proven and id = any(%s)", (list(score),)):
            score[sid] += PROVEN_BONUS  # tips Arun marked "worked for me" come first
        top = sorted(score, key=score.get, reverse=True)[:limit]
        if not top:
            return []
        rows = {r[0]: r for r in c.execute(
            """select id, created_at, source_type, source_url, title, topic, summary, key_points
               from saves where id = any(%s) and status = 'done'""", (top,))}
    out = []
    for sid in top:
        if sid in rows:
            passages = list(dict.fromkeys(p for p in (chunk_text.get(sid), word_text.get(sid)) if p))
            out.append(tuple(rows[sid]) + ("\n…\n".join(passages), float(score[sid])))
    return out

def eval_question(save_id):
    with _connect() as c:
        r = c.execute("select question from eval_questions where save_id = %s", (save_id,)).fetchone()
        return r[0] if r else None


def set_eval_question(save_id, question):
    with _connect() as c:
        c.execute("insert into eval_questions values (%s, %s) on conflict (save_id) do update set question = excluded.question",
                  (save_id, question))


def random_done(n):
    with _connect() as c:
        return c.execute("""select id, title, summary, key_points from saves where status = 'done'
                            order by random() limit %s""", (n,)).fetchall()


def append_extra(save_id, label, text, point=None):
    """Add a section (screen text, resource, attachment) to a save. Returns the updated row for re-embedding."""
    with _connect() as c:
        return c.execute(
            """update saves set content = coalesce(content, '') || %s,
                      key_points = case when %s::text is null then key_points
                                        else array_append(coalesce(key_points, '{}'), %s::text) end
               where id = %s returning id, title, topic, summary, key_points, content, source_type, source_url""",
            (f"\n\n--- {label} ---\n{text}", point, point, save_id)).fetchone()


def add_toget(save_id, what, how):
    with _connect() as c:
        if not c.execute("select 1 from toget where save_id = %s and not done", (save_id,)).fetchone():
            c.execute("insert into toget (save_id, what, how) values (%s, %s, %s)", (save_id, what, how))


def open_toget():
    with _connect() as c:
        return c.execute(
            """select t.id, t.save_id, t.what, t.how, s.title from toget t join saves s on s.id = t.save_id
               where not t.done order by t.created_at""").fetchall()


def finish_toget(save_id):
    with _connect() as c:
        return c.execute("update toget set done = true where save_id = %s and not done", (save_id,)).rowcount


def set_tg_msg(save_id, msg_id):
    with _connect() as c:
        c.execute("update saves set tg_msg_id = %s where id = %s", (msg_id, save_id))


def by_tg_msg(msg_id):
    with _connect() as c:
        r = c.execute("select id from saves where tg_msg_id = %s", (msg_id,)).fetchone()
        return r[0] if r else None


def tg_msg_of(save_id):
    with _connect() as c:
        r = c.execute("select tg_msg_id from saves where id = %s", (save_id,)).fetchone()
        return r[0] if r else None


def mark_proven(save_id):
    with _connect() as c:
        r = c.execute("update saves set proven = true where id = %s returning title", (save_id,)).fetchone()
        return r[0] if r else None


def proven_ids(ids):
    with _connect() as c:
        return {r[0] for r in c.execute("select id from saves where proven and id = any(%s)", (list(ids),))}


def playbook_rows(topic, embedding, limit=25):
    """Saves for a topic's playbook: everything tagged with the topic, plus the closest saves by meaning."""
    v = _vec(embedding)
    with _connect() as c:
        return c.execute(
            """select id, created_at, source_type, source_url, title, topic, summary, key_points, content, proven
               from saves where status = 'done' and (topic ilike %s or id in (
                   select id from saves where status = 'done' and embedding is not null
                   order by embedding <=> %s limit 12))
               order by proven desc, created_at limit %s""", (f"%{topic}%", v, limit)).fetchall()


def save_playbook(topic, text, n):
    with _connect() as c:
        c.execute("""insert into playbooks (topic, text, save_count) values (%s, %s, %s)
                     on conflict (topic) do update set text = excluded.text, save_count = excluded.save_count,
                     updated_at = now()""", (topic, text, n))


def get_playbook(topic):
    with _connect() as c:
        return c.execute("select topic, text, save_count, updated_at from playbooks where topic ilike %s",
                         (topic,)).fetchone()


def list_playbooks():
    with _connect() as c:
        return c.execute("select topic, save_count, updated_at from playbooks order by updated_at desc").fetchall()


def add_feedback(question, answer, helpful, save_ids):
    with _connect() as c:
        c.execute("insert into feedback (question, answer, helpful, save_ids) values (%s, %s, %s, %s)",
                  (question, answer, helpful, save_ids))


def all_for_backup():
    with _connect() as c:
        return c.execute(
            """select id, created_at, source_type, source_url, title, topic, summary, key_points, content, status,
                      embedding from saves order by id""").fetchall()


def delete(save_id):
    with _connect() as c:
        return c.execute("delete from saves where id = %s returning title", (save_id,)).fetchone()
