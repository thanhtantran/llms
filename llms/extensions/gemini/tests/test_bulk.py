"""
Tests for bulk metadata edits.

The load-bearing one is `test_total_counts_documents_not_edits`: the number on the Apply button is
what a re-index will cost, and a multi-field edit that priced three changed fields on one document
as three embedding passes would be lying about money.

    python3 -m pytest gemini/tests/test_bulk.py
    python3 gemini/tests/test_bulk.py            # no pytest required
"""

import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))


def _stub_llms_db():
    """See test_ingest: `db.py` imports `llms.db`, which only exists inside the llms package."""
    try:
        import llms.db  # noqa: F401
        return
    except ImportError:
        pass
    import sys
    import types as _types

    pkg = _types.ModuleType("llms")
    dbmod = _types.ModuleType("llms.db")
    dbmod.DbManager = object
    for fn in ("order_by", "select_columns"):
        setattr(dbmod, fn, lambda *a, **k: "")
    dbmod.valid_columns = lambda columns, value: [
        x for x in (value if isinstance(value, list) else str(value).split(",")) if x in columns
    ]
    dbmod.to_dto = lambda ctx, row, cols: row
    pkg.db = dbmod
    sys.modules.setdefault("llms", pkg)
    sys.modules.setdefault("llms.db", dbmod)


def _load(name):
    _stub_llms_db()
    spec = importlib.util.spec_from_file_location(name, os.path.join(_HERE, "..", f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gdb = _load("db")


class FakeDB:
    """
    The parts of GeminiDB a bulk edit touches, with writes recorded instead of performed.

    Borrowing the real methods rather than reimplementing them is the point: these tests fail if
    the preview and the write ever stop agreeing.
    """

    BULK_COLUMNS = gdb.GeminiDB.BULK_COLUMNS
    BULK_LIST_COLUMNS = gdb.GeminiDB.BULK_LIST_COLUMNS
    bulk_apply = staticmethod(gdb.GeminiDB.bulk_apply)
    bulk_changes = staticmethod(gdb.GeminiDB.bulk_changes)
    bulk_preview = gdb.GeminiDB.bulk_preview
    bulk_update = gdb.GeminiDB.bulk_update
    document_summary = gdb.GeminiDB.document_summary

    def __init__(self):
        self.writes = []

    def update_document(self, id, document, user=None):
        self.writes.append((id, document))


class FilterDB:
    document_filter = gdb.GeminiDB.document_filter
    columns = {"document": {"id": "INTEGER", "category": "TEXT", "docType": "TEXT", "versions": "JSON", "tags": "JSON", "categoryPath": "JSON"}}

    def sql_filter(self, columns, query, args=None, user=None):
        params = dict(args or {})
        clauses = []
        for key, value in query.items():
            if key in columns:
                clauses.append(f"{key} = :{key}")
                params[key] = value
        return "WHERE 1=1" + (" AND " + " AND ".join(clauses) if clauses else ""), params


# Tags arrive from SQLite as JSON text, which is what bulk_apply has to cope with.
DOCS = [
    {"id": 1, "displayName": "a.md", "docType": "guide", "status": None, "tags": '["security"]'},
    {"id": 2, "displayName": "b.md", "docType": None, "status": "published", "tags": None},
    {"id": 3, "displayName": "c.md", "docType": "reference", "status": None, "tags": '["gdpr","security"]'},
    {"id": 4, "displayName": "d.md", "docType": None, "status": None, "tags": "[]"},
]


class BulkTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()

    # --- what the selection currently says ---------------------------------------------

    def test_summary_counts_values_and_gaps(self):
        s = self.db.document_summary(DOCS, ["docType", "tags"])
        self.assertEqual(s["count"], 4)
        self.assertEqual(
            s["fields"]["docType"],
            {"values": [{"value": "guide", "count": 1}, {"value": "reference", "count": 1}], "empty": 2},
        )
        # list fields count per value, and an empty list counts as empty rather than as a value
        self.assertEqual(
            s["fields"]["tags"],
            {"values": [{"value": "security", "count": 2}, {"value": "gdpr", "count": 1}], "empty": 2},
        )
        self.assertEqual(s["sample"], ["a.md", "b.md", "c.md", "d.md"])

    def test_summary_fields_none_is_everything_and_empty_is_nothing(self):
        self.assertEqual(set(self.db.document_summary(DOCS)["fields"]), set(FakeDB.BULK_COLUMNS))
        # The delete confirm asks for the names and the count only.
        bare = self.db.document_summary(DOCS, [])
        self.assertEqual(bare["fields"], {})
        self.assertEqual(bare["count"], 4)

    # --- the preview -------------------------------------------------------------------

    def test_fill_skips_documents_that_already_have_a_value(self):
        p = self.db.bulk_preview(DOCS, [{"field": "docType", "op": "fill", "value": "faq"}])
        self.assertEqual((p["change"], p["skipped"], p["same"]), (2, 2, 0))

    def test_total_counts_documents_not_edits(self):
        changes = [
            {"field": "docType", "op": "fill", "value": "faq"},
            {"field": "status", "op": "fill", "value": "draft"},
        ]
        p = self.db.bulk_preview(DOCS, changes)
        # Two fields change on documents 2 and 4, one on 1 and 3 - four documents, four embeds.
        self.assertEqual(p["change"], 4)
        self.assertEqual(p["fields"]["docType"]["change"], 2)
        self.assertEqual(p["fields"]["status"]["change"], 3)
        self.assertEqual(p["change"], len(self.db.bulk_update(DOCS, changes)))

    def test_preview_matches_the_write(self):
        for changes in (
            [{"field": "docType", "op": "set", "value": "faq"}],
            [{"field": "docType", "op": "clear"}],
            [{"field": "tags", "op": "add", "value": ["security"]}],
            [{"field": "tags", "op": "remove", "value": ["security"]}],
            [{"field": "tags", "op": "set", "value": ["api"]}, {"field": "status", "op": "fill", "value": "draft"}],
        ):
            with self.subTest(changes=changes):
                db = FakeDB()
                self.assertEqual(db.bulk_preview(DOCS, changes)["change"], len(db.bulk_update(DOCS, changes)))

    # --- the write ---------------------------------------------------------------------

    def test_one_write_per_document_carrying_every_changed_field(self):
        changed = self.db.bulk_update(DOCS, [
            {"field": "docType", "op": "fill", "value": "faq"},
            {"field": "tags", "op": "add", "value": ["security"]},
        ])
        # 1 and 3 have a docType already and already carry the tag: nothing to write, nothing to
        # re-index. That's the property that makes a backfill over a curated corpus safe.
        self.assertEqual(changed, [2, 4])
        self.assertEqual(dict(self.db.writes)[2], {"docType": "faq", "tags": ["security"]})

    def test_moving_a_document_keeps_categoryPath_in_step(self):
        self.db.bulk_update([{"id": 9, "category": "guides"}],
                            [{"field": "category", "op": "set", "value": "guides/auth"}])
        self.assertEqual(self.db.writes[0][1]["categoryPath"], gdb.category_ancestors("guides/auth"))

    def test_columns_outside_the_allow_list_are_refused(self):
        with self.assertRaises(Exception) as e:
            self.db.bulk_update(DOCS, [{"field": "uploadedAt", "op": "set", "value": "x"}])
        self.assertIn("not a bulk-editable", str(e.exception))

    def test_list_facets_use_json_membership_instead_of_scalar_equality(self):
        sql, params = FilterDB().document_filter({"tags": "redis", "versions": "v2", "docType": "guide"})
        self.assertIn("docType = :docType", sql)
        self.assertIn("json_each(document.tags)", sql)
        self.assertIn("json_each(document.versions)", sql)
        self.assertNotIn("tags = :tags", sql)
        self.assertEqual(params["json_tags_0"], "redis")
        self.assertEqual(params["json_versions_0"], "v2")

    def test_uncategorised_filter_matches_null_and_legacy_empty_categories(self):
        for query in ({"null": "category"}, {"category": ""}):
            with self.subTest(query=query):
                sql, _ = FilterDB().document_filter(query)
                self.assertIn("(category IS NULL OR category = '')", sql)
                self.assertNotIn("category = :category", sql)


if __name__ == "__main__":
    unittest.main(verbosity=2)
