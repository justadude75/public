-- Phase 1 and 2: text index, links, imports, summaries, contact overrides, outreach state.

-- Text chunks for search. One row per passage of a source.
create table chunk (
  id        text primary key,
  source_id text not null references source(id),
  ordinal   integer not null,
  text      text not null
);
create index chunk_source on chunk(source_id);
create virtual table chunk_fts using fts5(text, chunk_id unindexed, tokenize = 'porter unicode61');

-- Search index over entity names and current facts, rebuilt per entity on change.
create virtual table entity_fts using fts5(entity_id unindexed, names, facts, tokenize = 'porter unicode61');

-- Embeddings (any target), stored as float32 bytes. Model recorded per row.
create table embedding (
  target_kind text not null,                 -- chunk | entity
  target_id   text not null,
  model       text not null,
  dims        integer not null,
  vector      blob not null,
  created_at  text not null default (datetime('now')),
  primary key (target_kind, target_id, model)
);

-- Sources attached to an entity or event (the link library, notes, documents).
create table entity_source (
  entity_id text not null references entity(id),
  source_id text not null references source(id),
  role      text not null default 'link',      -- link | note | document
  note      text,
  added_at  text not null default (datetime('now')),
  primary key (entity_id, source_id, role)
);

-- Imports: one batch per run; every item created is recorded for re-import and rollback.
create table import_batch (
  id          text primary key,
  importer    text not null,
  label       text,
  started_at  text not null default (datetime('now')),
  status      text not null default 'applied',  -- applied | rolled_back
  stats       text not null default '{}'
);

create table import_item (
  origin    text not null,                      -- stable key of the record's origin (note gid, vCard UID, ...)
  item_key  text not null,                      -- stable key of one derived item within the origin
  kind      text not null,                      -- entity | claim | event | commitment | source | identifier | link
  item_id   text not null,
  batch_id  text not null references import_batch(id),
  active    integer not null default 1,
  primary key (origin, item_key, kind)
);
create index import_item_batch on import_item(batch_id);

-- Markdown notes known to the importer.
create table md_note (
  gid          text primary key,
  path         text not null,
  content_hash text not null,
  note_type    text,
  entity_id    text references entity(id),
  last_imported text not null default (datetime('now'))
);
create index md_note_path on md_note(path);

-- Generated memory summaries.
create table summary (
  entity_id    text primary key references entity(id),
  text         text not null,
  generator    text not null,                   -- rules | model:<id>
  basis_hash   text not null,
  generated_at text not null default (datetime('now'))
);

-- "Next contact" set after a meeting overrides cadence until the next logged contact.
alter table relationship_profile add column next_contact_at text;
alter table relationship_profile add column next_contact_set_at text;

-- Snoozed or dismissed suggestions on the Today view.
create table outreach_state (
  kind       text not null,                     -- due | fact_change | brief | debrief | intro | inference
  key        text not null,                     -- entity id or a composite key
  status     text not null,                     -- snoozed | dismissed | done
  until      text,
  reason     text,
  updated_at text not null default (datetime('now')),
  primary key (kind, key)
);

-- Resolution decisions (merge, not the same, undo).
create table resolution_decision (
  id          text primary key,
  kind        text not null,                    -- merge | not_same | unmerge
  payload     text not null,
  decided_by  text not null default 'user',
  score       real,
  rationale   text,
  decided_at  text not null default (datetime('now')),
  reverted_by text references resolution_decision(id)
);
