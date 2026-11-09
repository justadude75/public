-- Phase 4: research vault.
alter table source add column author text;
alter table source add column source_type text;           -- paper | article | filing | court | dataset | web | interview | book | other
alter table source add column reliability text;           -- Admiralty A-F (source reliability)
alter table source add column credibility integer;        -- Admiralty 1-6 (information credibility)
alter table source add column independent_of text;        -- id of a source this one copies (not independent)

create table source_blob (
  source_id    text primary key references source(id),
  content_type text not null,
  data         blob not null,
  sha256       text not null,
  fetched_at   text not null default (datetime('now')),
  archive_url  text
);

create table project (
  id          text primary key,
  title       text not null,
  description text,
  status      text not null default 'active',
  created_at  text not null default (datetime('now'))
);

create table project_item (
  project_id  text not null references project(id),
  target_kind text not null,                               -- entity | source | event
  target_id   text not null,
  added_at    text not null default (datetime('now')),
  primary key (project_id, target_kind, target_id)
);

create table hypothesis (
  id          text primary key,
  project_id  text not null references project(id),
  title       text not null,
  description text,
  status      text not null default 'open',               -- open | supported | rejected
  created_at  text not null default (datetime('now'))
);

create table evidence_item (
  id          text primary key,
  project_id  text not null references project(id),
  target_kind text not null,                               -- claim | source | note
  target_id   text,
  label       text not null,
  created_at  text not null default (datetime('now'))
);

create table hypothesis_rating (
  hypothesis_id text not null references hypothesis(id),
  evidence_id   text not null references evidence_item(id),
  stance        text not null,                             -- CC | C | N | I | II
  note          text,
  primary key (hypothesis_id, evidence_id)
);

create table cross_vault_link (
  local_entity_id  text not null references entity(id),
  other_vault      text not null,
  other_entity_id  text not null,
  other_name       text,
  confirmed_at     text not null default (datetime('now')),
  primary key (local_entity_id, other_vault, other_entity_id)
);
