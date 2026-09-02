# Metadata UI — view, edit, query

Companion to [METADATA_SCHEMA.md](./METADATA_SCHEMA.md). Interaction mockup:
[`mockups/metadata-editor.html`](./mockups/metadata-editor.html) — open it, the autocomplete is live.

---

## 1. The constraint that shapes the whole UI

**Editing metadata is not a cheap inline edit.** A local column is an `UPDATE`; the copy Gemini
holds is immutable, so changing it means delete + re-upload — an embedding pass per document,
queued through the upload worker.

If the UI hides that, a user "fixes a typo" on 400 documents and silently triggers 400 re-indexes.
If the UI makes every edit modal and expensive, nobody backfills anything. The resolution is to
**decouple the edit from the re-index**:

1. Editing writes local columns immediately. Free, instant, undoable.
2. Affected documents become *pending* — local metadata differs from what's in Gemini.
3. A persistent banner shows the count and offers **Re-index N** as one deliberate action.

That's one confirm for a batch, instead of a confirm per edit or none at all.

### Pending is derived, not tracked

No `dirty` flag is needed. `document.customMetadata` already holds exactly what Gemini has — the
upload worker writes it from the API response, and `sync` refreshes it from the remote document.
So *pending* is `local columns ≠ customMetadata`, computed with the projection the worker already
uses.

This can't drift the way a flag can: a crashed re-index, a manual sync, a restore from backup all
self-correct, because the comparison is against ground truth rather than against a boolean someone
forgot to clear.

---

## 2. Autocomplete — your idea, plus the case it misses

Suggestions come from `GET /filestores/{id}/facets`, the generalisation of the existing
`/categories` endpoint. Same source as the facet tiles, so there's one definition of "what values
exist here" and it can never disagree with itself.

Each suggestion carries its **document count**, sorted descending. That alone does a lot of work:
seeing `guide — 612 docs` next to `guides — 3 docs` tells you which one is the real vocabulary and
which is somebody's earlier slip.

### Four states, not two

The match/new border you described, with one addition that turned out to matter:

| State | Border | Message | Rationale |
|---|---|---|---|
| **Exact** | teal | `✓ Existing value — 1,180 other documents use it` | Confirms, and the count is a sanity check |
| **Canonicalised** | teal | `✓ Matches ServiceStack (1,180 docs) — will be saved with that spelling` | See below |
| **Near-match** | red | `⚠ Did you mean guide (612 docs)?` + `Use "guide"` / `Keep "guides"` | The state that actually prevents drift |
| **New** | amber | `＋ New value — this would be the first document in the store with it` | Legitimate, but deserves a beat of hesitation |

**Canonicalised** is the one worth calling out, because building the mockup exposed the bug. A
naive implementation matches case-insensitively and reports "existing value" — then stores what the
user typed. Type `Servicestack`, get a reassuring green border, and quietly create a second value
alongside `ServiceStack`. The control designed to prevent vocabulary drift *causes* it.

So a case- or whitespace-insensitive hit resolves to the **canonical stored spelling**, and says
so. Same on blur, so it holds even if the user never opens the menu.

**Near-match** is where most of the value is. Free-form metadata dies of `v8` / `V8` / `8.0`, and
`guide` / `guides`. Normalise (lowercase, strip spaces/underscores/hyphens) and take a Levenshtein
distance against existing values — within 1 edit for short strings, 2 for longer — and offer both
options explicitly. Never block: `Keep "guides"` is always one click away, because sometimes the
near-miss is the point.

**New is confirmable, not prevented.** ⌘⏎ or clicking *Add "x" as a new value* commits it. One
extra keystroke: negligible when deliberate, enough to catch a fat-finger.

### Multi-value fields

`versions` and `tags` are `stringListValue`, so they render as chip inputs. Each chip carries its
own state — teal with a usage count for known values, amber with `new` for fresh ones — so a
document tagged `["security", "gdpr"]` shows at a glance that one is established vocabulary and the
other isn't.

---

## 3. Bulk selection and apply

Mockup: [`mockups/metadata-bulk.html`](./mockups/metadata-bulk.html).

Single-document editing is a detail view. The operation that matters is **backfilling metadata onto
a corpus that already exists** — import 1,500 documents, then discover none of them have `docType`.
Doing that a row at a time is not merely tedious, it's not going to happen, and the metadata stays
empty. So bulk is the primary path and the single-document editor is the exception.

Both use the same form. A selection adds *operations* to it (below) and a summary of what those
documents currently say; a single document is the same grid with the values it already has. What
appears on selection is a slim bar naming the selection and offering **Edit metadata** and
**Delete** — the editing itself happens in a dialog, because a bar wide enough to edit five fields
in covers the rows you are selecting.

### Selection has three tiers

1. **Row checkboxes**, tracked by id so selection survives paging, sorting and search. Losing a
   selection because you paged is the fastest way to make someone give up.
2. **Select page** — the header checkbox.
3. **Select all N matching the current filter.** The one that matters: you're paging 50 at a time
   through 412 matches, and the thing you want is all 412.

Tier 3 has to be visually unmistakable from tier 2, because the blast radius differs by 8×. When
the page is fully ticked, a bar appears — *All 8 documents on this page are selected.
**Select all 412 matching this filter*** — and once escalated it says so plainly, with the way back.

The filtered set doubles as the selection expression, so the API takes either form:

```
POST /documents/bulk   { "ids": [...], "set": {...} }
POST /documents/bulk   { "filter": {"category":"guides","docType":null}, "set": {...} }
```

The `filter` form matters for large sets — you don't want to ship 12,000 ids over the wire, and the
filter is a stable description of intent that can be re-run.

### The operation is not just "set"

Overwriting is rarely what you want when backfilling, and offering only that guarantees someone
destroys curated values on their first attempt.

| Field kind | Operations |
|---|---|
| Single-value | **Set where empty** (default) · Set (overwrite) · Clear |
| List (`tags`, `versions`) | **Add to list** (default) · Remove from list · Replace list |

**Set where empty** is the default because it's the safe backfill: documents that already have a
value are left alone, which protects curated data *and* cuts the re-index bill. In the mockup's
fixture, `docType` across 412 documents is 269 re-indexes with fill-empty and 358 with overwrite —
a third more cost for an operation that also silently discards work.

**Add to list** is the equivalent for multi-valued fields: tagging 400 documents `security` without
wiping the tags they already carry.

### Preview the effect, not just the count

"412 selected" is not enough information to press a button that costs 412 embedding passes. The
panel splits the selection into what actually happens:

```
Selected                412
Will change             269
Already set — skipped   143
─────────────────────────────
Re-index cost   269 embed · ~4 min
```

The second and third numbers are the point. Without them a user assumes worst-case cost and doesn't
run the operation; with them the real price is visible before committing. The three buckets always
sum to the selection, so the arithmetic is checkable at a glance — and the Apply button carries the
number (`Apply to 269`) rather than a bare verb.

The estimate comes from the worker's measured throughput, so it improves once the worker gets the
concurrency treatment from §2.6 of the main doc.

### Pending is the undo buffer

This is what makes an expensive operation safe, and it falls straight out of §1's deferred model:

1. **Apply** writes local columns. Instant, free, and nothing has reached Gemini.
2. The documents are now *pending* — and **Undo is free**, because undo is just another local write.
3. **Re-index** is the deliberate, costed step. Only after this does undo mean re-indexing again.

So the risky operation has a review stage built in by construction rather than bolted on as a
confirm dialog. A user can apply three different bulk edits, look at the result, and back all of
them out without having spent anything.

Re-indexing needs the treatment any long job does: progress with a count, ETA, cancel, and
per-document failure — `128 done, 12 failed · Retry failed`. The `error` column already exists for
this, and the retry endpoint already exists per document.

### Vocabulary management falls out of it

With derived facets (§5) plus bulk apply, the cleanup tools are nearly free — and they're what you
need on any corpus imported before the metadata existed:

- **Rename** `guides` → `guide` across 3 documents
- **Merge** `V8` into `v8`
- **Delete** a value, clearing the column on those documents

Each is a filter plus a set, so they reuse the same endpoint, preview and pending flow.

### The best bulk edit is the one you never do

Worth stating because "tedious" is a symptom, not the disease: most metadata should never be
entered by hand. It should be **derived at ingest**.

The extension already does a version of this — folder name becomes `category`. Generalising that to
path-pattern rules on the filestore is a small piece of Phase 1 ingestion and removes most of the
manual work:

```json
{ "rules": [
  { "match": "docs/v8/**",        "set": { "versions": ["v8"] } },
  { "match": "**/reference/**",   "set": { "docType": "reference" } },
  { "match": "**/*.faq.md",       "set": { "docType": "faq" } },
  { "match": "**",                "set": { "locale": "en", "status": "published" } }
] }
```

Saved on the store, applied on every import, so it's configured once rather than re-done after each
crawl. Bulk edit then handles the exceptions and the pre-existing backlog rather than the bulk of
the work — which is the right division of labour.

---

## 4. Coverage, so the gaps are visible

Missing metadata is invisible: a document with no `docType` looks identical to one with a correct
`docType` until an answer goes wrong. The store page gets a coverage strip — one row per field,
percentage populated, and the missing count as a link into the filtered document list.

That turns the panel into a **worklist**: see `status — 46% · 810 missing`, click it, land on those
810 documents with the bulk editor open. The reason to build it isn't reporting, it's that it makes
the next action obvious.

---

## 5. Querying

The existing category tiles generalise into a facet rail — one group per configured facet, values
with counts, from the same endpoint that feeds the autocomplete. Selections become chips, AND-ed.

**Show the generated filter expression.** It reads as a debugging nicety and is actually load-bearing:

```
product="ServiceStack" AND versions:"v8" AND status="published"
```

The grammar beyond `=` is undocumented (§1 of the schema doc), so when a filter returns nothing
this string is the only way to tell a syntax problem from an empty result set. It's also exactly
what gets pasted into a published assistant's config in Phase 3 — the filter you tested is
verifiably the filter that ships.

**Show both counts.** `Local matches 612 · Gemini returns 598`. A gap means documents were edited
locally and not re-indexed — the same pending state as the banner, surfaced where it changes what
you conclude from a query. Without it, a stale-metadata problem looks like a retrieval problem.

---

## 6. Endpoints this needs

```
GET   /filestores/{id}/facets?fields=category,docType,product,versions,locale,status,tags
      → { "docType": { "values":[{"value":"guide","count":612},…], "null": 272 }, … }

POST  /documents/bulk          { "ids":[…] | "filter":{…},
                                 "changes":[{ "field":"docType", "value":"guide",
                                              "op":"fill"|"set"|"clear"|"add"|"remove" }, …],
                                 "dryRun": true }        // → the preview, no writes
      → { "selected":412, "change":269, "skipped":143, "same":0,
          "fields": { "docType": { "change":269, "skipped":143, "same":0 } } }

POST  /documents/summary       { "ids":[…] | "filter":{…}, "fields":[…] }
      → { "count":412, "sample":["a.md",…],
          "fields": { "docType": { "values":[{"value":"guide","count":140},…], "empty":269 } } }

POST  /documents/delete        { "ids":[…] | "filter":{…} }
      → { "selected":412, "deleted":410, "ids":[…], "errors":[{ "displayName":"x.md", … }] }

POST  /documents/bulk/undo     { "batchId": "…" }        // free while pending
POST  /filestores/{id}/reindex { "ids":[…] }             // omit ids = everything pending
GET   /filestores/{id}/pending → { "count":47, "ids":[…] }
```

`dryRun` is what backs the preview panel, so the numbers shown are produced by the code that does
the work rather than by a parallel estimate that can disagree with it. `batchId` comes back from a
non-dry-run apply and is what Undo targets.

`changes` is a list because the editor edits several fields at once, and the total has to count
*documents*: three fields changed on one document is one embedding pass, and summing three
per-field counts would price it as three. The single-field form is still accepted.

`/documents/summary` exists because `/facets` describes the *store*, not the selection, and so
can't tell "all 412 say guide" from "they say six different things" — which want different edits.
It also names the documents a delete is about to take, which is the only check available on a
selection made by filter rather than by ticking rows.

Two notes on `/facets`:

- It supersedes `/filestores/{id}/categories`, which becomes `?fields=category`. Keep the old route
  as an alias so the current UI doesn't break.
- `versions` and `tags` are JSON arrays locally, so their `GROUP BY` needs `json_each` rather than a
  plain column aggregate. That's the one piece of custom SQL in the whole design.

---

## 7. Where it lands in the existing UI

`FileStoreDetails` already has most of the frame — category tiles, a paged document grid, search,
sort. The additions are incremental rather than a rewrite:

| Existing | Becomes |
|---|---|
| Category tiles | Facet rail — category plus whichever facets the store configures |
| Document grid | Adds selection checkboxes (id-tracked, survive paging) and metadata columns |
| — | Selection bar — names the selection, offers Edit metadata and Delete |
| — | Metadata dialog: the import form plus operations and a live effect preview |
| — | Edit button at the end of a row's metadata, opening the same dialog for one document |
| — | Coverage strip + pending banner at the top |

Suggested order, each shippable alone:

1. `/facets` endpoint + facet rail — read-only, immediately useful, no edit machinery.
2. `/documents/bulk` with `dryRun` + selection + the pending banner and Undo. This before the
   single-document editor: it's the operation people actually need, and the single-document
   drawer is a one-row special case of it.
3. Autocomplete control — used by both, but a bulk edit is where it pays off first.
4. Coverage strip, then rename/merge, which are filter-plus-set on top of what exists by then.
5. Ingest rules (Phase 1), which stop most of the backlog from being created in the first place.
