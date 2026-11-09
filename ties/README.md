# ties

A local-first relationship graph for personal use, which can also run as a standalone research vault. It implements every phase of [`docs/personal-graph-plan.md`](../docs/personal-graph-plan.md). See [`BUILD_STATUS.md`](BUILD_STATUS.md) for what is built and its known limits.

Everything lives in one SQLite file per vault, which can be encrypted with SQLCipher. The app runs in a browser tab on `127.0.0.1`. It makes no network calls unless you turn on a feature that needs one, and each such feature is off by default.

## Install on your computer

You need Python 3.10 or newer. From a copy of this repository:

| System | Command | What you get |
|---|---|---|
| macOS | `cd ties && ./scripts/install.sh` | `~/Applications/ties.command` (double-click it, or drag it to the Dock) |
| Linux | `cd ties && ./scripts/install.sh` | "ties" in your applications menu |
| Windows | `cd ties; powershell -ExecutionPolicy Bypass -File scripts\install.ps1` | `ties.bat` on your desktop |

The installer:
- sets up the app with encryption and fast search;
- creates an encrypted vault at `~/.ties/personal.vault` on first run, after asking you for a passphrase;
- adds a launcher that opens ties in your browser.

Running it again updates the app and leaves your vault alone.

Manual install:

```bash
cd ties
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[encryption,fast]'     # [encryption] = SQLCipher, [fast] = numpy for meaning search
ties init --encrypt                     # creates ~/.ties/personal.vault and asks for a passphrase
ties serve --open                       # http://127.0.0.1:8765
```

To try it first with sample data:

```bash
ties --vault /tmp/demo.vault demo && ties --vault /tmp/demo.vault serve --open
ties --vault /tmp/research.vault demo --research && ties --vault /tmp/research.vault serve --port 8766 --open
```

## Personal vault

| Area | What it does |
|---|---|
| **Today** | Lists who to reach out to, ranked by how overdue they are, their tier, relationship strength and links to your goals. Each item shows its reason and can be snoozed. Also shows follow-ups due, the next 14 days (including birthdays), briefs for meetings in the next few days, prompts to add notes after meetings, job/city/title changes among your contacts, and planned events that have passed so you can confirm whether they happened. |
| **Entry** | Forms for people, organizations and places. While you type a name it shows possible duplicates (nicknames, initials, similar spellings, same email). |
| **Facts and relationships** | Each fact records how you know it (observed, told, heard, public, inferred), a confidence, and the dates it was true. Corrections supersede older facts and never overwrite them, and the full history is kept. Relationships read correctly from both sides. |
| **Debrief** | One form after a meeting for the summary, facts learned, your inferences, follow-ups (yours and theirs), people mentioned, links shared, and when to be in touch next. |
| **Capture** | Free text plus structured lines (`- [[Sarah]] interest:: sailing`, `- [ ] send deck due:: 2026-11-12`). With a local model configured, it can also extract facts from the free text; those arrive as suggestions for review. |
| **Memory** | A summary on each profile, built from recorded facts or written by your local model. Profiles also show what changed since you last spoke. |
| **Inferences** | Rules suggest connections: people at the same small events, people introduced by the same person, and people at the same small organization at the same time. You confirm or reject each one in Review, and a rejection is remembered. |
| **Search** | Keyword search over people, facts and notes, plus meaning-based search when an embedding model is configured. |
| **Import** | Markdown notes (with the `gid:` ID line written back), vCard, iCalendar, CSV (LinkedIn and Google exports are matched automatically) and JSON restore. Every import shows a preview first, can be re-run safely, keeps conflicts with your hand-entered facts for review, and can be undone as a whole batch. |
| **Duplicates** | Possible duplicates are listed in Review. Merging keeps everything from both records and can be undone exactly. Marking a pair "not the same" stops it being suggested again. |
| **Graph** | An explorer with centering, suggested connections shown dashed, and "met together" links. It also finds the strongest introduction paths to anyone in your network. |
| **Insights** | Introduction ideas (one person's "looking for" matched to another's "can offer"), people who bridge separate groups, clusters, goals, and a weekly review. |
| **Strategy** | Asks a model for advice on your network through the privacy gateway. People appear only as P1, P2…; names, contact details, notes and sensitive facts are removed; you see the exact text before it is sent; every request is logged. Sending to an outside model is off by default. |

## Research vault

Create one with `ties init --research`, or try the sample with `ties demo --research`.

- **Projects** group entities, sources and hypotheses for one line of research. Entities are shared across projects.
- **Sources** carry Admiralty grades (reliability A–F, credibility 1–6) and can be marked as copies of another source. Claims cite sources with quotes. Each claim shows its number of independent sources and its best grade.
- **Contradictions:** single-valued facts are kept side by side when sources disagree, and the contradictions page lists them with their support.
- **As-of views** on every profile: what you had recorded on a given date, and what was true on a given date.
- **Hypotheses:** a competing-hypotheses matrix that ranks hypotheses by the least evidence against them and flags evidence that rates differently across hypotheses.
- **Exports:** GraphML, GEXF, CSV and Follow the Money, for a whole vault or one project.
- **Bulk import from free sources** that need no account or key (turn on network access for the vault first):
  - **Crossref:** papers, authors with ORCID, funders and citations. Free-text author affiliations are matched to institutions through ROR's affiliation matcher.
  - **ROR:** research institutions with their parent and child organizations, locations and Wikidata ids.
  - **GLEIF:** companies by name or LEI, with their registered address, parent company and subsidiaries.
  - **Wikidata:** search by name, or import by item id, with employers, education, positions, family, ownership and dates.

  Requests identify the tool, are spaced out per service, and back off when a service asks them to slow down. OpenAlex is still supported but now needs a free API key.
- **Batch duplicate resolution** scores pairs on names, identifiers and shared connections. Pairs with different ORCID, Wikidata, ROR or DOI values are never merged. `ties resolve --auto 0.95` merges the surest pairs, and every merge can be undone.
- **Linking vaults:** `ties link other.vault` links people in this vault to the same people in another vault. A linked profile shows the other vault's facts read-only, and nothing is copied between vaults.

## AI, all optional

- **Local model:** set an OpenAI-compatible address on this machine in Settings (Ollama, llama.cpp server, LM Studio and others). Addresses outside this machine are refused here. The model is used for capture extraction, memory summaries, embeddings for meaning-based search, and the weekly-review summary.
- **Agents:** run `ties mcp` to start a local MCP server on stdin/stdout. Agents can read freely. Their writes (`propose_fact`, `propose_follow_up`) wait in Review, and every call is audited.
- **Outside model:** used only through the privacy gateway. It is off by default, and the API key is read from `TIES_GATEWAY_KEY`.

## Commands

| Command | What it does |
|---|---|
| `ties init [--encrypt] [--research]` | Create a vault |
| `ties serve [--port N] [--open]` | Run the app on localhost |
| `ties demo [--research]` | Create a vault with sample data |
| `ties import markdown\|vcard\|ical\|csv <path> [--apply] [--contacts]` | Import (preview unless you add `--apply`) |
| `ties research crossref\|ror\|gleif\|wikidata "<query>" [--id ID]` | Fetch research records from free sources (`--allow-network`, `--apply`) |
| `ties bench --people N` | Build a synthetic vault and time the main pages |
| `ties resolve [--auto 0.95]` | Score duplicates; optionally merge the surest |
| `ties link <other.vault> [--yes]` | Link entries to another vault |
| `ties mcp [--agent NAME]` | Run the local MCP server |
| `ties export --out f.json`, `ties restore f.json`, `ties backup <dir>` | Export, restore into a new vault, copy the encrypted file |

Use `--vault PATH` or `$TIES_VAULT` to choose a vault. `$TIES_PASSPHRASE` supplies a passphrase without a prompt (needed for `ties mcp` with an encrypted vault).

## Scale

`ties bench --people N --vault /tmp/bench.vault` builds a synthetic vault and times the main pages. Results on the build machine (one CPU core in use, pure Python, no numpy):

| | 2,000 people | 20,000 people |
|---|---|---|
| Vault contents | 517 contacts, 3,152 events, 11,390 facts | 4,978 contacts, 29,641 events, 113,820 facts |
| Build plus full search index | 0.4 s | 4 s |
| Today | 0.07 s | 0.19 s |
| Network list (100 per page) | 0.02 s | 0.10 s |
| Profile | 0.07 s | 0.11 s |
| Search | 0.01 s | 0.02 s |
| Review, Insights, Weekly | ≤ 0.03 s | ≤ 0.1 s |
| Graph centered on someone | 0.1 s | about 1 s on first view |
| Background warm-up after a change | 1.5 s | 15 s |
| Peak memory | 120 MB | 510 MB |

How it stays fast:
- **Caching:** expensive results (contact status, duplicate candidates, introduction matches, bridges and clusters, the whole graph) are cached until the vault changes. This includes changes made by another process, such as the MCP server.
- **Background warm-up:** while `ties serve` runs, a background job recomputes those results after each change, so pages read from cache.
- **Bulk queries:** lists, timelines and profiles fetch their details in batches. Long lists are paged.
- **Search index:** rows are keyed by row number, and a full rebuild is a few bulk statements (280 s down to 1 s at 20,000 people).
- **Bridges analysis:** organizations are their own nodes rather than links between every pair of colleagues, and above 600 nodes the analysis uses a sample.
- **Introduction matching:** rare shared words count for more, and each request keeps only its best few matches.
- **Inference rules:** each rule adds at most 200 suggestions per run, with your tracked contacts first. Names you only know of (for example paper authors) don't trigger rules. Review shows 100 per group and can reject a whole group at once.
- **Database:** WAL mode with normal sync, a 64 MB page cache and indexes for the common lookups.

## Tests

```bash
pip install -e '.[dev,encryption]'
pytest          # 98 tests
```
