-- Indexes for large vaults (tens of thousands of entries).
create index if not exists claim_predicate_status on claim(predicate_id, status);
create index if not exists claim_recorded on claim(recorded_at);
create index if not exists claim_evidence_source on claim_evidence(source_id);
create index if not exists event_status_start on event_detail(status, start_at);
create index if not exists event_notes on event_detail(notes_source_id);
create index if not exists commitment_from on commitment(from_id, status);
create index if not exists commitment_to on commitment(to_id, status);
create index if not exists entity_source_source on entity_source(source_id);
create index if not exists identifier_entity on identifier(entity_id);
create index if not exists entity_engagement on entity(engagement, status);
create index if not exists outreach_kind on outreach_state(kind, status);
