-- Phase 3: goals.
create table goal (
  id          text primary key,
  title       text not null,
  description text,
  keywords    text,                             -- comma-separated topics used to suggest people
  horizon     text,
  status      text not null default 'active',   -- active | done | dropped
  created_at  text not null default (datetime('now'))
);

create table goal_link (
  goal_id     text not null references goal(id),
  target_kind text not null,                    -- entity | tag
  target_id   text not null,
  relevance   real not null default 1.0,
  role        text,                             -- e.g. target, helper, advisor
  primary key (goal_id, target_kind, target_id)
);
