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
