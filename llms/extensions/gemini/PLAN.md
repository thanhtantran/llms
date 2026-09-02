# Implementation readiness — metadata + ingest

Short answer: **no, not yet.** The design is settled; four things aren't, and each of them is
cheaper to resolve now than to discover halfway through. Two I can't resolve without your API key
or your call; two are work items nobody has scheduled.

---

## Blockers

### B1 — The filter grammar is still unverified 🔴

`categoryPath:"guides"`, `versions:"v8"`, `updatedAt > …`, `AND`/`OR`/`NOT` — all of it rests on
AIP-160 being implemented, and Google's docs demonstrate only `author="Robert Graves"`.

This isn't a detail to find out during implementation, because **it changes the schema**:

| If `:` on `stringListValue` fails | Consequence |
|---|---|
| `categoryPath` | Drop it. Flat single-segment `category`, full path in a local-only column, no subtree filtering, tree UI becomes a browse-only affordance. |
| `versions` | Becomes `versionMin`/`versionMax` numerics, which breaks on `8.1.2` alongside `2024.3`. |
| `tags` | Loses remote filterability; becomes local-only. |

If numeric comparison fails, `updatedAt` stops being a filter and is display-only.

**`probe_filters.py` is written and ready to run** — it creates a scratch store, uploads three
fixture documents covering every value type, runs ten filter expressions, and reports which
actually work. Costs three embeddings and cleans up after itself.

```bash
GOOGLE_API_KEY=… python3 probe_filters.py
```

I can't run it from here. Everything below assumes the full grammar works; the fallbacks above are
what changes if it doesn't.

### B2 — `uniq_filestoreid_hash` blocks the new identity model 🔴

The `document` table carries:

```sql
CONSTRAINT uniq_filestoreid_hash UNIQUE (filestoreId, hash)
```

Under source-key identity that's wrong, and it will fail on real corpora on day one. Two documents
with identical content at different source keys are entirely legitimate — the same `LICENSE.md` in
two folders, a shared boilerplate page under two sections, an FAQ duplicated across products. Today
the second one silently fails to insert.

The problem is that `add_missing_columns()` only does `ALTER TABLE … ADD COLUMN`. **SQLite cannot
drop a table constraint**, so this needs a genuine migration: create the new table, copy, swap,
recreate indexes — inside a transaction, with the existing writer connection.

That's the first piece of real migration machinery in this extension, and it has to be right
because it touches everyone's existing data. Budget for it explicitly rather than discovering it
when the first duplicate file appears.

The replacement is a unique **index** (which SQLite *can* add later):

```sql
CREATE UNIQUE INDEX uniq_document_source_key
  ON document(filestoreId, IFNULL(sourceId,0), sourceKey);
```

### B3 — The upload worker can't carry an import 🔴

`UploadWorker` is one thread, one document at a time, polling `time.sleep(5)`. A 1,500-document
import is hours, most of it idle.

I listed the worker fixes as §2.6 of the main doc and then sequenced ingest as if they were
parallel. They aren't — ingest is unusable without them, so bounded concurrency (4–8 in flight),
backoff on 429, and cancel are a **prerequisite**, not a follow-up. The progress and ETA numbers
the dry-run preview promises also come from here.

### B4 — Extraction has nowhere to run 🟠

HTML parsing, PDF text extraction and hashing are CPU-bound. Running them on the aiohttp event loop
stalls every other request in the process, including unrelated chat streams — and a 5,000-page
crawl would do it for minutes at a time.

Needs a decision before code: a `ThreadPoolExecutor` via `run_in_executor` (simple, GIL-bound but
fine since most of this releases the GIL in C), or a worker process. Not a large piece of work, but
it shapes how the pipeline is written, so it can't be retrofitted cheaply.

---

## Underspecified — decide now, build later

**U1 — Long-running runs need a transport.** A 5,000-page crawl can't complete inside a request.
The extension already has an SSE precedent (`threads/{id}/updates/stream`); reuse it for run
progress rather than inventing polling.

**U2 — Existing documents have no source and a different `category` meaning.** Today's `category`
is a flat label from the upload param; tomorrow's is a folder path. Migration needs an explicit
answer: I'd leave existing rows untouched (a flat value is a valid one-segment path), backfill
`sourceKey` from `displayName`, and leave `sourceId` null. Worth confirming, since the alternative
is a corpus with two meanings for one column.

**U3 — Changing chunking is a full re-index.** Same class of problem as `extractorVersion` (§3.4)
and currently unhandled. `chunking` belongs in the same "bump = deliberate, costed, confirmed
re-index" mechanism rather than being a quietly editable field.

**U4 — Secrets storage.** "Follow the `github_auth` precedent" isn't a spec. Concretely: source
config stores `{"token": "$ZENDESK_TOKEN"}` — an env var *reference*, resolved at run time, never
the value — with a config file under `ctx.get_user_path()` as the fallback for values that can't be
env vars. The API must never return them.

**U5 — The ingest UI isn't mocked.** Metadata edit and bulk apply have mockups; source list, source
config, dry-run review and run history don't. Given how much the bulk mockup changed the design
(the fill-empty default, the canonicalised autocomplete state), the dry-run review screen is the one
most likely to teach us something — it's where an operator decides to spend money.

---

## Tests

`tests/` has 20 files and the style is established. Three that matter:

1. **Idempotency** — a fixture corpus, imported twice, asserts the second run performs zero writes
   and zero uploads. This is the guarantee from §3.1 and the one most likely to silently regress.
2. **Category derivation** — the table from §7 as parameterised cases, including `root` scoping,
   `maxDepth`, backslashes, root-level documents, and `categoryPath` ancestors.
3. **Delete rails** — a discover that raises partway must compute zero deletions; a discover
   returning 5% of the previous set must refuse rather than delete 95%.

Also worth noting: `tests/test_google_streaming.py` exists and covers the streaming path I changed
in Phase 0 — but nothing there asserts on grounding metadata. Since I couldn't verify the streaming
citation fix against a live response, **a fixture-based test feeding a recorded SSE stream with
`groundingMetadata` through `handle_stream_response` is the cheapest way to lock that behaviour
down**, and it belongs with this work.

---

## Revised order

Combining both features as you suggested, with the blockers folded in:

| # | Step | Why here |
|---|---|---|
| 0 | Run `probe_filters.py` | Settles the schema. Everything downstream assumes an answer. |
| 1 | Worker concurrency + cancel + progress (B3) | Nothing else is usable without it, and it's independently valuable today. |
| 2 | `document` table rebuild (B2) + new metadata columns | One migration, done once, rather than two. |
| 3 | `/facets` endpoint + facet rail | Read-only, no edit machinery, immediately useful. |
| 4 | Ingest pipeline + `source`/`source_run` + local folder & zip | The pipeline proves itself with no network, no auth, no extraction subtleties. |
| 5 | Metadata rules + category derivation + dry-run preview | The point where ingest stops creating a bulk-edit backlog. |
| 6 | `/documents/bulk` + pending/undo + autocomplete | Handles the pre-existing backlog and the exceptions. |
| 7 | Sitemap crawl + HTML extraction (B4 lands here) | First source that needs extraction to be good; makes `sourceUrl` real. |
| 8 | Git source | Small once the pipeline exists; best incremental story. |
| 9 | Scheduling + delete detection with rails | Turns imports into a corpus that stays current. |

Steps 1–3 are all pure improvement to what exists today — no new surface — which makes them a safe
place to start while the probe result comes back.

---

## What I need from you

1. **Run the probe** (or give me a key and I'll run it). It gates step 0.
2. **U2** — confirm existing documents are left as-is with `sourceKey` backfilled from
   `displayName`.
3. **Should ingest be admin-only?** Creating a source that reads the filesystem and fetches URLs is
   a privileged operation; Phase 0 only gated *mutating* routes on being signed in, not on a role.
4. **Do you want the dry-run review screen mocked first?** It's the screen where someone commits
   spend, and the two mockups so far both changed the design underneath them.
