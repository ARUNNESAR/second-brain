"""Making sure nothing saved is ever 'forgotten':

- index_save(): every save is split into small chunks (its summary + every ~1000 characters of its text), each
  embedded, so a detail from minute 12 of a long video is as findable as the title.
- memory_test(): the bot quizzes itself on random saves and reports how many it finds in the top 3. A number,
  not a guess, for "does it remember?".
"""
import logging
import time

import brain
import db

log = logging.getLogger(__name__)

CHUNK = 1000      # characters per chunk
OVERLAP = 150     # chunks overlap a little so an idea split across the border isn't lost
MAX_CHUNKS = 80   # ~80,000 characters: enough for a 1-hour video's notes


def _chunks(title, topic, summary, points, content):
    head = f"{title}\nTopic: {topic}\n{summary or ''}\n" + "\n".join(f"- {p}" for p in points or [])
    pieces = [head]
    text = (content or "").strip()
    start = 0
    while text and start < len(text) and len(pieces) < MAX_CHUNKS:
        end = min(len(text), start + CHUNK)
        if end < len(text):  # end on a sentence or line break when possible
            cut = max(text.rfind(". ", start + CHUNK // 2, end), text.rfind("\n", start + CHUNK // 2, end))
            end = cut + 1 if cut > 0 else end
        pieces.append(f"{title}: " + text[start:end].strip())  # title kept so each chunk knows its source
        if end >= len(text):
            break
        start = max(end - OVERLAP, start + 1)
    return pieces


def index_save(save_id):
    """(Re)build the search chunks of one save. Call after any change to a save."""
    row = db.get(save_id)
    if not row:
        return 0
    _, _, _, _, title, topic, summary, points, content = row
    pieces = _chunks(title, topic, summary, points, content)
    db.replace_chunks(save_id, list(zip(pieces, brain.embed_many(pieces))))
    try:
        index_moves(save_id, row)
    except Exception:  # quota or a blocked answer: moves_missing() tries again later
        log.exception("moves for save %s failed", save_id)
    return len(pieces)


MOVE_FLOOR = 0.63  # a question whose best move (for his own words) scores below this isn't covered by his saves:
                   # tested Oct 31: off-topic questions <= 0.62, real situations >= 0.67
MOVE_PART = 20_000  # characters read per pass when finding moves (~15 minutes of a video)


def _parts(text):
    parts = []
    while len(text) > MOVE_PART * 1.3:
        cut = text.rfind("\n", MOVE_PART // 2, MOVE_PART)
        cut = cut if cut > 0 else MOVE_PART
        parts.append(text[:cut])
        text = text[cut:]
    return parts + [text]


def index_moves(save_id, row=None):
    """Break one save into its mentor moves ("when this happens -> do this -> say this") and make each findable."""
    row = row or db.get(save_id)
    if not row:
        return 0
    _, _, _, _, title, topic, summary, points, content = row
    moves, creator = [], ""
    for part in _parts(content or ""):  # long videos in parts: one pass over 90 minutes skims and misses most moves
        found = brain.extract_moves(title, topic, summary, points, part)
        moves += found.moves
        creator = creator or found.creator
    vecs = brain.embed_many([brain.move_text(m) for m in moves]) if moves else []
    db.replace_moves(save_id, [(m.situation, m.move, m.say, m.why, m.avoid, v) for m, v in zip(moves, vecs)])
    if creator:
        db.set_creator(save_id, creator, only_if_empty=True)
    return len(moves)


def creators_missing(batch=25):
    """Name the mentor of every save that has none yet (older saves). Returns how many were looked at."""
    items = db.saves_without_creator()
    for i in range(0, len(items), batch):
        part = items[i:i + batch]
        for (save_id, *_), name in zip(part, brain.creators_for(part)):
            db.set_creator(save_id, name or "-")  # "-" = looked, nobody named: don't ask again
    return len(items)


def moves_missing(limit=5):
    """Make moves for saves that don't have them yet (older saves, or ones that failed). Returns how many saves."""
    done = 0
    for i in db.saves_without_moves(limit):
        try:
            index_moves(i)
            done += 1
        except Exception as e:
            if "RESOURCE_EXHAUSTED" in str(e):
                break  # free quota used up: try again next time
            log.exception("moves for save %s failed", i)
            db.replace_moves(i, [])  # a save Gemini can't read: don't retry it forever
    return done


def index_missing():
    """Index every finished save that has no chunks yet (first run, or anything missed). Returns how many."""
    ids = db.unindexed_saves()
    for i in ids:
        index_save(i)
    return len(ids)


def search(question, limit=6):
    return db.hybrid_search(question, brain.embed(question, True), limit)


def mentor_context(question, saves=6, moves=15, profile=""):
    """What the mentor needs for one question. Steps, like the best second-brain tools do it:
    1. understand: rewrite his message into 3 clear situations (any wording, Tanglish too);
    2. search moves with his words AND the 3 rewrites, keep each move's best score;
    3. second look: re-rank the top 40 by "would this actually help him now";
    4. add his past similar situations and how they went.
    Returns (saves, moves, ctx) - saves: of the top moves first, then the best-matching saves;
    ctx: {"plan", "past", "mentors", "vector"}."""
    mentors = _mentors()
    try:
        plan = brain.plan_query(question, mentors)
    except Exception:  # quota or a bad reply: search with his own words only
        log.exception("query plan failed")
        plan = None
    queries = [question] + (plan.situations if plan else [])
    vecs = brain.embed_many(queries, for_query=True)
    v = vecs[0]
    mentor = plan.mentor if plan and plan.mentor in mentors else ""
    found = db.search_moves_multi(queries, vecs, 60, creator=mentor)
    direct = max((m[13] or 0 for m in found), default=0)  # best match for his OWN words: off-topic check
    pool, per = [], {}
    for m in found:
        per[m[0]] = per.get(m[0], 0) + 1
        if per[m[0]] <= 3 and len(pool) < 40:  # max 3 per save: one long podcast can't crowd out the rest
            pool.append(m)
    found_moves = brain.rerank(question, profile, pool, keep=moves) if len(pool) > 3 else pool
    rows = db.hybrid_search(question, v, saves)
    past = db.similar_journal(v)
    have = {r[0] for r in rows}
    extra = []
    for sid in dict.fromkeys(m[0] for m in found_moves[:6]):
        if sid not in have:
            row = db.get(sid)
            if row:
                extra.append(tuple(row) + (max(m[6] for m in found_moves if m[0] == sid),))
    return extra + rows, found_moves, {"plan": plan, "past": past, "mentors": mentors, "vector": v, "direct": direct}


_mentor_cache = {"at": 0, "names": []}


def _mentors():
    """Names of the mentors in his saves (cached 10 minutes: saves one database round trip per question)."""
    if time.time() - _mentor_cache["at"] > 600:
        _mentor_cache.update(at=time.time(), names=[n for n, _ in db.creators()])
    return _mentor_cache["names"]


def memory_test(n=20):
    """Ask one realistic question per random save; count how often that save comes back in the top 3."""
    found, misses = 0, []
    sample = db.random_done(n)
    for save_id, title, summary, points in sample:
        q = db.eval_question(save_id)
        if not q:
            q = brain.test_question(title, summary, points)
            db.set_eval_question(save_id, q)
        top = [r[0] for r in search(q, 3)]
        if save_id in top:
            found += 1
        else:
            misses.append((save_id, title, q))
    return found, len(sample), misses
