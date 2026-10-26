"""Making sure nothing saved is ever 'forgotten':

- index_save(): every save is split into small chunks (its summary + every ~1000 characters of its text), each
  embedded, so a detail from minute 12 of a long video is as findable as the title.
- memory_test(): the bot quizzes itself on random saves and reports how many it finds in the top 3. A number,
  not a guess, for "does it remember?".
"""
import brain
import db

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
    return len(pieces)


def index_missing():
    """Index every finished save that has no chunks yet (first run, or anything missed). Returns how many."""
    ids = db.unindexed_saves()
    for i in ids:
        index_save(i)
    return len(ids)


def search(question, limit=6):
    return db.hybrid_search(question, brain.embed(question, True), limit)


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
