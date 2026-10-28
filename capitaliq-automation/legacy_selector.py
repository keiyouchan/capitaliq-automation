"""Ignite-tree field selector used by Real Estate and other classic screeners."""

from __future__ import annotations

import json
import re
import time


def is_pending_node(entry):
    return bool(entry.get("busy") or "~pending" in (entry.get("path") or "").lower()
                or re.fullmatch(r"(?:pending|loading)[.\s…]*", entry.get("name") or "", re.I))


def read_tree(tree):
    return tree.evaluate("""el=>Array.from(el.querySelectorAll('li[data-role="node"]')).map(n=>({
        path:n.getAttribute('data-path'),name:n.querySelector(':scope > a')?.textContent.trim(),
        visible:n.getClientRects().length>0,leaf:n.classList.contains('ui-igtree-node-nochildren'),
        expanded:n.querySelector(':scope > [data-role="expander"]')?.getAttribute('data-exp'),
        checked:n.querySelector(':scope > [data-role="checkbox"]')?.getAttribute('data-chk'),
        busy:el.getAttribute('aria-busy')==='true' || n.getAttribute('aria-busy')==='true'
    }))""")


def wait_for_tree(page, cfg, tree):
    """Require loaded, stable children before consuming a category snapshot."""
    deadline = time.monotonic() + cfg.ui_timeout_ms / 1000
    previous = None
    while time.monotonic() < deadline:
        nodes = read_tree(tree)
        pending = [n for n in nodes if n["visible"] and is_pending_node(n)]
        signature = [(n["path"], n["name"], n["expanded"]) for n in nodes if n["visible"]]
        if nodes and not pending and signature == previous:
            return nodes
        previous = None if pending else signature
        page.wait_for_timeout(100)
    raise RuntimeError("Category children did not finish loading; stopping instead of skipping Pending nodes")


def legacy_completed(completed, field, path, cache, helpers):
    """Bind old name-only records only to their known original node path."""
    relevant = set(completed)
    old = cache.get(field, {})
    if old.get("node_path") != path:
        relevant.discard(field)
        for key in list(relevant):
            description = helpers.task_description(key)
            if description["field"] == field and description.get("field_id") is None:
                relevant.discard(key)
    return relevant


def reveal_leaf(node):
    """Reopen collapsed ancestors instead of skipping their loaded children."""
    expanders = node.locator('xpath=ancestor::li[@data-role="node"]').locator(
        ':scope > [data-role="expander"][data-exp="false"]')
    # Re-query after each click: Ignite may replace the subtree when expanding.
    if expanders.count():
        expanders.first.click()
        return True
    node.locator(':scope > a').scroll_into_view_if_needed()
    return False


def choose_periods(page, cfg, root, field, completed, limit, required, helpers, field_path=None):
    panel = root.locator(".subcategory-panel")
    # Classic parameter lists are distinct from the newer React year menu.
    # Support native multi-select time lists, and retain diagnostics for other
    # parameter renderers rather than marking their defaults as all periods.
    selects = panel.locator("select[multiple]")
    if selects.count() > 1:
        raise RuntimeError(f"Classic selector has multiple parameter axes for {field}; inspect its DOM diagnostic")
    if selects.count() == 1:
        select = selects.first
        options = select.locator("option").evaluate_all("""nodes => nodes.filter(o=>!o.disabled).map(o=>({label:o.text.trim(),value:o.value}))""")
        if not options or any(not re.search(r"\d{4}|year|qtr|quarter|YTD|LTM|\bQ[1-4]\b", o["label"], re.I) for o in options):
            raise RuntimeError(f"Classic parameter list for {field} is not a recognized time list")
        keys = {helpers.period_task_key(field, o["label"], field_path): o for o in options}
        # Legacy pending batches may still contain name+period keys without IDs.
        if required:
            for key in required:
                period = helpers.task_description(key)["period"]
                option = next((o for o in options if o["label"] == period), None)
                if option is not None:
                    keys[key] = option
        wanted = required or [key for key in keys if key not in completed and
                    helpers.period_task_key(field, helpers.task_description(key)["period"]) not in completed][:limit]
        if any(key not in keys for key in wanted) or len(wanted) > limit:
            raise RuntimeError(f"Cannot restore time-point tasks for {field}")
        if not wanted:
            return []
        select.select_option([keys[key]["value"] for key in wanted], force=True)
        selected = select.locator("option").evaluate_all("nodes=>nodes.filter(o=>o.selected).map(o=>o.value)")
        if set(selected) != {keys[key]["value"] for key in wanted}:
            raise RuntimeError(f"Could not apply classic time-point selection for {field}")
        catalog_path = cfg.state_file.with_name("time_points.json")
        catalog = json.loads(catalog_path.read_text()) if catalog_path.exists() else {}
        catalog[helpers.field_task_key(field, field_path)] = [o["label"] for o in options]
        helpers.save_state(catalog_path, catalog)
        return wanted
    if panel.locator(".ui-igtree [data-role='node'], [data-testid='periods'], select").count():
        raise RuntimeError(f"Classic parameters for {field} need calibration; stopping before exporting defaults")
    if required and any(helpers.task_description(key)["period"] is not None for key in required):
        raise RuntimeError(f"Pending time parameters disappeared for {field}")
    key = helpers.field_task_key(field, field_path)
    if required:
        return required
    return [key] if key not in completed and field not in completed else []


def select_fields(page, cfg, already, limit, required_tasks, helpers):
    root = page.locator(".modal-field-selector:visible")
    tree = root.locator(".non-shopping-cart-panel .ui-igtree").first
    cache_path = cfg.state_file.with_name("field_cache.json")
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    visited = set()
    checked = set()
    batch = []
    inventory_path = cfg.state_file.with_name("field_inventory.json")
    inventory = json.loads(inventory_path.read_text()) if inventory_path.exists() else {"fields": {}}
    while len(batch) < limit:
        nodes = wait_for_tree(page, cfg, tree)
        for entry in nodes:
            if entry["leaf"] and entry["name"] and not is_pending_node(entry):
                relevant = legacy_completed(already, entry["name"], entry["path"], cache, helpers)
                key = helpers.field_task_key(entry["name"], entry["path"])
                inventory["fields"][entry["path"]] = {
                    "field": entry["name"], "field_id": entry["path"],
                    "status": "identifier" if entry["name"] in cfg.identifier_fields else
                        "downloaded" if key in relevant or entry["name"] in relevant else "discovered"}
        inventory["enumeration_complete"] = False
        helpers.save_state(inventory_path, inventory)
        progressed = False
        for entry in nodes:
            path, name = entry["path"], entry["name"]
            if not name:
                continue
            node = tree.locator(f"li[data-path={json.dumps(path)}]")
            if not entry["leaf"]:
                if not entry["visible"]:
                    continue
                if entry["expanded"] == "false" and path not in visited:
                    node.locator(':scope > [data-role="expander"]').click()
                    deadline = time.monotonic() + cfg.ui_timeout_ms / 1000
                    while node.locator(':scope > [data-role="expander"]').get_attribute("data-exp") != "true":
                        if time.monotonic() >= deadline:
                            raise RuntimeError(f"Classic category failed to expand: {name}")
                        page.wait_for_timeout(100)
                    visited.add(path)
                    progressed = True
                    break
                continue
            required = [key for key in required_tasks or [] if helpers.task_description(key)["field"] == name
                        and helpers.task_description(key).get("field_id", path) == path] or None
            if name in cfg.identifier_fields or path in checked or (required_tasks is not None and not required):
                continue
            relevant = legacy_completed(already, name, path, cache, helpers)
            info = cache.get("ignite:" + path, cache.get(name, {}))
            static_key = helpers.field_task_key(name, path)
            if (static_key in relevant or name in relevant) and (not cfg.all_time_points or info.get("temporal") is False):
                continue
            if not entry["visible"]:
                if reveal_leaf(node):
                    progressed = True
                    break
                if not node.locator(':scope > a').is_visible():
                    raise RuntimeError(f"Unfinished classic field is hidden: {name} ({path}); refusing to skip it")
            # Ignite renders hidden checkbox spans. A normal click on the
            # visible anchor activates this field and replaces the previous
            # selection; attempting to click those spans stalls on field two.
            node.locator(":scope > a").click()
            deadline = time.monotonic() + cfg.ui_timeout_ms / 1000
            while True:
                selected_paths = tree.evaluate("""el=>Array.from(el.querySelectorAll('li.ui-igtree-node-nochildren'))
                    .filter(n=>n.querySelector(':scope > [data-role="checkbox"]')?.getAttribute('data-chk')==='on')
                    .map(n=>n.getAttribute('data-path'))""")
                if selected_paths == [path]:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Could not select exactly one classic field: {name}; checked paths={selected_paths!r}")
                page.wait_for_timeout(100)
            definition = root.locator(".field-definition .field-label")
            if definition.count() == 1:
                while definition.inner_text().strip() != name:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f"Field definition has not switched to {name}; stopping before reading stale parameters")
                    page.wait_for_timeout(100)
            try:
                tasks = choose_periods(page, cfg, root, name, relevant, limit - len(batch), required, helpers, path) if cfg.all_time_points else (required or [static_key])
            except RuntimeError:
                if not batch:
                    raise
                helpers.LOG.warning("Saving the %s ready columns before inspecting unsupported parameters for %s", len(batch), name)
                return batch
            cache["ignite:" + path] = {"field": name, "node_path": path, "selector": "ignite", "temporal": bool(tasks and tasks[0].startswith("period:"))}
            helpers.save_state(cache_path, cache)
            checked.add(path)
            if not tasks:
                progressed = True
                break
            root.locator("button:has(i.fa-caret-right)").click()
            expected = len(batch) + len(tasks)
            deadline = time.monotonic() + cfg.ui_timeout_ms / 1000
            while True:
                actual = [n for n in helpers.selected_field_names(page) if n not in cfg.identifier_fields]
                if len(actual) == expected:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Classic selector expected {expected} data columns, found {actual!r}")
                page.wait_for_timeout(100)
            batch.extend(tasks)
            inventory["fields"][path]["status"] = "selected_this_batch"
            helpers.save_state(inventory_path, inventory)
            helpers.LOG.info("Added %s; batch now has %s/%s data columns", name, len(batch), limit)
            progressed = True
            break
        if not progressed:
            break
    return batch
