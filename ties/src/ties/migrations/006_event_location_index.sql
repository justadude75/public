-- Looking up events at a place (profile timelines for places).
create index if not exists event_location on event_detail(location_id);
-- The Review badge counts open suggestions on every page.
create index if not exists claim_open on claim(status) where status in ('proposed','disputed');
