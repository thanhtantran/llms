# Document metadata — proposed schema

Fills in §3.2 of [RAG_IMPROVEMENTS.md](./RAG_IMPROVEMENTS.md). Covers what metadata a document
should carry, which of it goes to Gemini vs. stays local, and why each field earns its place.

---

## 1. What the API can actually do

The design is bounded by what `metadata_filter` can express, so start there. Verified against
the live Gemini REST API rather than the prose docs:

**`CustomMetadata`** takes a `key` plus exactly one of three value types:

```python
{'key': 'docType',   'stringValue': 'guide'}
{'key': 'updatedAt', 'numericValue': 1755648000.0}
{'key': 'versions',  'stringListValue': {'values': ['v6', 'v7', 'v8']}}
```

`stringListValue` is the one worth noticing — **multi-valued metadata is supported natively**, so a
page that applies to three versions doesn't need three documents or a delimited string hack.
`numericValue` is a float, so dates have to be encoded as epoch seconds to be comparable.

**`FileSearch`** (the tool) takes `file_search_store_names`, `metadata_filter` (one AIP-160 filter
string), and `top_k`. Note `top_k` — it exists and the extension doesn't set it today.

**`ChunkingConfig`** takes `white_space_config.max_tokens_per_chunk` and `.max_overlap_tokens`.

### The filter grammar — measured, not assumed

Google's File Search docs demonstrate exactly one filter form — `author="Robert Graves"` — and
delegate everything else to [AIP-160](https://google.aip.dev/160). Probed against the live API
(`POST /ext/gemini/capabilities/probe`, two fixtures with contrasting metadata, judged on *which*
document comes back):

| Operator | Expression | Works |
|---|---|---|
| equality | `status="published"` | ✅ |
| AND | `status="published" AND versions:"v8"` | ✅ |
| OR | `status="published" OR status="deprecated"` | ✅ |
| NOT | `NOT status="deprecated"` | ✅ |
| has, on a stringList | `versions:"v8"` | ✅ |
| numeric comparison | `sortkey > 1700000000` | ✅ |

**The full grammar is available.** `categoryPath:"guides"` subtree filtering, list-valued
`versions`/`tags`, and `updated_at` staleness filtering are all safe to build on.

`OR` binds *tighter* than `AND` — `a AND b OR c` parses as `a AND (b OR c)`. Always parenthesise.

### ⚠ Keys must be lowercase or snake_case

The one real constraint, and it isn't documented anywhere. A **camelCase key indexes without
complaint and then never matches a filter**:

| Same document, same value, three spellings | Filter result |
|---|---|
| `doc_type="guide"` | ✅ matches |
| `doctype="guide"` | ✅ matches |
| `docType="guide"` | ❌ **returns nothing** |
| `sortkey > 1700000000` | ✅ matches |
| `sortKey > 1700000000` | ❌ **returns nothing** |

This fails *silently* — no error at upload, no error at query, just an empty result set — which
makes it exactly the kind of thing to find by probing rather than by reading. An earlier draft of
this schema used `docType`, `sourceUrl`, `categoryPath` and `updatedAt`, all of which would have
been unfilterable.

Pushed keys are therefore snake_case. Local column names stay camelCase to match the rest of the
codebase; `PUSHED_METADATA` in `db.py` is the single place the two conventions meet, and an
`assert` there refuses any key that isn't lowercase.

### The constraint that shapes everything

**Remote metadata is immutable without re-uploading the document.** Local SQLite columns are free
to change; a `custom_metadata` value can only be corrected by deleting and re-indexing, which costs
an embedding pass per document.

That gives a clean rule for what goes where:

> Push a field to Gemini only if it is used by a **filter**, a **citation**, or a **reconciliation
> decision**. Everything else stays local.

Metadata that is merely nice to know — author, word count, crawl depth, ingestion job id — belongs
in the `document` table, where changing it is an `UPDATE` rather than a re-index.

---

## 2. The store is the security boundary, not a filter

Before the field list, the question that determines half of it: should public and internal content
live in one store separated by metadata, or in separate stores?

**Separate stores.** An earlier draft of this document recommended an `audience` metadata field.
That was wrong, for a reason worth writing down.

### Why a filter is the wrong control

`metadata_filter` **fails open**. It's an optional string on the tool: omit it, misspell a key,
quote it wrong, and the call still succeeds — returning everything. There is no deny-by-default
and no error to catch. Every code path that builds a filter is a path that can silently drop it.

`file_search_store_names` **fails closed**. It's required, and retrieval can only reach the stores
named in it. A public assistant configured with `["fileSearchStores/acme-public"]` cannot return an
internal document, whatever goes wrong upstream — no filter bug, prompt injection, or config
mistake can reach content that isn't in a named store.

For a control whose failure mode is *leaking a customer's confidential documents to the public
internet*, that difference is the whole argument. Auditing it is also a list operation — "what's in
the public store?" — rather than reasoning about whether a filter expression is airtight.

### The cost objection doesn't hold

The case for one store was double indexing spend, double sync surface, and drift. Checking the API
rather than assuming:

- **`file_search_store_names` is a list.** The staff assistant queries
  `["acme-public", "acme-internal"]` in a single call; the widget queries `["acme-public"]` alone.
  A document lives in exactly one store and **nothing is duplicated**. That was the false premise —
  I'd assumed the internal assistant needed its own copy of the public corpus. It doesn't.
- **Storage is free.** Only the one-time embedding pass is charged; storage and query embeddings
  cost nothing. So even where duplication is genuinely wanted, it's a one-off cost, not recurring.
- **Reclassification costs the same either way.** Remote metadata is immutable without re-upload,
  so moving a document from internal to public is a delete + re-index under *both* designs. The
  metadata field buys nothing here.

There's even an independent argument for splitting: Google recommends keeping each store under
20 GB for retrieval latency, so a large corpus wants partitioning regardless.

### The rule this generalises to

> Use a **separate store** for any axis where fail-open is a **breach**.
> Use **metadata** for any axis where fail-open is merely a **worse answer**.

`audience` is a breach axis → store. `locale`, `versions`, `docType`, `product`, `status` are
quality axes → metadata. Getting `locale` wrong returns the Japanese page to an English question;
annoying, not a disclosure. That single test resolves the whole class of question.

### What this means concretely

Add `visibility` to the **`filestore`** table — `public` | `internal`, defaulting to `internal`.
One flag on a handful of stores rather than a flag on ten thousand documents, and it's what the
Phase 3 assistant config validates against:

> A published (public) assistant may only reference stores whose `visibility` is `public`.

That check lives in one place, runs at config time rather than query time, and is a hard error
rather than a silently-degraded filter.

---

## 3. Core fields — always pushed

Seven keys. Four exist today (`id`, `hash`, `category`, `sourceUrl`); three are new. Audience is
deliberately absent — that's a store, per §2.

| Column | Pushed as | Type | Why it's core |
|---|---|---|---|
| `id` | `id` | numeric | Existing. Local row identity for reconciliation. |
| `hash` | `hash` | string | Existing. Change detection during sync. |
| `category` | `category` | string | Existing. Structural axis — the document's folder path relative to its source root. |
| **`categoryPath`** | `category_path` | stringList | Ancestor prefixes, so a filter can select a whole subtree. |
| `sourceUrl` | `source_url` | string | Added in Phase 0. The page a citation links to. |
| **`docType`** | `doc_type` | string | Routes a question to the right *kind* of content. |
| **`sourceUpdatedAt`** | `updated_at` | numeric | Staleness — filtering and display. |

### `category` and `categoryPath`

`category` is derived at ingest from the document's folder path relative to its source root — see
[INGEST.md §7](./INGEST.md). Deriving it rather than typing it is what keeps it consistent enough to
be worth filtering on.

Because it's now a *path* (`guides/auth`, not `guides`), exact-match filtering can no longer select
a subtree: `category="guides"` doesn't match `guides/auth`. `categoryPath` carries every ancestor
prefix so `categoryPath:"guides"` does, while `category="guides/auth"` still pins one folder.

The pair costs one extra key and depends on the `:` operator working against `stringListValue` —
probe #3 in §7. If that fails, collapse to a flat single-segment `category` and keep the full path
in a local-only column.

### `docType`

`reference` | `guide` | `faq` | `api` | `release-notes` | `policy` | `changelog`

A support widget should lean on `faq` and `guide`; a developer assistant on `api` and `reference`.
Release notes are actively harmful in a support answer — they describe what changed in one version,
phrased as though it's current.

It also gives §3.3's chunking presets a key to hang off: `reference` and `api` want small chunks
with high overlap; `guide` and `policy` want large chunks that keep an argument intact.

### `updatedAt`

Epoch seconds, because `numericValue` is a float and AIP-160 compares numbers.

Two uses. The filter — `updatedAt > 1735689600` to exclude content nobody has touched since 2024 —
and, more valuable day to day, **display**: showing "last updated 3 years ago" next to a citation
is a cheap, strong trust signal, and it feeds the content-gap report (§3.5) with "these are your
most-cited stalest pages."

---

## 4. Recommended fields — columns, optionally populated

Not every corpus needs these; a single-product English-only docs site doesn't. They're columns like
the core set (§5), simply left null where they don't apply — a null column costs nothing and is
never pushed to Gemini, so no store pays for keys it doesn't use.

| Key | Type | Example | Rationale |
|---|---|---|---|
| `product` | string | `ServiceStack` | Orgs ship more than one thing. Keeps `category` from becoming a compound key. |
| `versions` | stringList | `["v6","v7","v8"]` | Answering a v6 question from v8 docs is the top complaint in versioned docs. |
| `locale` | string | `en`, `ja` | Stops an English question retrieving the Japanese page, and vice versa. |
| `status` | string | `published` | Content lifecycle — excludes deprecated pages by default. |
| `tags` | stringList | `["security","deprecated"]` | Escape hatch for org-specific facets. |

### `versions` as a list, not a number

A documentation page usually applies to a *range* of versions, not one. Modelling it as
`versions: ["v6","v7","v8"]` and filtering `versions:"v8"` matches reality and needs no
interpretation of what "v8" means as an ordered value.

If the probe in §7 shows `:` isn't supported, the fallback is a pair — `versionMin` and
`versionMax` as numerics — filtered with `versionMin <= 8 AND versionMax >= 8`. That works with
plain comparison operators but forces versions into a single ordered number, which breaks the
moment someone ships `8.1.2` alongside `2024.3`. Prefer the list; keep the pair in reserve.

### `status` — a quality axis, not a security one

Worth being explicit about why this stays as metadata while audience became a store: `status`
answers *is this still true*, and getting it wrong surfaces a deprecated page in an answer. That's
a worse answer, not a disclosure — the §2 test puts it squarely on the metadata side.

`published` | `draft` | `deprecated` | `archived`

The reason to have it rather than deleting stale docs: deprecated content still answers "was this
ever supported?" and "when was this removed?", which are real support questions. Excluding it by
default while keeping it retrievable on request is strictly better than losing it. Default filter
for any assistant: `status="published"`.

### `tags`

The `tags` column already exists in the `document` table and is unused. Promoting it to a
`stringListValue` gives orgs somewhere to put the facet you didn't anticipate — `["gdpr"]`,
`["enterprise-only"]`, `["needs-review"]` — without a schema change or a re-index of everything.

---

## 5. Fixed columns, not a declared schema

An earlier draft of this section recommended a per-store `metadataSchema` JSON column that declared
each field's key, type, allowed values and facet-ability. **I'd drop that.** It solves a problem
that turns out to already be solved, at a cost that lands well before the benefit.

### Why fixed columns win here

**The proposed fields aren't customer-specific.** `docType`, `status`, `updatedAt`, `locale`,
`product`, `versions` are near-universal for a documentation or knowledgebase corpus. A field that
every store wants is a column, not a schema entry — and as columns they get type affinity, indexes,
and a purpose-built UI instead of a generic one.

**The existing plumbing gives them to you for free.** `GeminiDB.add_missing_columns()` already runs
`ALTER TABLE … ADD COLUMN` for any declared column that isn't present, so adding them is a dict
entry and a restart. More usefully, `sql_filter()` builds a `WHERE` clause from *any* query
parameter whose name matches a column:

```python
for k in query:
    if k in all_columns:
        filter[k] = query[k]
```

So the moment `status` and `locale` are columns, `GET /documents?status=deprecated&locale=en`
works with no new code. A JSON schema column gets none of that — every query against it needs
hand-written `json_extract` SQL.

**Facets should be discovered, not declared.** This is the part that made the schema look
necessary and doesn't. The extension *already* derives facets from data:
`GET /filestores/{id}/categories` returns each distinct category with its document count, computed
by `GROUP BY`. Generalising that one endpoint to `GET /filestores/{id}/facets?fields=product,locale`
gives the whole facet UI — real values, real counts, no declaration to keep in sync with reality.
A declared schema can only tell you `product` *exists*; the data tells you there are four of them
and how many documents each has.

**Declared schemas drift apart.** Left to per-store declaration, one store calls it `status` and
another `state`, and the content-gap analytics in §3.5 of the main doc can no longer aggregate
across stores. A shared vocabulary is a feature, and fixed columns enforce it for free.

**It's the wrong thing to build now.** A schema means a JSON column, a validator, a schema editor,
schema versioning, migration when the schema changes, and a generic facet renderer. That's real
machinery to build before a single customer has told you which field they're missing — while
Phase 1 ingestion, the actual blocker, is still unbuilt.

### The escape hatch that replaces it

`tags` (a `stringListValue`) covers the long tail. A customer who needs `gdpr`, `enterprise-only`
or `needs-review` adds a tag rather than a schema field. What they give up versus a declared field
is validation and facet counts — usually an acceptable trade for something a handful of documents
carry.

The one small piece of per-store config worth keeping is a **display** preference, not a schema:

```json
{ "facets": ["category", "product", "versions"] }
```

An array on `filestore` saying which columns to surface as tiles on that store's page. Two lines
of config, no validator, no versioning — and it's genuinely per-store, because a single-product
English corpus shouldn't render empty `product` and `locale` pickers.

### When to revisit

Two triggers, either of which would change the answer:

- **Three or more customers want the same field you don't have.** That's not a schema signal —
  promote it to a column and everyone benefits from the shared vocabulary.
- **Customers want *mutually different* fields that need validation and facet counts**, not just
  tags — a legal corpus wanting `jurisdiction` and `practiceArea` while a manufacturer wants
  `partNumber` and `machineModel`, both as first-class validated facets. That is when a declared
  schema earns its complexity, and not before.

Until one of those shows up, fixed columns plus `tags` plus derived facets covers it.

---

## 6. What stays local

Explicitly *not* pushed to Gemini, because none of it filters, cites, or reconciles:

`filename`, `url`, `size`, `mimeType`, `state`, `error`, `startedAt`, `uploadedAt`,
`displayName`, `createTime`/`updateTime`, plus the ingestion fields Phase 1 will add
(`sourceType`, `sourceRef`, `ingestJobId`, `crawlDepth`, `author`, `wordCount`).

Each of these changes on its own schedule. Pushing `state` would mean re-indexing a document every
time an upload retried.

### One anti-pattern to name

The failure mode here is compressing everything into `category`:

```
category = "public-v8-en-auth"      ← don't
```

It reads fine on day one and then can't be filtered on any single axis, can't be faceted, and has
to be parsed by string surgery everywhere. Keep `category` as the structural/navigational axis it
already is — the thing that maps to a folder and drives the tiles — and put the orthogonal
dimensions in their own keys.

---

## 7. Verify before building on it

Only `=` is demonstrated in Google's documentation. Before the facet UI or the widget's filter
builder depends on the richer grammar, settle it with a probe: upload three fixture documents
carrying every value type, then run each expression and record which return results.

| # | Expression | Establishes |
|---|---|---|
| 1 | `status="published"` | Baseline equality (documented) |
| 2 | `status="published" AND locale="en"` | `AND` |
| 3 | `versions:"v8"` | `:` on `stringListValue` — decides the version model above |
| 4 | `updatedAt > 1735689600` | Numeric comparison — decides whether staleness filtering works |
| 5 | `status="published" AND (docType="faq" OR docType="guide")` | Grouping + `OR` precedence |
| 6 | `NOT status="deprecated"` | Negation |

Worth keeping as a test that runs against a scratch store, since this is undocumented surface that
can change under you.

---

## 8. Migration

Adding keys to the schema doesn't change already-indexed documents — remote metadata is immutable
without re-upload. Two consequences:

- The existing `sync` endpoint will start reporting **Metadata Mismatch** for every pre-schema
  document. That's correct, and it's the signal to act on rather than noise to suppress.
- Sync should grow a **"re-index N documents"** action that re-uploads with current metadata, with
  a cost estimate attached, since for a large corpus this is the expensive operation.

Suggested order, each independently useful:

1. `filestore.visibility` + the public/internal store split — the safety boundary, and the
   prerequisite for a public widget. No document re-indexing needed for stores that already
   hold only one kind of content.
2. `status` — lifecycle, cheap, and immediately useful for excluding deprecated pages.
3. `docType` + `updatedAt` — answer quality and the staleness signal.
4. Generalise `/filestores/{id}/categories` into `/filestores/{id}/facets` — one endpoint,
   and the facet UI follows from data already in the table.
5. `product` / `versions` / `locale` / `tags` — added as columns, populated by the stores
   that need them.

---

## Sources

- [File search | Gemini API](https://ai.google.dev/gemini-api/docs/file-search)
- [File Search API reference](https://ai.google.dev/api/file-search)
- [AIP-160: Filtering](https://google.aip.dev/160)
- Gemini REST resources — `CustomMetadata`, `StringList`, `FileSearch`, `ChunkingConfig`
