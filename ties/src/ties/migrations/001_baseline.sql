-- Phase 0 baseline schema. Later phases add tables in new migration files;
-- these tables follow Appendix A of docs/personal-graph-plan.md.

create table meta (
  key   text primary key,
  value text not null
);

-- ── Registries ─────────────────────────────────────────────────────────────
create table entity_type (
  id          text primary key,
  label       text not null,
  parent_id   text references entity_type(id),
  attr_schema text not null default '{}'
);

create table predicate (
  id            text primary key,
  label         text not null,
  reverse_label text,                         -- how the edge reads from the object's side
  domain_types  text not null,                -- JSON array of subject types, ["*"] = any
  range_kind    text not null,                -- entity | string | date | number
  range_types   text,                         -- JSON array of object types when range_kind = 'entity'
  cardinality   text not null default 'many', -- one | many
  temporal      integer not null default 1,
  symmetric     integer not null default 0,
  inverse_id    text references predicate(id),
  sensitivity   text not null default 'normal',
  sort          integer not null default 100
);

-- ── Evidence ───────────────────────────────────────────────────────────────
create table source (
  id           text primary key,
  kind         text not null,                 -- manual | note | markdown | import | web
  external_key text,
  uri          text,
  title        text,
  content      text,
  content_hash text not null,
  captured_at  text not null default (datetime('now')),
  published_at text,
  metadata     text not null default '{}'
);
create unique index source_external on source(kind, external_key) where external_key is not null;

-- ── Knowledge ──────────────────────────────────────────────────────────────
create table entity (
  id           text primary key,
  type_id      text not null references entity_type(id),
  display_name text not null,
  status       text not null default 'active', -- active | merged | archived
  merged_into  text references entity(id),
  engagement   text,                           -- ego | contact | acquaintance | reference
  attrs        text not null default '{}',
  created_at   text not null default (datetime('now'))
);
create index entity_type_status on entity(type_id, status);

create table entity_name (
  entity_id  text not null references entity(id),
  name       text not null,
  name_norm  text not null,                    -- lowercased, accents removed; used for search
  kind       text not null default 'primary',  -- primary | alias | former | nickname
  valid_from text,
  valid_to   text
);
create index entity_name_norm on entity_name(name_norm);
create index entity_name_entity on entity_name(entity_id);

create table identifier (
  entity_id text not null references entity(id),
  scheme    text not null,                     -- email | phone | url | handle | other
  value     text not null,                     -- normalized
  verified  integer not null default 0,
  source_id text references source(id),
  primary key (scheme, value, entity_id)
);

create table claim (
  id             text primary key,
  subject_id     text not null references entity(id),
  predicate_id   text not null references predicate(id),
  object_id      text references entity(id),
  value          text,                         -- JSON
  qualifiers     text not null default '{}',
  valid_from     text,
  valid_to       text,
  time_precision text,
  confidence     real not null default 1.0,
  status         text not null default 'accepted', -- proposed | accepted | rejected | disputed | superseded
  basis          text not null default 'observed', -- observed | told | heard | public | inferred
  asserted_by    text not null default 'user',
  recorded_at    text not null default (datetime('now')),
  superseded_by  text references claim(id),
  check ((object_id is null) <> (value is null))
);
create index claim_subject on claim(subject_id, predicate_id, status);
create index claim_object  on claim(object_id) where object_id is not null;

create table claim_evidence (
  claim_id   text not null references claim(id),
  source_id  text not null references source(id),
  span_start integer,
  span_end   integer,
  note       text,
  primary key (claim_id, source_id)
);

create table event_detail (
  entity_id         text primary key references entity(id),
  kind              text not null,
  status            text not null,             -- planned | occurred | cancelled | missed
  start_at          text not null,             -- YYYY-MM-DD or YYYY-MM-DDTHH:MM (local time)
  end_at            text,
  time_precision    text not null default 'day',
  location_id       text references entity(id),
  channel           text,
  initiated_by      text references entity(id),
  counts_as_contact integer not null default 1,
  notes_source_id   text references source(id)
);
create index event_start on event_detail(start_at);

create table participation (
  event_id  text not null references entity(id),
  entity_id text not null references entity(id),
  role      text not null default 'attendee',
  primary key (event_id, entity_id, role)
);
create index participation_entity on participation(entity_id);

create table tag (
  id        text primary key,
  namespace text not null default '',
  name      text not null,
  parent_id text references tag(id),
  unique (namespace, name)
);

create table tagging (
  tag_id      text not null references tag(id),
  target_kind text not null,                   -- entity | event | claim | source
  target_id   text not null,
  primary key (tag_id, target_kind, target_id)
);
create index tagging_target on tagging(target_kind, target_id);

-- ── Personal relationship layer ────────────────────────────────────────────
create table relationship_profile (
  entity_id        text primary key references entity(id),
  tier             text not null default 'active', -- inner | close | active | extended | wider
  cadence_days     integer,
  closeness_self   integer,
  how_met          text,
  how_met_event_id text references entity(id),
  introduced_by    text references entity(id)
);

create table commitment (
  id              text primary key,
  from_id         text not null references entity(id),
  to_id           text references entity(id),
  kind            text not null default 'promise', -- promise | ask | offer
  description     text not null,
  due_at          text,
  status          text not null default 'open',    -- open | done | dropped
  source_event_id text references entity(id),
  created_at      text not null default (datetime('now')),
  done_at         text
);
create index commitment_status on commitment(status, due_at);

-- ── Audit ──────────────────────────────────────────────────────────────────
create table audit_log (
  id     integer primary key,
  at     text not null default (datetime('now')),
  actor  text not null,
  action text not null,                        -- write | read | egress
  target text,
  detail text
);
