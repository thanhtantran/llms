"""Tests for staged crawl workspaces and import.json inheritance."""

import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import types
import unittest
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = "gemini_crawl_tests"
pkg = types.ModuleType(PKG)
pkg.__path__ = [str(ROOT)]
sys.modules[PKG] = pkg


def load(name):
    spec = importlib.util.spec_from_file_location(f"{PKG}.{name}", ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ingest = load("ingest")
crawl = load("crawl")


class CrawlTests(unittest.TestCase):
    def test_site_name_includes_port_with_dash(self):
        self.assertEqual(crawl.site_name("http://localhost:5000/docs"), "localhost-5000")
        self.assertEqual(crawl.site_name("https://Docs.Example.org/a"), "docs.example.org")

    def test_query_strings_get_stable_distinct_folders(self):
        a = crawl.page_relative_path("https://example.org/search?q=one")
        b = crawl.page_relative_path("https://example.org/search?q=two")
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("search--"))
        self.assertTrue(a.endswith(".md"))

    def test_page_paths_use_clean_markdown_files_and_directory_indexes(self):
        self.assertEqual(crawl.page_relative_path("https://example.org/"), "index.md")
        self.assertEqual(crawl.page_relative_path("https://example.org/docs/"), "docs/index.md")
        self.assertEqual(crawl.page_relative_path("https://example.org/docs/templates/next-rsc"),
                         "docs/templates/next-rsc.md")
        self.assertEqual(crawl.page_relative_path("https://example.org/about.html"), "about.md")

    def test_canonical_url_applies_scope_paths_and_query_policy(self):
        options = {"include": ["/docs/**"], "exclude": ["/docs/archive/**"], "query": {
            "mode": "allow", "allow": ["version", "lang"], "exclude": ["utm_*"],
        }}
        start = "https://example.org/docs/"
        self.assertEqual(
            crawl.canonical_url("guide?utm_source=x&lang=en&version=v2", start, options),
            "https://example.org/docs/guide?lang=en&version=v2",
        )
        self.assertIsNone(crawl.canonical_url("/docs/archive/old", start, options))
        self.assertIsNone(crawl.canonical_url("/docs-private/page", start, options))
        self.assertIsNone(crawl.canonical_url("https://other.org/docs/page", start, options))

    def test_ordered_rules_support_follow_only_and_query_matching(self):
        options = {"rules": [
            {"match": "/sitemap/**", "action": "followOnly"},
            {"queryString": True, "action": "exclude"},
        ]}
        self.assertEqual(crawl.crawl_action("https://x/sitemap/a", options), "followOnly")
        self.assertEqual(crawl.crawl_action("https://x/page?q=1", options), "exclude")
        self.assertEqual(crawl.crawl_action("https://x/page", options), "save")

    def test_crawl_rule_schema_and_server_validation_share_the_contract(self):
        valid = [{"match": "/archive/**", "action": "exclude"},
                 {"queryString": True, "action": "followOnly"}]
        self.assertIs(crawl.validate_crawl_rules(valid), valid)
        self.assertEqual(crawl.CRAWL_RULE_SCHEMA["type"], "array")
        self.assertEqual(len(crawl.CRAWL_RULE_SCHEMA["items"]["oneOf"]), 2)
        with self.assertRaisesRegex(ValueError, "needs a path glob"):
            crawl.validate_crawl_rules([{"action": "exclude"}])

    def test_parser_extracts_canonical_robots_and_nofollow_links(self):
        parser = crawl.PageParser()
        parser.feed('''<html><head><link rel="canonical" href="/real"><meta name="robots" content="noindex,nofollow"></head>
            <body><a href="/one">One</a><a href="/two" rel="nofollow">Two</a></body></html>''')
        self.assertEqual(parser.canonical, "/real")
        self.assertEqual(parser.robots, {"noindex", "nofollow"})
        self.assertEqual(parser.links, [("/one", False), ("/two", True)])

    def test_directory_manifest_overwrites_root_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            pathlib.Path(root, "import.json").write_text(json.dumps({"metadata": {
                "defaults": {"product": "Docs", "status": "draft", "tags": ["root"]},
                "rules": [{"match": "**/*.md", "set": {"locale": "en"}}],
            }}))
            pathlib.Path(root, "guides").mkdir()
            pathlib.Path(root, "guides", "import.json").write_text(json.dumps({"metadata": {
                "defaults": {"status": "published", "tags": ["guides"]},
            }}))
            cfg = crawl.effective_manifest(root, "guides/auth/page.md")
            self.assertEqual(cfg["metadata"]["defaults"], {
                "product": "Docs", "status": "published", "tags": ["guides"],
            })
            self.assertEqual(len(cfg["metadata"]["rules"]), 1)

    def test_saving_metadata_preserves_crawl_and_transforms(self):
        with tempfile.TemporaryDirectory() as root:
            crawl.write_json(os.path.join(root, "import.json"), {
                "crawl": {"url": "https://example.org"},
                "transforms": [{"pattern": "old", "replacement": "new"}],
            })
            crawl.save_metadata(root, {"defaults": {"product": "Docs"}, "rules": []})
            cfg = crawl.read_json(os.path.join(root, "import.json"))
            self.assertEqual(cfg["crawl"]["url"], "https://example.org")
            self.assertEqual(len(cfg["transforms"]), 1)

    def test_transforms_apply_only_to_matching_markdown(self):
        with tempfile.TemporaryDirectory() as root:
            pathlib.Path(root, "page.md").write_text("keep OLD", encoding="utf-8")
            pathlib.Path(root, "other.txt").write_text("OLD", encoding="utf-8")
            changed = crawl.apply_transforms(root, [{
                "match": "**/*.md", "pattern": "old", "replacement": "new", "flags": "gi",
            }])
            self.assertEqual(changed, 1)
            self.assertEqual(pathlib.Path(root, "page.md").read_text(), "keep new")
            self.assertEqual(pathlib.Path(root, "other.txt").read_text(), "OLD")

    def test_transform_schema_and_server_validation_share_the_contract(self):
        valid = [{"match": "**/*.md", "pattern": "old$", "replacement": "new", "flags": "gim"}]
        self.assertIs(crawl.validate_transforms(valid), valid)
        self.assertEqual(crawl.TRANSFORM_SCHEMA["type"], "array")
        with self.assertRaisesRegex(ValueError, "unsupported flags"):
            crawl.validate_transforms([{"pattern": "old", "flags": "x"}])
        with self.assertRaisesRegex(ValueError, "is invalid"):
            crawl.validate_transforms([{"pattern": "[", "flags": "g"}])
        with self.assertRaisesRegex(ValueError, "replacement is invalid.*invalid group reference 1"):
            crawl.validate_transforms([{"pattern": "text", "replacement": "\\1", "flags": "g"}])

    def test_crawled_page_browser_only_lists_and_reads_generated_pages(self):
        with tempfile.TemporaryDirectory() as root:
            pathlib.Path(root, "guide").mkdir()
            pathlib.Path(root, "guide", "index.md").write_text("# Guide", encoding="utf-8")
            pathlib.Path(root, "import.json").write_text(json.dumps({
                "crawl": {"generated": ["guide/index.md"]},
            }), encoding="utf-8")
            pathlib.Path(root, "notes.md").write_text("private", encoding="utf-8")
            self.assertEqual(crawl.list_crawled_pages(root), ["guide/index.md"])
            self.assertEqual(crawl.read_crawled_page(root, "guide/index.md"), "# Guide")
            with self.assertRaisesRegex(ValueError, "required"):
                crawl.read_crawled_page(root, "notes.txt")
            with self.assertRaisesRegex(ValueError, "not found"):
                crawl.read_crawled_page(root, "notes.md")
            with self.assertRaisesRegex(ValueError, "not found"):
                crawl.read_crawled_page(root, "../index.md")

    def test_zip_directory_manifest_and_frontmatter_override_defaults(self):
        prose = " ".join(["documentation"] * 30)
        with tempfile.TemporaryDirectory() as root:
            archive = os.path.join(root, "docs.zip")
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("import.json", json.dumps({"metadata": {"defaults": {
                    "product": "Docs", "status": "draft", "tags": ["root"],
                }}}))
                zf.writestr("guides/import.json", json.dumps({"metadata": {"defaults": {
                    "status": "published", "tags": ["guides"],
                }}}))
                zf.writestr("guides/auth/page.md", f"---\ntags: [page]\n---\n{prose}")
            source = ingest.ZipSource(None, {"path": archive})
            plan = ingest.build_plan({"rules": {}, "category": {}}, source, {})
            self.assertEqual(len(plan.add), 1)
            doc = plan.add[0]
            self.assertEqual(doc["product"], "Docs")
            self.assertEqual(doc["status"], "published")
            self.assertEqual(doc["tags"], ["page"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
