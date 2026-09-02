#!/usr/bin/env python3
"""
Probe what Gemini File Search actually implements for `metadata_filter`.

Google's File Search docs demonstrate exactly one filter form (`author="Robert Graves"`) and
delegate the rest to AIP-160. The metadata schema in METADATA_SCHEMA.md and the category design in
INGEST.md both depend on operators that are therefore unverified. This settles it empirically.

Creates a scratch store, uploads three fixture documents covering every CustomMetadata value type,
runs each filter expression, and reports which documents came back. Deletes the store afterwards.

Cost: three embedding passes. Runtime: a couple of minutes, mostly upload polling.

    GOOGLE_API_KEY=... python3 probe_filters.py
    python3 probe_filters.py --keep     # leave the store behind for manual poking
"""

import os
import sys
import time

try:
    from .client import GeminiClient
except ImportError:  # direct execution: python3 probe_filters.py
    from client import GeminiClient

STORE_NAME = "llms-filter-probe"

# One fact per document, phrased so a grounded answer has to cite the document it came from.
# Metadata deliberately spans string, numeric and stringList values.
DOCS = [
    {
        "key": "alpha",
        "text": "The Alpha widget costs 100 dollars and ships from Perth.",
        "meta": {
            "docType": "guide",
            "status": "published",
            "locale": "en",
            "updatedAt": 1755648000,          # 2025-08-20
            "versions": ["v7", "v8"],
            "category": "guides/auth",
            "categoryPath": ["guides", "guides/auth"],
        },
    },
    {
        "key": "beta",
        "text": "The Beta widget costs 200 dollars and ships from Sydney.",
        "meta": {
            "docType": "faq",
            "status": "deprecated",
            "locale": "en",
            "updatedAt": 1600000000,          # 2020-09
            "versions": ["v6"],
            "category": "guides/perf",
            "categoryPath": ["guides", "guides/perf"],
        },
    },
    {
        "key": "gamma",
        "text": "The Gamma widget costs 300 dollars and ships from Darwin.",
        "meta": {
            "docType": "reference",
            "status": "published",
            "locale": "ja",
            "updatedAt": 1755648000,
            "versions": ["v8"],
            "category": "api",
            "categoryPath": ["api"],
        },
    },
]

QUESTION = "List every widget mentioned in the documents and what it costs."

# (expression, expected keys, what it establishes)
PROBES = [
    ('docType="guide"',                                    {"alpha"},                  "equality (documented baseline)"),
    ('docType="guide" AND status="published"',             {"alpha"},                  "AND"),
    ('status="published" AND locale="en"',                 {"alpha"},                  "AND across two fields"),
    ('versions:"v8"',                                      {"alpha", "gamma"},         ": on stringListValue  ← versions model"),
    ('categoryPath:"guides"',                              {"alpha", "beta"},          ": for category subtree  ← category model"),
    ('category="guides/auth"',                             {"alpha"},                  "exact match on a path value"),
    ('updatedAt > 1700000000',                             {"alpha", "gamma"},         "numeric comparison  ← staleness filter"),
    ('updatedAt >= 1755648000 AND status="published"',     {"alpha", "gamma"},         "numeric + AND"),
    ('status="published" AND (docType="guide" OR docType="reference")', {"alpha", "gamma"}, "grouping + OR"),
    ('NOT status="deprecated"',                            {"alpha", "gamma"},         "negation"),
    ('-status="deprecated"',                               {"alpha", "gamma"},         "negation, - form"),
]

# Widen this until it errors, to find the undocumented ceiling on metadata keys per document.
KEY_COUNT_PROBE = 25


def to_custom_metadata(meta):
    out = []
    for k, v in meta.items():
        if isinstance(v, bool):
            out.append({"key": k, "string_value": str(v).lower()})
        elif isinstance(v, (int, float)):
            out.append({"key": k, "numeric_value": v})
        elif isinstance(v, list):
            out.append({"key": k, "string_list_value": {"values": v}})
        else:
            out.append({"key": k, "string_value": str(v)})
    return out


def upload(client, store, doc, tmpdir):
    path = os.path.join(tmpdir, f"{doc['key']}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc["text"])
    op = client.file_search_stores.upload_to_file_search_store(
        file_search_store_name=store.name,
        file=path,
        config={
            "display_name": f"{doc['key']}.txt",
            "custom_metadata": to_custom_metadata(doc["meta"]),
        },
    )
    while not op.done:
        time.sleep(3)
        op = client.operations.get(op)
    if op.error:
        raise RuntimeError(f"upload of {doc['key']} failed: {op.error}")
    return op.response.document_name


def cited_keys(response):
    """Which fixture documents the answer was actually grounded in."""
    keys = set()
    for cand in response.candidates or []:
        gm = getattr(cand, "grounding_metadata", None)
        for chunk in (getattr(gm, "grounding_chunks", None) or []) if gm else []:
            rc = getattr(chunk, "retrieved_context", None)
            title = (getattr(rc, "title", "") or "") if rc else ""
            for d in DOCS:
                if d["key"] in title.lower():
                    keys.add(d["key"])
    return keys


def run_probe(client, store, model, expr):
    tool = {"file_search": {
        "file_search_store_names": [store.name],
        "metadata_filter": expr,
    }}
    res = client.models.generate_content(
        model=model, contents=QUESTION, config={"tools": [tool]},
    )
    return cited_keys(res), res


def probe_key_limit(client, store, tmpdir):
    meta = {f"k{i:02d}": f"v{i}" for i in range(KEY_COUNT_PROBE)}
    try:
        upload(client, store, {"key": "keylimit", "text": "Key count probe.", "meta": meta}, tmpdir)
        return f"{KEY_COUNT_PROBE} keys accepted (no ceiling found at this size)"
    except Exception as e:
        return f"failed at {KEY_COUNT_PROBE} keys: {str(e)[:160]}"


def main():
    keep = "--keep" in sys.argv
    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        sys.exit("Set GOOGLE_API_KEY or GEMINI_API_KEY")

    model = os.getenv("PROBE_MODEL", "gemini-flash-latest")
    client = GeminiClient(api_key=api_key)

    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="probe-")

    print(f"Creating scratch store '{STORE_NAME}'…")
    store = client.file_search_stores.create(config={"display_name": STORE_NAME})
    try:
        for doc in DOCS:
            print(f"  uploading {doc['key']}…")
            upload(client, store, doc, tmpdir)

        print("\nWaiting for documents to become searchable…")
        time.sleep(10)

        # Baseline: no filter at all should retrieve all three. If it doesn't, retrieval - not
        # filtering - is the problem, and every result below would be noise.
        base, _ = run_probe(client, store, model, None)
        print(f"Baseline (no filter): cited {sorted(base) or 'nothing'}")
        if len(base) < len(DOCS):
            print("  ! Not all fixtures were retrieved unfiltered. Results below are unreliable;")
            print("    try raising top_k or rephrasing QUESTION before trusting them.\n")

        width = max(len(e) for e, _, _ in PROBES)
        print(f"\n{'expression'.ljust(width)}  {'got':22} {'want':22} result")
        print("-" * (width + 60))

        results = []
        for expr, want, why in PROBES:
            try:
                got, _ = run_probe(client, store, model, expr)
                ok = got == want
                verdict = "OK" if ok else ("REJECTED/EMPTY" if not got else "MISMATCH")
            except Exception as e:
                got, ok, verdict = set(), False, f"ERROR {str(e)[:60]}"
            results.append((expr, ok, why, verdict))
            print(f"{expr.ljust(width)}  {str(sorted(got)):22} {str(sorted(want)):22} {verdict}")

        print("\n" + "=" * 72)
        print("What this means for the design\n")
        for expr, ok, why, _ in results:
            print(f"  [{'✓' if ok else '✗'}] {why:44} {expr}")

        print(f"\nKey-count ceiling: {probe_key_limit(client, store, tmpdir)}")

        failed = [w for _, ok, w, _ in results if not ok]
        if failed:
            print("\nFallbacks now required (see PLAN.md B1):")
            for w in failed:
                print(f"  - {w}")
        else:
            print("\nFull AIP-160 grammar available. Schema as designed.")

    finally:
        if keep:
            print(f"\nLeaving store in place: {store.name}")
        else:
            print(f"\nDeleting scratch store {store.name}…")
            client.file_search_stores.delete(name=store.name, config={"force": True})


if __name__ == "__main__":
    main()
