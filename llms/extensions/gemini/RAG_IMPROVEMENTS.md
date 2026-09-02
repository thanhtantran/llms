# Gemini RAG — review & improvement proposal

Scope: `llmspy/gemini` (extension) plus the parts of `ServiceStack/llms` it depends on
(`llms/extensions/providers/google.py`, `llms/extensions/app/`, `llms/main.py`).

Audience for the recommendations: **Organization customers who want to import their docs /
knowledgebase and let staff *and their own customers* query it.**

---

## 1. What exists today

The extension is a well-shaped, complete-for-a-developer implementation of Gemini
**File Search Stores**:

| Area | Implementation |
|---|---|
| Storage | SQLite (`filestore`, `document`) at `.llms/user/{user}/gemini/gemini.sqlite`, self-migrating via `add_missing_columns` |
| Upload | `POST /filestores/{id}/upload?category=` multipart → SHA-256 → `~/.llms/cache/{2}/{hash}.{ext}` + `.info.json` → DB row |
| Indexing | `UploadWorker` background thread, batch of 10, `upload_to_file_search_store` + operation polling, writes back `name`/`state`/`sizeBytes` |
| Metadata | `custom_metadata` = `id` (numeric), `hash`, `category` |
| Reconciliation | `POST /filestores/{id}/sync` — matches by hash then name, reports *Missing from Local / Missing from Gemini / Missing Metadata / Metadata Mismatch / Unmatched Fields / Duplicates*, and repairs local rows |
| Query | `createNewChat()` emits an OpenAI-shaped `file_search` tool; `google.py:347` maps it straight to Gemini's `file_search` tool. `metadata_filter` supports `category=x` or `hash=y` |
| Citations | `GeminiFooter` renders `thread.providerResponse.candidates[0].groundingMetadata.groundingChunks`, expandable, linked to the cached file |
| UI | Store list, category tiles with counts/sizes, paged doc grid, retry/delete, sync report, per-extension model override |

It's genuinely good plumbing. Everything below is about the distance between *"a developer can
index their docs"* and *"an organization can run their support knowledgebase on this"*.

---

## 2. Bugs & correctness gaps (fix these first — they're cheap and they're trust)

### 2.1 Streaming answers lose all citations 🔴
`google.py` sets `context["providerResponse"] = obj` only in the **non-streaming** branch
(~line 644). The SSE branch (~lines 180–270) accumulates text, reasoning, usage and
`functionCall` parts but never touches `groundingMetadata`, and never sets `providerResponse`.

Since the app streams by default, **`GeminiFooter` renders nothing for a normal chat turn**.
The single most valuable RAG feature in the product only appears when streaming is off.

*Fix:* accumulate `candidate.groundingMetadata` (chunks + supports) across chunks in the stream
loop and set `context["providerResponse"]` on completion.

### 2.2 Sources are thread-scoped, not message-scoped 🔴
`providerResponse` is one JSON column on the **thread** (`app/db.py:65`), overwritten every turn.
Scroll up in a 10-message conversation and only the last answer has sources. In a support
context every answer needs its own citations.

*Fix:* attach `groundingChunks`/`groundingSupports` to the assistant message
(`messages[].groundingMetadata`), keep `providerResponse` for debugging only.

### 2.3 Chunk text gets replaced by a number 🟠
`truncate_long_strings(provider_response, max_length=10000)` (`app/__init__.py:1718`) replaces any
string over 10K with `"(12345)"`. A long `retrievedContext.text` therefore renders in the Sources
panel as a length. Exclude `groundingMetadata` from truncation, or truncate to a prefix rather
than a count.

### 2.4 No inline citations 🟠
`groundingSupports` (segment start/end offsets + `groundingChunkIndices`) is discarded entirely,
so answers can't render `[1]`, `[2]` markers against the sentences they support. For a
customer-facing widget this is the difference between "an AI said something" and "here's the
answer, and here's the doc it came from."

### 2.5 Document identity is content-hash, which is the wrong key 🟠
`upload_to_filestore` looks up `find_document({"hash": sha256})` and, if found, **deletes and
re-uploads**. Consequences:

- Re-uploading an *unchanged* file → needless delete + re-index + re-embed spend.
- Re-uploading a *changed* file (new hash) → a **second** doc; the stale version stays in the
  store forever and keeps getting retrieved. Silent answer rot.

*Fix:* identity should be a stable source key — `(filestoreId, category, displayName)` or an
explicit `sourceUrl`/`ref`. Hash then becomes the *change detector*: same key + same hash = skip,
same key + new hash = replace, key absent from source = tombstone.

### 2.6 Worker throughput & liveness 🟠
`UploadWorker` is a single daemon thread doing one document at a time with `time.sleep(5)`
polling. A 5,000-page docs import is measured in hours. Also:

- `completed` is an unbounded in-memory list.
- `if len(unprocessed_docs) == 0: break` exits the loop **without** clearing `self.running` in that
  path (the `finally` covers it, but the two exit paths differ) — worth normalising.
- `g_worker` is one global for all users; no per-store progress, no cancel, no ETA, no backoff on
  429s.

*Fix:* bounded concurrency (e.g. 4–8 in flight), exponential backoff, a `job` table with
progress/status so the UI can show "1,240 / 5,000 indexed, ~6 min remaining", plus pause/cancel.

### 2.7 No auth enforcement on extension routes 🔴 (deployment-dependent)
`main.py` wraps extension handlers with `on_request` only — there is **no `check_auth`**.
The gemini handlers call `ctx.get_username(request)`, which returns `None` for an anonymous
caller, and `db.get_user_filter(None)` resolves to `WHERE user IS NULL` — the shared/default
scope. So with auth enabled, an unauthenticated request can still **list, create, upload to and
`DELETE`** the shared filestores.

*Fix:* `ctx.assert_username(request)` on every mutating handler, plus a role check
(`admin` for create/delete/upload) once roles exist.

### 2.8 Small things
- `ctx.dbg(f"Remote doc not found locally: ")` — missing `{info}`.
- `duplicate_docs` looks up `local_doc_hashes[hash]`, which only ever holds one row per hash, so
  the report names the local doc rather than the duplicated remote docs.
- `total_remote` is computed and only logged.
- `document.tags` (JSON) is declared and never written or used.

---

## 3. What Organization customers need that isn't there

### 3.1 Ingestion is the #1 blocker
Today the only way in is multipart upload — the UI file picker or `curl -F`. An org with a
knowledgebase has it in a **docs site, a git repo, Confluence, Notion, SharePoint, Zendesk,
Discourse or a helpdesk export**. Nobody is going to hand-pick 4,000 files.

Needed, roughly in value order:

1. **Folder / zip import** with directory structure → `category`, include/exclude globs, and a
   dry-run preview ("1,412 files, 38 MB, 6 categories").
2. **URL / sitemap crawl** — point it at `docs.acme.com/sitemap.xml`, fetch, convert HTML→markdown
   (strip nav/footer), store `sourceUrl` per doc.
3. **Git repo sync** — clone/pull, glob `**/*.md`, map paths to categories, re-sync on a schedule
   or a webhook. (This is also how *you* would keep the ServiceStack docs store current.)
4. **Scheduled refresh + delete detection** — a source that dropped a page must tombstone the
   indexed doc, or the assistant keeps answering from removed content.
5. **Connectors** — Zendesk, Confluence, Notion, SharePoint/Graph, Discourse, GitHub Issues,
   Google Drive. Each is small once the ingestion pipeline above exists (fetch → normalise →
   `sourceUrl` + metadata → same worker).

### 3.2 Metadata is too thin to filter on
`custom_metadata` carries `id`, `hash`, `category`. Real orgs need
`product`, `version`, `locale`, `audience` (`public` | `internal`), `updatedAt`, `sourceUrl`.

That last pair unlocks two things that matter a lot:

- **`audience=public` filtering** so one store can serve both the internal staff assistant and the
  public widget without leaking internal runbooks.
- **Citations that link to the customer's real docs page** instead of downloading a cached blob
  from `/~cache/ab/ab12…md`. No org will ship a widget that cites an opaque hash URL.

Expose these as a per-store **schema** (declared fields) so the UI can render facet pickers and
build `metadata_filter` expressions instead of the current single `category=` / `hash=` string.

### 3.3 No chunking control
`upload_worker.process_doc` never sets `chunking_config`. API reference docs, long PDFs and short
FAQ entries want very different `max_tokens_per_chunk` / `max_overlap_tokens`. Make it a
per-store setting with sane presets (`prose` / `reference` / `faq` / `code`).

### 3.4 No org ownership model
`get_user_filter()` is binary: `user IS NULL` (shared) or `user = :user` (private). There is no
concept of a team, an owner, or a role. Practically, an org of 50 either:

- dumps everything into the anonymous shared bucket (and 2.7 above means anyone can delete it), or
- has 50 people each upload and pay to index their own copy of the same corpus.

Needed: `owner`, `visibility` (`private` | `org` | `public`), and roles (`admin` manages stores &
ingestion, `member` queries, `anonymous` queries published assistants only). Also a per-store
audit trail — who uploaded/deleted what, when.

### 3.5 Nothing measures whether the RAG is any good
There's no query log, no thumbs up/down, no "retrieved zero chunks" signal, no report of questions
the KB couldn't answer. This is the thing that turns a one-off setup into a renewal: an org wants
to open a dashboard and see *"deflection rate 61%, 340 questions this week, top 12 questions with
no matching content"* — that last list is a **content roadmap** they'd pay for on its own.

### 3.6 Cost & quota visibility
No per-store size accounting against Gemini's File Search quotas, no token/spend budget, no alert
before an import blows a limit. Check the current File Search store size limits and query pricing
and surface both in the store UI before customers discover them the hard way.

### 3.7 Provider lock-in
The whole feature is welded to `google.genai` and `fileSearchStores/...`. `llms` is otherwise
proudly multi-provider; an org evaluating this will ask what happens if they need OpenAI vector
stores, Azure AI Search, or self-hosted embeddings for data-residency reasons.

Worth extracting a thin `KnowledgeStore` interface (`create/upload/list/delete/sync/query-tool`)
with Gemini as the first implementation. The UI, ingestion pipeline, metadata schema, analytics
and widget then work for every backend — and "bring your own vector store" becomes a selling point
rather than an objection.

---

## 4. The widget idea — yes, and it's the strongest commercial angle here

**Verdict: do it.** It's the natural product on top of what's already built, and it's the reason
an org buys this rather than wiring the Gemini SDK up themselves. But it needs one new
first-class concept the extension doesn't have.

### 4.1 The missing concept: a published *Assistant*

A filestore is not a product. An **Assistant** is: a named, published configuration that binds
store(s) + model + persona + filters + limits + branding.

```
assistant
  id, publicId (unguessable slug), user/org, enabled
  name, greeting, suggestedQuestions[], placeholder
  filestoreIds[], metadataFilter        -- e.g. audience=public AND version=v8
  model, systemPrompt, temperature, maxOutputTokens
  theme { accent, logoUrl, position, mode }
  allowedOrigins[]                       -- https://acme.com, https://*.acme.com
  rateLimit { perIpPerMin, perSessionMsgs }, dailyTokenBudget
  requireCaptcha, escalation { webhookUrl | email | "none" }
  createdAt, updatedAt

conversation / message
  publicId, assistantId, sessionId, ip hash, origin, userAgent
  role, content, groundingChunks, tokens, cost, latencyMs
  feedback (+1/-1), resolved, escalatedAt
```

### 4.2 Delivery surfaces (ship more than one — customers' sites differ)

| Surface | Embed | Use case |
|---|---|---|
| **Bubble widget** | `<script src="https://kb.acme.com/ext/gemini/public/{publicId}/widget.js" async></script>` | The default. Floating launcher + panel. |
| **Inline search box** | `<gemini-search data-id="{publicId}"></gemini-search>` | "Ask the docs" bar on a docs site. |
| **Iframe page** | `/ext/gemini/public/{publicId}/embed` | CSP-strict sites; zero JS trust required. |
| **Headless JSON/SSE API** | `POST /ext/gemini/public/{publicId}/chat` | Customers who want to build their own UI, or a Slack/Teams bot. |

Build `widget.js` as a **dependency-free ~10KB loader that mounts into a shadow root**. Shadow DOM
is non-negotiable: it stops the customer's CSS and yours from destroying each other, and it's why
the widget can be one script tag instead of an integration project. Don't ship Vue/Tailwind into
somebody else's page.

### 4.3 The server side is small because you already have it

`POST /ext/gemini/public/{publicId}/chat` (SSE) is a thin, hardened proxy:

1. Resolve `publicId` → assistant; 404 if disabled.
2. Validate `Origin`/`Referer` against `allowedOrigins`; emit matching CORS headers
   (**note: `main.py` has no CORS handling at all today — this needs adding**).
3. Rate-limit per IP + per session; enforce `dailyTokenBudget`; optional Turnstile on first message.
4. **Ignore everything the client sends except the message text and session id.** Model, tools,
   system prompt and `metadata_filter` come from the stored assistant, never the request — a
   public endpoint that accepts a client-supplied model is a free-inference faucet.
5. Build the `file_search` tool from the assistant config and call the existing
   `ctx.chat_completion(...)`. The `GEMINI_API_KEY` never leaves the server.
6. Stream tokens back; on completion emit citations from `groundingMetadata` (which requires
   §2.1 to be fixed first) and persist the turn.

### 4.4 What makes it *good* rather than just present

- **Inline `[1][2]` citations** linking to `sourceUrl` (§2.4 + §3.2). This is the whole trust story.
- **Grounded-only mode** — if `groundingChunks` is empty, don't let the model freestyle. Reply
  "I couldn't find that in the docs" + escalate. Hallucinating on a customer's support widget is
  the failure mode that gets the product removed.
- **Escalation**: 👍/👎 on each answer, and a "Talk to a human" button that POSTs the transcript to
  a webhook / email / Zendesk. Orgs need the exit ramp before they'll put it on a pricing page.
- **Answer caching** — normalise the question, cache for N hours per assistant. Docs widgets get
  the same 50 questions forever; this is a large, cheap cost win.
- **Cheap by default** — `gemini-flash-lite` as the default widget model with a configurable
  escalation to a bigger model.
- **Analytics dashboard** in the existing extension UI: volume, deflection, 👎 transcripts, and
  **zero-grounding queries as a content-gap list**.
- **A `noindex` / rel-canonical story** if you also render answers server-side — you don't want
  customers' widget answers competing with their own docs in search.

### 4.5 Hosting

Two options, and you already have the machinery for both:

- **Self-hosted** — the customer runs `llms` behind their own domain (`kb.acme.com`). Simplest
  story, no data leaves them, and it's how the Docker install already works.
- **Hosted** — ride the existing `publish` extension / llmspy.org account model to offer
  `assistant.llmspy.org/{publicId}`. Lower friction for a trial, and it gives you a natural
  metered SKU.

---

## 5. Suggested sequencing

**Phase 0 — trust (days, small diffs, big payoff)**
Stream `groundingMetadata` (§2.1) · per-message sources (§2.2) · stop truncating chunk text (§2.3) ·
inline citations from `groundingSupports` (§2.4) · `sourceUrl` on documents (§3.2) ·
`assert_username` on mutating routes (§2.7).

**Phase 1 — ingestion (the actual blocker)**
Folder/zip import with category mapping · sitemap/URL crawl → markdown · git sync · scheduled
refresh with delete detection · source-key identity instead of hash (§2.5) · chunking config
(§3.3) · concurrent worker with progress/cancel (§2.6).

**Phase 2 — organization**
Owner + visibility + roles · shared org stores · audit trail · per-store quotas and spend caps ·
richer metadata schema with facet UI (§3.2).

**Phase 3 — the widget**
Assistant entity · public SSE endpoint + CORS + rate limits · `widget.js` (shadow DOM) · inline
citation rendering · feedback + escalation · analytics dashboard.

**Phase 4 — reach**
Connectors (Zendesk / Confluence / Notion / SharePoint / Discourse / GitHub) · Slack & Teams bots ·
hosted assistants via `publish` · `KnowledgeStore` abstraction so it isn't Gemini-only (§3.7).

---

## 6. One-paragraph pitch this enables

> Point llms at your docs site, git repo or Confluence. It indexes them into Gemini File Search,
> keeps them in sync on a schedule, and gives you a one-line `<script>` tag that puts a cited,
> grounded support assistant on your website. Every answer links back to the page it came from.
> Every question your docs *couldn't* answer lands in a content-gap report.

That's a product. What's in the repo today is the (solid) first third of it.
