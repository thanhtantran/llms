# Ingest — design & recommended sources

Phase 1 of [RAG_IMPROVEMENTS.md](./RAG_IMPROVEMENTS.md). Consumes the metadata design in
[METADATA_SCHEMA.md](./METADATA_SCHEMA.md) and the bulk/pending model in
[METADATA_UI.md](./METADATA_UI.md).

Today the only way into a store is `POST /filestores/{id}/upload` with multipart files. That's fine
for a developer with a folder and fatal for an organisation with a documentation site, a wiki and a
helpdesk. This is the blocker everything else in the roadmap sits behind.

---

## 1. Four decisions that shape everything else

### 1.1 A source is an entity, not an action

The instinct is to build "import from a URL" as a button. Build it as a **registered source**
instead — a row that knows what it points at, what it last saw, and how to look again.

Everything valuable follows from that and nothing follows from a button: re-sync, scheduled
refresh, delete detection, incremental fetch, per-source metadata rules, per-source failure
reporting. A one-shot import is just a source that never runs twice.

### 1.2 One pipeline; sources differ only at the front

```
discover → fetch → extract → derive metadata → diff → apply
```

Only **discover** and **fetch** are source-specific. Everything downstream — HTML to markdown,
metadata rules, change detection, tombstoning, queueing to the upload worker — is shared. A new
connector is then two methods and a config schema, not a new import path, which is the difference
between shipping four sources and shipping fifteen.

```python
class Source:
    async def discover(self) -> AsyncIterator[Item]:   # key, title, changeToken, fetch handle
        ...
    async def fetch(self, item) -> tuple[bytes, str]:  # content, mime type
        ...
```

`Item.changeToken` is whatever the source cheaply offers — an ETag, a git blob sha, a
`lastModified`. Where a source gives nothing, the content hash after fetch is the fallback.

### 1.3 Dependencies stay near zero

`llms` has exactly one runtime dependency (`aiohttp`) and the bundled Gemini extension adds none.
That's clearly deliberate, so ingest shouldn't quietly arrive with
BeautifulSoup, trafilatura, GitPython and boto3 behind it.

The codebase already has the idiom for this — `browser` needs the `agent-browser` CLI, `pdf` needs
`typst`, and each sets `ctx.disabled = True` when its tool is absent. Apply it per **source type**:

| Need | Approach |
|---|---|
| HTTP | `aiohttp`, already a dependency |
| Sitemap / RSS / Atom | `xml.etree`, stdlib |
| Zip, CSV, JSONL | `zipfile`, `csv`, `json`, stdlib |
| Git | the `git` CLI via subprocess — no library, and it's already on every dev machine |
| HTML → markdown | stdlib `html.parser` extractor by default; use `trafilatura`/`markdownify` **if importable** |
| PDF text | `pdftotext` if on PATH; otherwise the source reports the file as unsupported rather than failing |
| JS-rendered pages | the existing `browser` extension's `agent-browser`, as an opt-in escalation |

A source whose requirement is missing shows as *unavailable, needs `git` on PATH* in the UI rather
than erroring at run time.

### 1.4 Ingest is where metadata comes from

Agreed already, but it's worth stating as a design rule rather than a feature: **bulk edit exists
for exceptions and backlog, not for the main flow.** If a normal import leaves the operator with
1,500 documents to hand-label, ingest has failed. §6 is therefore not a nice-to-have.

---

## 2. Data model

```
source
  id, filestoreId, user, name, type, enabled
  config          JSON   -- type-specific (root path, base url, repo, tokens ref)
  category        JSON   -- { root, stripPrefix, maxDepth } (§7)
  volatile        JSON   -- regexes stripped before hashing (§3.2)
  extractorVer    TEXT   -- bump = deliberate full re-index (§3.4)
  rules           JSON   -- metadata derivation (§6)
  include/exclude JSON   -- glob or url patterns
  extract         JSON   -- selector, strip rules, min length (§7)
  chunking        JSON   -- per-source chunking config
  schedule        TEXT   -- null | interval | cron
  onDelete        TEXT   -- tombstone | remove | ignore
  cursor          JSON   -- incremental state: etag map, git sha, delta token
  lastRunId, lastRunAt, createdAt, updatedAt

source_run
  id, sourceId, startedAt, completedAt, status, dryRun
  discovered, added, changed, unchanged, removed, failed, skipped
  bytes, error, log

document  (additions)
  sourceId       INTEGER
  sourceKey      TEXT     -- stable identity within the source
  sourceEtag     TEXT     -- change token from the source
  contentHash    TEXT     -- sha256 of normalised extracted text (§3.2)
  metadataHash   TEXT     -- sha256 of canonical derived metadata (§3.3)
  extractorVer   TEXT     -- which extractor produced contentHash (§3.4)
  categoryPath   JSON     -- ancestor prefixes, pushed as stringListValue (§7)
  tombstonedAt   TIMESTAMP
```

`source_run` earns its place: an operator whose nightly sync has been quietly failing for a week
needs to see that, and a run that added 900 documents needs to be explainable after the fact.

---

## 3. Identity — and the bug it fixes

§2.5 of the main doc: today `upload_to_filestore` looks up by content hash, so re-uploading an
**unchanged** file deletes and re-indexes it, while re-uploading a **changed** file creates a second
document and leaves the stale one to keep answering questions.

Identity should be the source key, with hash demoted to change detector:

```
UNIQUE (filestoreId, sourceId, sourceKey)
```

| Situation | Action |
|---|---|
| key absent from index | **add** |
| key present, `contentHash` equal | **unchanged** — skip, no spend |
| key present, hash differs | **replace** — delete remote, re-upload, keep the row and its metadata |
| key in index, not seen this run | **removed** — §5 |

Manual uploads keep working: `sourceId` is null and `sourceKey` defaults to the filename, so the
existing endpoint becomes a degenerate source rather than a special case.

### 3.1 Re-importing the same source

The guarantee worth designing to, and worth writing a test for:

> **Running a source twice with nothing changed upstream performs zero writes and spends nothing.**

That's what makes a scheduled nightly sync viable, and it's what makes an operator willing to press
*Run* when they're unsure whether anything changed.

### 3.2 What exactly gets hashed

Determinism is the whole point, so the input to the hash has to be pinned down. Same document, same
bytes, different machine, different Python version, six months later — same hash.

```
contentHash  = sha256( normalise(extracted_text) )
metadataHash = sha256( canonical_json(derived_metadata) )
```

`normalise()` is fixed and boring on purpose:

1. Decode as UTF-8, apply Unicode NFC.
2. Line endings to `\n`; strip trailing whitespace per line; collapse 3+ blank lines to 2.
3. Strip a leading/trailing blank block.
4. Apply the source's `volatile` patterns (below).

`canonical_json()` sorts keys, sorts list values, and omits nulls — otherwise a metadata dict that
merely changed iteration order reads as a change.

**Hash the extracted text, not the raw bytes.** A docs site that stamps a build id or a "generated
at" line into every page changes every byte on every deploy. Hashing after extraction means the
footer never mattered; hashing before it means a full re-index nightly, and the bill to match.

For the residue that survives extraction, the source gets a `volatile` list applied before hashing:

```json
{ "volatile": ["(?m)^Last updated: .*$", "build [0-9a-f]{7,}"] }
```

Worth a line in the run summary when it fires — *12 documents differed only in volatile content,
treated as unchanged* — so it's a visible decision rather than silent suppression.

### 3.3 Content change vs metadata change

Both need a re-upload, since the remote copy is immutable either way, but they aren't the same event
and shouldn't be reported as one:

| Changed | Meaning | Cost |
|---|---|---|
| `contentHash` | The document itself was edited | Re-extract, re-embed |
| `metadataHash` only | A rule changed, or a folder was renamed | Re-upload, same text |
| Neither | Genuinely unchanged | Nothing |

Splitting them makes the run summary honest — *44 changed, 12 metadata only* — and explains the
otherwise baffling case where editing a rule causes a hundred re-indexes.

### 3.4 When the extractor itself changes

The one non-obvious failure mode. Improve the HTML stripper and every `contentHash` in the corpus
changes, so the next scheduled run quietly decides the entire site was edited and re-indexes it.

Pin an `extractorVersion` into the source and record it on the document. A version bump then means
"a full re-index is expected", stated explicitly with a cost estimate and requiring confirmation,
rather than arriving as a surprise on a 3am cron run. It also gives a straight answer to "why did
this document get re-indexed" months later.

---

## 4. The run, and dry run

Every run is previewable, because embedding spend is the thing being committed:

```
Discovered   1,412 files · 38.2 MB
  add             218
  changed          44   (content)
  metadata only    12   (rules changed, same text)
  unchanged     1,138   ← no spend
  removed          12
  skipped           8   (3 empty, 5 unsupported type)

Estimated    274 embeds · ~5 min
```

Same principle as bulk apply: the number that makes people press the button is `unchanged`, because
it shows a re-sync is cheap. A dry run also renders the **derived metadata** for a sample of items
and reports which rules matched how many files — which is where you find out that
`**/reference/**` matched nothing because the directory is actually `ref/`.

Dry run is the default for a source's first run. The operator reviews and confirms; subsequent
scheduled runs apply directly.

---

## 5. Delete detection, with rails

A full discover yields the authoritative key set; anything indexed under that source and not seen is
gone upstream. That's necessary — without it a removed page keeps being cited forever — and it is
also the single most dangerous operation in the system.

Three rails:

1. **Never delete after a partial run.** Deletion is only computed when discover completes without
   error. A half-finished crawl must not conclude the other half was deleted.
2. **Cap the blast radius.** If a run would remove more than a threshold (default 20% or 100
   documents, whichever is smaller) it stops and asks. A site that moved to a new URL scheme, an
   expired token returning an empty list, a typo'd base path — all present as mass deletion, and all
   are recoverable if the run refuses to proceed.
3. **Tombstone by default.** `onDelete: tombstone` removes the document from the store but keeps
   the row, so it's visible, restorable, and countable. `remove` is opt-in for people who want it.

---

## 6. Metadata derivation

Resolved per item, later winning over earlier:

1. **Source defaults** — `{ product: "ServiceStack", locale: "en" }`
2. **Path / URL rules** — first match wins per field, or all-match-merge for list fields
3. **Document frontmatter** — a YAML `---` block, mapped through an allowlist
4. **Source-native fields** — HTTP `Last-Modified` → `updatedAt`, `<html lang>` → `locale`,
   `<link rel=canonical>` → `sourceUrl`, git author date → `updatedAt`, Zendesk `section` →
   `category`
5. **Request override** — whatever the operator passed for this run

```json
{
  "defaults": { "product": "ServiceStack", "locale": "en", "status": "published" },
  "rules": [
    { "match": "docs/v8/**",      "set": { "versions": ["v8"] } },
    { "match": "docs/v7/**",      "set": { "versions": ["v7"] } },
    { "match": "**/reference/**", "set": { "docType": "reference" } },
    { "match": "**/*.faq.md",     "set": { "docType": "faq" } },
    { "match": "**/internal/**",  "skip": true }
  ],
  "frontmatter": { "allow": ["docType", "tags", "status", "versions"] }
}
```

### `sourceUrl` is a template

`sourceUrl` is the page a citation links to, so it is per document — but a default and a rule are
both single strings, which would put one identical link on every document they match. So the value
is expanded per document before hashing:

```json
{ "defaults": { "sourceUrl": "https://docs.acme.com/{category}/{name}" } }
```

| Placeholder | `docs/guides/auth.md`, root `docs` |
|---|---|
| `{fullPath}` | `docs/guides/auth.md` |
| `{path}` | `guides/auth.md` |
| `{pathNoExt}` | `guides/auth` |
| `{dir}` | `docs/guides` |
| `{filename}` | `auth.md` |
| `{name}` | `auth` |
| `{ext}` | `md` |
| `{category}` | `guides` |
| `{title}` | the item's title, or `{name}` |

Names are case-insensitive. A placeholder that resolves to nothing — a document in no category —
doesn't leave `//` behind. Unknown placeholders and malformed templates are rejected.

A variable can extract part of its value with `{variable:/pattern/}`. The first capture group is
used, or the complete match when there is no capture group. For example,
`{name:/^\d{4}-\d{2}-\d{2}_(.+)$/}` turns `2026-09-04_servicestack-pdf` into
`servicestack-pdf`. When a pattern does not match, that document's Source URL is omitted and a
warning is logged; the rest of the preview or import continues.

Expansion runs after the category is derived and before the metadata hash, so changing the template
reads as a metadata change on re-run — which is what makes backfilling URLs onto an existing corpus
a re-index rather than a re-import. It applies wherever the value came from, defaults or rules,
because by then they have resolved to one string.

Two details worth fixing now rather than later:

- **Frontmatter must be stripped from the body after parsing.** Leaving it in means every chunk of
  every page starts with YAML, which is noise in retrieval and noise in the excerpt shown under a
  citation.
- **`skip: true` belongs in the rules.** Excluding `**/internal/**` at ingest is the cheap, honest
  way to keep internal content out of a public store — and it composes with the store-as-boundary
  decision from the schema doc rather than competing with it.

---

## 7. Category — the source's folder structure

Category is derived by ingest, not typed by a human. Every source type must define what "relative
folder path from the root" means for it, because that mapping is what makes the field consistent
enough to filter and browse.

| Source | `category` derived from |
|---|---|
| Local folder / zip | Directory path relative to the configured root |
| Git repo | Repo-relative directory path, after `root` is stripped |
| Sitemap / URL crawl | URL path minus the final segment, relative to the base path |
| Zendesk | `category / section` |
| Confluence | Space key + ancestor page titles |
| Notion | Parent page chain |
| CSV / JSONL | A designated column |
| HTTP JSON | A configured field path |

### Normalisation

Deterministic, because it feeds a hash and a filter:

- Forward slashes always; no leading or trailing separator.
- A document at the root gets `""` — an empty string, not null, so "root" is a browsable value
  rather than a gap. Reserve null for "this source doesn't do categories".
- Case and spacing preserved as-is. Slugging `Getting Started` → `getting-started` breaks the
  round-trip back to the source and surprises people reading the tree.
- `root` scopes both discovery and the category base — most repos keep docs under `docs/`, which
  nobody wants prefixed onto all 1,500 categories. Anything outside `root` isn't part of the source
  at all, so one option does both jobs.
- `maxDepth` (default unlimited) limits discovery by directory depth relative to `root`, or to the
  source directory when `root` is unset. `0` imports only files directly in that directory; `1`
  also includes files in immediate child directories. Files below the limit are not counted as
  discovered or skipped.

```json
{ "root": "docs", "maxDepth": 3 }

docs/guides/auth/jwt.md   →  "guides/auth"
docs/index.md             →  ""              (root of the source)
docs/a/b/c/d/e.md         →  not discovered  (deeper than maxDepth)
src/README.md             →  not discovered  (outside root)
```

The URL-crawl equivalent is the configured base path, which plays exactly the same role — there's
one concept here, not two.

### The filtering problem, and `categoryPath`

A nested `category` breaks exact-match filtering, which is the only operator Gemini's docs actually
demonstrate. `category="guides"` does **not** match `guides/auth`, so "search everything under
guides" — the obvious thing to want — silently returns a subset.

Push a companion list alongside it: every ancestor prefix, including itself.

```
guides/auth/jwt.md
  category      = "guides/auth"                 (string — display, exact match on the leaf)
  categoryPath  = ["guides", "guides/auth"]     (stringList — subtree match)
```

`categoryPath:"guides"` then selects the whole subtree via the `:` has-operator, and
`category="guides/auth"` still pins one folder exactly. Both are cheap: the list is at most as long
as the path is deep.

This does depend on `:` working against `stringListValue`, which is probe #3 in
[METADATA_SCHEMA.md §7](./METADATA_SCHEMA.md). **Run that probe before building the tree UI.** If it
fails, the fallback is a flat `category` derived from the first path segment with the full path
kept in a local-only `path` column — browsable, but no subtree filtering.

### The tree needs rollup counts

`document_categories()` today is a flat `GROUP BY category`, which was right for flat values and
produces an unusable list once categories are paths — hundreds of rows, most with one document.

The facets endpoint should return the tree with two counts per node: **own** (documents directly in
that folder) and **total** (own + all descendants). Without the rollup, a parent folder whose
documents all live in subfolders reads as empty, and the tree looks broken.

```
guides            412
  auth             38
  perf             21
  api             104
    autoquery      44
```

---

## 8. Extraction is where retrieval quality is won

The least glamorous stage and the one that decides whether answers are good. A docs page run
through a naive HTML-to-text converter yields a chunk that is 60% navigation, and every such chunk
competes with real content for retrieval slots.

- **Scope the content.** A configurable selector per source (`main`, `article`, `.content`),
  defaulting to a readability-style heuristic. Getting this right once per site is worth more than
  any amount of chunking tuning.
- **Strip boilerplate** — nav, header, footer, sidebar, cookie banners, "Edit this page on GitHub",
  "Was this helpful?" widgets.
- **Preserve structure** — headings, lists, tables and code fences all survive to markdown. Heading
  structure is what makes a chunk self-describing.
- **Drop near-empty documents.** Below a configurable word count (default ~25 words of prose) a page
  is nav furniture; indexing it only adds noise. Report them in the run summary as skipped, so it's
  visible rather than silent.
- **Normalise whitespace**, and resolve relative links against the page URL so a citation excerpt
  doesn't contain dead references.
- **Per-source chunking** (§3.3 of the main doc): `reference`/`api` want smaller chunks with more
  overlap; `guide`/`policy` want larger ones that keep an argument intact.

---

## 9. Scheduling and incremental sync

There's no scheduler in the codebase; the closest idiom is `watch_config_files` and the extension's
own `UploadWorker`. A single asyncio task that wakes on an interval and runs due sources fits that
style and needs nothing new.

Incremental support per source is what makes a nightly sync nearly free:

| Source | Cheap change detection |
|---|---|
| Git | `git diff --name-status <lastSha> HEAD` — only changed paths are even read |
| HTTP / sitemap | `If-None-Match` / `If-Modified-Since` per URL, ETags kept in `cursor` |
| SharePoint / Drive | native delta / change tokens |
| Zendesk / Notion / Confluence | `start_time` / `last_edited_time` filters |
| Local folder | mtime + size, hash only when they differ |

Git is worth calling out: it's the only source where discover itself is incremental, so a repo of
5,000 files with 3 changed costs three fetches and one `git pull`.

---

## 10. Recommended sources

### Ship these four first

| Source | Why | Incremental | Needs |
|---|---|---|---|
| **Local folder** | The obvious one, and the fastest path from "I have docs" to a working store. Directory → `category` already exists conceptually. | mtime + hash | — |
| **Zip / archive upload** | Same value without server filesystem access — works for hosted deployments and for someone who just has an export. One request, thousands of files. | n/a | stdlib |
| **Sitemap / URL crawl** | *The* organisational case: "index docs.acme.com". Also the only source that naturally supplies `sourceUrl`, which is what makes citations link somewhere useful. | ETag | aiohttp |
| **Git repo** | Best incremental story of any source, and the right answer for anyone whose docs are markdown in a repo — which is most developer-facing orgs. Also how you'd keep the ServiceStack docs store current. | `git diff` | `git` CLI |

Those four cover the large majority of "import our documentation" and share one pipeline.

### Then, by demand

| Source | Notes |
|---|---|
| **Zendesk Help Center** | The support-KB case, and the one that pairs with a public widget. Clean API, `section` → `category`, article URL → `sourceUrl`. |
| **Confluence** | Enterprise wiki default. Space → `category`, CQL for discovery, `lastModified` incremental. |
| **Notion** | Common for smaller orgs. Blocks API needs a markdown converter; `last_edited_time` incremental. |
| **GitHub Issues / Discussions** | Underrated — answers "has anyone hit this before", which docs never cover. |
| **Discourse** | Community forums, same shape as above. |
| **SharePoint / OneDrive** | MS-shop table stakes. Graph delta queries are the best incremental of any API here. |
| **Google Drive** | `changes` API gives proper delta. Mixed formats mean it leans hardest on extraction. |

### Two generic escape hatches

Worth more than several bespoke connectors, and cheap:

- **HTTP JSON API** — configurable endpoint, pagination, a path to the item array, and a field map
  to content + metadata. Covers in-house systems, product catalogues, and any SaaS you haven't
  written a connector for. Same role `tags` plays in the metadata schema.
- **CSV / JSONL** — one row per document, columns mapped to metadata. The universal fallback: every
  system on earth can export CSV, and it makes "we have 4,000 support macros in a spreadsheet" a
  five-minute job.

### Explicitly not

- **Slack / chat history** — high volume, low signal, and a confidentiality minefield. Sounds
  appealing, degrades retrieval.
- **Whole-site crawl without a sitemap or path restriction** — an unbounded crawler is a way to
  index someone's search results and pagination. Require a sitemap, a path prefix, or an explicit
  depth cap.

---

## 11. Security

- **Local folder** is confined to a set of trusted roots. An ingest source is not a licence to
  read `/etc`. Two tiers, because the two audiences have opposite needs:

  - **Admins are exempt.** It is their machine, and preview lists every file before anything is
    read into a store. `ctx.is_admin()` is `True` whenever no auth provider is configured, so a
    single-user desktop install imports from anywhere with nothing to set up. That also means a
    self-hosted deployment which has not yet wired up auth is effectively unrestricted — turning
    auth on is what makes the rail load-bearing.
  - **Everyone else** is held to `gemini.importRoots` in the deployment-wide config at
    `~/.llms/user/default/config.json`. `default` is the anonymous user and the tail of the
    preference cascade, which is what makes that file the global tier rather than one person's
    settings. A bare top-level `importRoots` is accepted too.

  ```json
  {
    "gemini": {
      "importRoots": ["/srv/docs", "~/knowledgebase", "$WORKSPACE"]
    }
  }
  ```

  `~` expands, and `$WORKSPACE` / `$TEMP` resolve through the same aliases the server uses
  elsewhere. With nothing configured this falls back to `ctx.resolve_allowed_directories()`, so a
  deployment that never writes the file behaves exactly as it did before. The file is re-read on
  mtime change — adding a root does not need a restart.

  Two details the check depends on: both sides are `realpath`-ed, so a symlink planted inside a
  trusted root cannot read outside it; and the prefix test includes the separator, so `/srv/docs`
  does not grant `/srv/docs-private`. The check runs on **every run**, not only at create, so a
  source an admin saved does not become a standing read primitive for everyone else.
- **Crawling**: respect `robots.txt`, send an identifying user-agent, cap concurrency and rate,
  restrict to the configured host and path prefix.
- **SSRF**: block private and link-local address ranges by default. A source URL is attacker-adjacent
  input the moment a non-admin can create one.
- **Secrets**: source tokens follow the `github_auth` precedent — a config file under
  `ctx.get_user_path()`, or an env var reference in `config` rather than the value itself. Never
  return them from the API.
- **Creating and running sources is an admin operation**, gated the same way the mutating routes
  added in Phase 0 are.

---

## 12. API surface

```
GET    /sources?filestoreId=1
POST   /sources                        { type, filestoreId, config, rules, schedule, … }
PATCH  /sources/{id}
DELETE /sources/{id}                   ?documents=keep|tombstone|remove

POST   /sources/{id}/run               { "dryRun": true }   → run summary, no writes
POST   /sources/{id}/run               { "confirm": "<dryRunId>" }
POST   /sources/{id}/cancel
GET    /sources/{id}/runs              → history with counts
GET    /sources/{id}/runs/{runId}/log

GET    /source-types                   → available types + whether each is usable here
```

`/source-types` reporting availability is what lets the UI say *Git — unavailable, needs `git` on
PATH* instead of offering a source that fails on first run.

CLI equivalents matter for a project that started as a CLI — `llms --import-folder ./docs --store 1`
and `llms --sync-source 3` make ingest scriptable and cron-able without the UI.

---

## 13. Rollout

1. **Pipeline + `source`/`source_run` tables + source-key identity.** Fixes the change-detection bug
   in §3 on its own, before any new source exists.
2. **Local folder + zip.** Exercises the whole pipeline with no network, no auth, no extraction
   subtleties.
3. **Metadata rules + dry-run preview.** The point at which ingest stops creating a bulk-edit
   backlog.
4. **Sitemap crawl + HTML extraction.** The first source that needs §7 to be good, and the one that
   makes `sourceUrl` real.
5. **Git.** Small once the pipeline exists, and the best incremental story.
6. **Scheduling + delete detection with rails.** Turns imports into a corpus that stays current.
7. **Connectors and the generic HTTP/CSV sources**, by demand.
