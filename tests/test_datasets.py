"""Run with the project's Python: -B -m unittest discover -s tests -v."""

import copy
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

APP = Path(__file__).resolve().parents[1] / "capitaliq-automation" / "capitaliq-automation"
sys.path.insert(0, str(APP))
import dataset_workflow as datasets
import capitaliq_screener_export as exporter


def document():
    return {"dataset": "test", "screener_url": "https://www.capitaliq.spglobal.com/web/client?auth=inherit#office/screener?perspective=289",
            "confirmed": True, "criteria": [{"id": "types", "label": "Property Types", "kind": "select", "multiple": True,
                "complete": True, "options": [{"label": "Office", "value": "1219"}, {"label": "Multifamily", "value": "1231"}],
                "selection": {"mode": "all"}}]}


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.cfg = exporter.Settings.load(APP / "json/config.example.json")
        self.cfg.dataset_name = "test"
        self.cfg.screener_url = document()["screener_url"]
        self.cfg.criteria_file = Path("criteria.json")

    def test_dynamic_column_capacity(self):
        self.cfg.target_fields_per_file = 1000
        for rows, total, data in [(46403, 10, 8), (89934, 5, 3), (10000, 50, 48), (100, 200, 198)]:
            with self.subTest(rows=rows):
                actual = exporter.safe_batch_size(rows, self.cfg)
                self.assertEqual(actual, data)
                self.assertLessEqual((actual + self.cfg.id_columns) * rows, self.cfg.cell_limit)
        for rows in (0, 250001):
            with self.assertRaises(RuntimeError):
                exporter.safe_batch_size(rows, self.cfg)

    def test_confirmation_gate(self):
        d = document()
        datasets.validate_criteria(d, self.cfg)
        d["confirmed"] = False
        with self.assertRaises(ValueError):
            datasets.validate_criteria(d, self.cfg)

    def test_wrong_screen_and_unknown_option(self):
        d = document()
        d["screener_url"] = d["screener_url"].replace("289", "127420")
        with self.assertRaises(ValueError):
            datasets.validate_criteria(d, self.cfg)
        d = document()
        d["criteria"][0]["selection"] = {"mode": "values", "values": ["unknown"]}
        with self.assertRaises(ValueError):
            datasets.validate_criteria(d, self.cfg)

    def test_single_select_cannot_select_all(self):
        d = document()
        d["criteria"][0]["multiple"] = False
        with self.assertRaises(ValueError):
            datasets.validate_criteria(d, self.cfg)

    def test_fingerprint_changes_only_with_scope(self):
        d = document()
        original = datasets.criteria_fingerprint(d)
        d["criteria"][0]["current"] = "UI state changed"
        self.assertEqual(original, datasets.criteria_fingerprint(d))
        d["criteria"][0]["selection"] = {"mode": "values", "values": ["1219"]}
        self.assertNotEqual(original, datasets.criteria_fingerprint(d))

    def test_partition_isolation_and_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "json/config.json"
            raw = json.loads((APP / "json/config.example.json").read_text())
            raw["dataset_name"] = "test"
            datasets.save_json(config, raw)
            datasets.save_json(config.with_name("criteria.json"), document())
            paths = datasets.create_partitions(config, "Property Types")
            self.assertEqual(len(paths), 2)
            first = json.loads(paths[0].with_name("criteria.json").read_text())
            self.assertEqual(first["criteria"][0]["selection"]["values"], ["1219"])
            self.assertEqual(first["dataset"], "test/office")
            self.assertNotEqual(paths[0].parent, paths[1].parent)
            checkpoint = paths[0].with_name("progress.json")
            checkpoint.write_text('{"completed_batches": 7}')
            datasets.create_partitions(config, "Property Types")
            self.assertEqual(json.loads(checkpoint.read_text())["completed_batches"], 7)

    def test_dry_run_never_resumes_or_downloads_pending_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = replace(self.cfg, output_dir=root / "downloads", profile_dir=root / "profile", state_file=root / "json/progress.json")
            state = {"completed_batches": 2, "downloaded_fields": [], "pending_batch": {
                "number": 3, "fields": ["Population"], "file": None, "explicit_periods": True}}
            datasets.save_json(cfg.state_file, state)
            before = cfg.state_file.read_bytes()
            playwright = MagicMock()
            page = playwright.chromium.launch_persistent_context.return_value.pages.__getitem__.return_value
            page.url = cfg.screener_url
            names = ["close_stale_field_selector", "wait_for_screener", "run_screen", "wait_for_screen_calculation",
                     "normalize_selected_fields", "open_columns", "remove_all_non_identifier_fields", "apply_columns"]
            from contextlib import ExitStack
            with ExitStack() as stack:
                for name in names:
                    stack.enter_context(patch.object(exporter, name))
                stack.enter_context(patch.object(exporter, "prepare_dataset_criteria", return_value=document()))
                stack.enter_context(patch.object(exporter, "detect_row_count", return_value=46403))
                stack.enter_context(patch.object(exporter, "select_next_fields", return_value=["Population"]))
                download = stack.enter_context(patch.object(exporter, "export_and_download"))
                resume = stack.enter_context(patch.object(exporter, "resume_pending_download"))
                exporter.run(playwright, cfg, reset=False, dry_run=True)
                download.assert_not_called()
                resume.assert_not_called()
            self.assertEqual(before, cfg.state_file.read_bytes())
            report = json.loads(cfg.state_file.with_name("dry_run.json").read_text())
            self.assertEqual(report["status"], "selection-and-cleanup-passed")
            self.assertFalse(report["downloaded"])

    def test_legacy_field_selector_uses_exact_add_button(self):
        from legacy_selector import select_fields
        page, cfg, helpers = MagicMock(), MagicMock(), MagicMock()
        helpers.field_task_key = exporter.field_task_key
        helpers.task_description = exporter.task_description
        cfg.all_time_points = True
        cfg.identifier_fields = ["Institution Name", "Property Key"]
        with tempfile.TemporaryDirectory() as directory:
            cfg.state_file = Path(directory) / "progress.json"
            root = page.locator.return_value
            tree = root.locator.return_value.first
            snapshot = [{"path": "125061|125063|225434", "name": "Ticker", "visible": True,
                         "leaf": True, "expanded": None, "checked": "on"}]
            tree.evaluate.return_value = ["125061|125063|225434"]
            node = tree.locator.return_value
            node.locator.return_value.get_attribute.return_value = "on"
            panel = root.locator.return_value
            panel.locator.return_value.count.return_value = 0
            helpers.selected_field_names.return_value = cfg.identifier_fields + ["Ticker"]
            with patch("legacy_selector.wait_for_tree", return_value=snapshot):
                tasks = select_fields(page, cfg, set(), 1, None, helpers)
            self.assertEqual(tasks, [exporter.field_task_key("Ticker", "125061|125063|225434")])
            root.locator.assert_any_call("button:has(i.fa-caret-right)")

    def test_legacy_batch_skips_ticker_and_fills_remaining_capacity(self):
        from legacy_selector import select_fields
        with tempfile.TemporaryDirectory() as directory:
            cfg = MagicMock(all_time_points=True, ui_timeout_ms=1000)
            cfg.identifier_fields = ["Institution Name", "Property Key"]
            cfg.state_file = Path(directory) / "progress.json"
            datasets.save_json(cfg.state_file.with_name("field_cache.json"), {"Ticker": {"temporal": False, "node_path": "1"}})
            page, root, tree, helpers = MagicMock(), MagicMock(), MagicMock(), MagicMock()
            helpers.field_task_key = exporter.field_task_key
            helpers.task_description = exporter.task_description
            page.locator.return_value = root
            tree_holder = MagicMock()
            tree_holder.first = tree
            add = MagicMock()
            root.locator.side_effect = lambda selector: tree_holder if selector.startswith(".non-shopping-cart") else add
            fields = [("1", "Ticker"), ("2", "Trading Symbol & Exchange"), ("3", "Property Key"), ("4", "Property Unique ID")]
            active = {"path": "1"}
            cart, anchors, node_mocks = [], [], {}
            for path, name in fields:
                node, anchor = MagicMock(), MagicMock()
                anchor.click.side_effect = lambda p=path: (active.update(path=p), anchors.append(p))
                def locate(selector, a=anchor):
                    self.assertEqual(selector, ":scope > a", "Hidden checkbox spans must never be clicked")
                    return a
                node.locator.side_effect = locate
                node_mocks[path] = node
            def snapshot(script):
                if "selected_paths" in script:
                    raise AssertionError("Unexpected script")
                if "li.ui-igtree-node-nochildren" in script:
                    return [active["path"]]
                return [{"path": p, "name": n, "visible": True, "leaf": True, "expanded": None,
                         "checked": "on" if active["path"] == p else "off"} for p, n in fields]
            tree.evaluate.side_effect = snapshot
            tree.locator.side_effect = lambda selector: node_mocks[json.loads(selector[len('li[data-path='):-1])]
            add.click.side_effect = lambda: cart.append(dict(fields)[active["path"]])
            helpers.selected_field_names.side_effect = lambda p: cfg.identifier_fields + cart
            with patch("legacy_selector.choose_periods", side_effect=lambda p,c,r,n,*args: [n]):
                tasks = select_fields(page, cfg, {"Ticker"}, 2, None, helpers)
            self.assertEqual(tasks, ["Trading Symbol & Exchange", "Property Unique ID"])
            self.assertEqual(anchors, ["2", "4"])
            self.assertEqual(add.click.call_count, 2)

    def test_hidden_unfinished_leaf_reopens_ancestors(self):
        from legacy_selector import reveal_leaf
        node = MagicMock()
        expanders = node.locator.return_value.locator.return_value
        expanders.count.return_value = 1
        self.assertTrue(reveal_leaf(node))
        expanders.first.click.assert_called_once()

    def test_hidden_city_is_selected_before_next_category(self):
        from legacy_selector import select_fields
        with tempfile.TemporaryDirectory() as directory:
            cfg = MagicMock(all_time_points=False, ui_timeout_ms=1000)
            cfg.identifier_fields = []
            cfg.state_file = Path(directory) / "progress.json"
            page, root, tree, helpers = MagicMock(), MagicMock(), MagicMock(), MagicMock()
            page.locator.return_value = root
            root.locator.return_value.first = tree
            helpers.field_task_key = exporter.field_task_key
            helpers.task_description = exporter.task_description
            helpers.selected_field_names.return_value = ["City"]
            tree.evaluate.return_value = ["address|city"]
            hidden = [{"path": "address|city", "name": "City", "visible": False, "leaf": True},
                      {"path": "risk|seismic", "name": "Seismic Risk", "visible": True, "leaf": True}]
            shown = [dict(hidden[0], visible=True), hidden[1]]
            with patch("legacy_selector.wait_for_tree", side_effect=[hidden, shown]), \
                    patch("legacy_selector.reveal_leaf", return_value=True) as reveal:
                tasks = select_fields(page, cfg, set(), 1, None, helpers)
            self.assertEqual(tasks, [exporter.field_task_key("City", "address|city")])
            reveal.assert_called_once()

    def test_loading_placeholder_never_enters_field_traversal(self):
        from legacy_selector import wait_for_tree
        page, tree = MagicMock(), MagicMock()
        cfg = MagicMock(ui_timeout_ms=1000)
        pending = [{"path": "125061|125065|~Pending", "name": "Pending...", "visible": True,
                    "leaf": True, "expanded": None}]
        loaded = [{"path": "125061|125065|220585", "name": "Seismic Risk (PML)", "visible": True,
                   "leaf": True, "expanded": None}]
        tree.evaluate.side_effect = [pending, loaded, loaded]
        self.assertEqual(wait_for_tree(page, cfg, tree), loaded)
        self.assertEqual(page.wait_for_timeout.call_count, 2)

    def test_same_named_fields_keep_distinct_progress(self):
        from legacy_selector import legacy_completed, choose_periods
        helpers = MagicMock()
        helpers.field_task_key = exporter.field_task_key
        helpers.task_description = exporter.task_description
        first = exporter.field_task_key("City", "property|location|city")
        second = exporter.field_task_key("City", "owner|location|city")
        self.assertNotEqual(first, second)
        cache = {"City": {"node_path": "property|location|city"}}
        completed = {"City", first}
        self.assertIn("City", legacy_completed(completed, "City", "property|location|city", cache, helpers))
        filtered = legacy_completed(completed, "City", "owner|location|city", cache, helpers)
        self.assertNotIn("City", filtered)
        root = MagicMock()
        root.locator.return_value.locator.return_value.count.return_value = 0
        result = choose_periods(MagicMock(), MagicMock(), root, "City", filtered, 12, None, helpers, "owner|location|city")
        self.assertEqual(result, [second])

    def test_placeholder_cannot_be_exported_or_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.json"
            for key in ("Pending...", exporter.field_task_key("Loading...", "property|~Pending")):
                with self.assertRaises(ValueError):
                    exporter.mark_pending({}, path, 1, [key], True)
                self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
