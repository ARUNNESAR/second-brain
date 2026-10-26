-- Second Brain memory. Safe to run more than once.
create extension if not exists vector;

create table if not exists saves (
    id          bigserial primary key,
    created_at  timestamptz not null default now(),
    source_type text not null,          -- youtube | image | link | note
    source_url  text,
    title       text,
    topic       text,
    summary     text,
    key_points  text[],
    content     text,                   -- full transcript / text, kept so nothing is lost
    status      text not null default 'done',  -- done | pending (saved but not processed yet)
    embedding   vector(768)
);

create index if not exists saves_embedding_idx on saves using hnsw (embedding vector_cosine_ops);
create index if not exists saves_created_idx on saves (created_at desc);

-- Queue + daily tip (added Oct 2026)
alter table saves add column if not exists attempts int not null default 0;
alter table saves add column if not exists next_try timestamptz not null default now();
alter table saves add column if not exists last_shown timestamptz;

-- Small settings (e.g. reminders on/off)
create table if not exists settings (key text primary key, value text);

-- Resources still to get ("comment X and I'll DM you…")
create table if not exists toget (
    id         bigserial primary key,
    save_id    bigint references saves(id) on delete cascade,
    what       text,
    how        text,
    done       boolean not null default false,
    created_at timestamptz not null default now()
);

-- Search chunks: every save split into small pieces, each findable by meaning (embedding) AND exact words (tsv)
create table if not exists chunks (
    id        bigserial primary key,
    save_id   bigint not null references saves(id) on delete cascade,
    idx       int not null,
    text      text not null,
    embedding vector(768),
    tsv       tsvector generated always as (to_tsvector('english', text)) stored
);
create index if not exists chunks_save_idx on chunks (save_id);
create index if not exists chunks_embedding_idx on chunks using hnsw (embedding vector_cosine_ops);
create index if not exists chunks_tsv_idx on chunks using gin (tsv);

-- Memory test: one realistic question per save, reused every week
create table if not exists eval_questions (
    save_id  bigint primary key references saves(id) on delete cascade,
    question text not null
);

-- 👍 / 👎 on answers: which saves actually helped (for future ranking)
create table if not exists feedback (
    id         bigserial primary key,
    created_at timestamptz not null default now(),
    question   text,
    answer     text,
    helpful    boolean,
    save_ids   bigint[]
);
