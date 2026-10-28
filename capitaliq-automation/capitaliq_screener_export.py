#!/usr/bin/env python3
"""Capital IQ Screener batch exporter (interactive Playwright).

The UI is proprietary and changes over time. Keep selectors in config.json so the
automation can be calibrated without editing the workflow code.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Locator, Page, Playwright, sync_playwright

from dataset_workflow import (
    criteria_fingerprint, create_partitions, initialize_dataset, prepare_dataset_criteria, screen_identity,
)
from legacy_selector import select_fields as select_legacy_fields


LOG = logging.getLogger("capitaliq-export")

REQUIRED_SELECTORS = {
    "add_selected_field",
    "apply_columns",
    "category_leaf_css",
    "category_tree",
    "collapsed_category_css",
    "display_columns",
    "download_ready",
    "entity_type_filter",
    "export",
    "export_mode",
    "export_values_option",
    "filter_dismiss",
    "company_types_filter",
    "operating_status_filter",
    "remove_selected_field",
    "reporting_level_filter",
    "result_count",
    "run_screen",
    "select_all",
    "selected_fields",
}


@dataclass
class Settings:
    screener_url: str
    output_dir: Path
    profile_dir: Path
    state_file: Path
    all_time_points: bool
    target_fields_per_file: int
    cell_limit: int
    id_columns: int
    row_count: int | None
    expected_row_count: int | None
    identifier_fields: list[str]
    initial_selected_fields: list[str]
    download_timeout_ms: int
    ui_timeout_ms: int
    headless: bool
    slow_mo_ms: int
    selectors: dict[str, list[str]]
    dataset_name: str | None = None
    criteria_file: Path | None = None

    @classmethod
    def load(cls, path: Path) -> "Settings":
        if not path.exists() and path.name == "config.json":
            relocated = path.parent / "json" / path.name
            if relocated.exists():
                LOG.info("Using relocated configuration: %s", relocated)
                path = relocated
        raw = json.loads(path.read_text(encoding="utf-8"))
        selectors = raw.get("selectors", {})
        required = REQUIRED_SELECTORS
        if raw.get("dataset_name"):
            required = required - {"company_types_filter", "entity_type_filter", "operating_status_filter", "reporting_level_filter", "select_all", "filter_dismiss"}
        missing = sorted(required - selectors.keys())
        if missing:
            raise ValueError("Missing selectors in config: " + ", ".join(missing))
        invalid = sorted(key for key, value in selectors.items() if not isinstance(value, list) or not value)
        if invalid:
            raise ValueError("Selectors must be non-empty lists: " + ", ".join(invalid))
        base = path.parent.resolve()
        def p(value: str) -> Path:
            value = value.replace("{CONFIG_DIR}", str(base))
            return Path(value).expanduser().resolve()
        return cls(
            screener_url=raw["screener_url"],
            output_dir=p(raw["output_dir"]), profile_dir=p(raw["profile_dir"]),
            state_file=p(raw["state_file"]),
            all_time_points=bool(raw.get("all_time_points", False)),
            target_fields_per_file=int(raw.get("target_fields_per_file", 10)),
            cell_limit=int(raw.get("cell_limit", 500_000)),
            id_columns=int(raw.get("id_columns", 1)),
            row_count=raw.get("row_count"),
            expected_row_count=raw.get("expected_row_count"),
            identifier_fields=list(raw.get("identifier_fields", ["Company Name"])),
            initial_selected_fields=list(raw.get("initial_selected_fields", [])),
            download_timeout_ms=int(raw.get("download_timeout_ms", 300_000)),
            ui_timeout_ms=int(raw.get("ui_timeout_ms", 30_000)),
            headless=bool(raw.get("headless", False)), slow_mo_ms=int(raw.get("slow_mo_ms", 100)),
            selectors=selectors,
            dataset_name=raw.get("dataset_name"),
            criteria_file=p(raw["criteria_file"]) if raw.get("criteria_file") else None,
        )


def first_visible(page: Page, candidates: list[str], *, timeout_ms: int = 8_000) -> Locator:
    """Return the first visible candidate; selectors can be CSS or text=/role= shorthands."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        for spec in candidates:
            if spec.startswith("role="):
                role, name = spec[5:].split("|", 1)
                loc = page.get_by_role(role, name=re.compile(name, re.I))
            elif spec.startswith("text="):
                loc = page.get_by_text(re.compile(spec[5:], re.I), exact=False)
            else:
                loc = page.locator(spec)
            try:
                if loc.count() and loc.first.is_visible():
                    return loc.first
            except Exception:
                pass
        page.wait_for_timeout(250)
    raise RuntimeError("No visible locator matched: " + " | ".join(candidates))


def click(page: Page, cfg: Settings, name: str, timeout_ms: int | None = None) -> None:
    first_visible(page, cfg.selectors[name], timeout_ms=timeout_ms or cfg.ui_timeout_ms).click()


def load_state(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"completed_batches": 0, "downloaded_fields": [], "pending_batch": None}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def detect_row_count(page: Page, cfg: Settings) -> int:
    if cfg.row_count:
        return int(cfg.row_count)
    deadline = time.monotonic() + cfg.ui_timeout_ms / 1000
    observed: list[str] = []
    while time.monotonic() < deadline:
        limit_error = page.get_by_text(re.compile(r"Data downloads are limited to", re.I))
        if any(limit_error.nth(i).is_visible() for i in range(limit_error.count())):
            raise RuntimeError("Screen exceeds the website export limits (250,000 rows / 200 columns / 10,000,000 cells); partition Criteria further")
        for spec in cfg.selectors["result_count"]:
            numeric_matches: list[int] = []
            loc = (
                page.get_by_text(re.compile(spec[5:], re.I), exact=False)
                if spec.startswith("text=")
                else page.locator(spec)
            )
            for index in range(loc.count()):
                candidate = loc.nth(index)
                try:
                    if not candidate.is_visible():
                        continue
                    text = re.sub(r"\s+", " ", candidate.inner_text()).strip()
                    if text and text not in observed:
                        observed.append(text)
                    numbers = [int(x.replace(",", "")) for x in re.findall(r"\d[\d,]*", text)]
                    numeric_matches.extend(numbers)
                except Exception:
                    continue
            # Selector order expresses specificity. Return before trying a
            # broader fallback so numeric IDs in the result grid are ignored.
            if numeric_matches:
                return max(numeric_matches)
        page.wait_for_timeout(250)
    raise RuntimeError(
        f"Could not find a numeric result count; matched text was {observed!r}. "
        "Update selectors.result_count or set row_count in config.json."
    )


def wait_for_screener(page: Page, cfg: Settings) -> None:
    """Allow interactive login, while also supporting runners without stdin."""
    if sys.stdin.isatty():
        print("Log in if needed and navigate to the prepared Screener. Then press Enter here.")
        input()
        timeout_ms = cfg.ui_timeout_ms
    else:
        timeout_ms = cfg.download_timeout_ms
        LOG.info(
            "No interactive stdin; waiting up to %.0f seconds for the Screener results.",
            timeout_ms / 1000,
        )
    first_visible(page, cfg.selectors["run_screen"] if cfg.dataset_name else cfg.selectors["display_columns"], timeout_ms=timeout_ms)


def close_stale_field_selector(page: Page, cfg: Settings) -> None:
    """Close a Field Selector dialog restored from a previously interrupted run."""
    heading = page.get_by_text("Field Selector", exact=True)
    try:
        if not heading.count() or not heading.first.is_visible():
            return
        LOG.info("Closing Field Selector left open by a previous run.")
        legacy = page.locator(".modal-field-selector:visible")
        if legacy.count():
            legacy.get_by_role("button", name=re.compile(r"^Cancel$", re.I)).click()
        else:
            page.get_by_role("button", name="Close", exact=True).click()
        heading.first.wait_for(state="hidden", timeout=cfg.ui_timeout_ms)
    except Exception as exc:
        raise RuntimeError("Could not close the stale Field Selector dialog") from exc


def dismiss_filter_menu(page: Page, cfg: Settings) -> None:
    click(page, cfg, "filter_dismiss")
    page.wait_for_timeout(300)


def select_all_filter(page: Page, cfg: Settings, selector_name: str) -> None:
    click(page, cfg, selector_name)
    click(page, cfg, "select_all")
    dismiss_filter_menu(page, cfg)


def prepare_filters(page: Page, cfg: Settings) -> None:
    """Reproduce the stable filter setup captured in recorded.py."""
    LOG.info("Selecting all company types, operating statuses, and entity types.")
    select_all_filter(page, cfg, "company_types_filter")
    # The recorded flow opens and closes Reporting Level to retain All Companies.
    click(page, cfg, "reporting_level_filter")
    dismiss_filter_menu(page, cfg)
    select_all_filter(page, cfg, "operating_status_filter")
    select_all_filter(page, cfg, "entity_type_filter")


def run_screen(page: Page, cfg: Settings) -> None:
    """Execute the prepared screen and wait until export controls are ready."""
    LOG.info("Running the prepared screen.")
    click(page, cfg, "run_screen")
    if not cfg.dataset_name:
        first_visible(page, cfg.selectors["export"], timeout_ms=cfg.download_timeout_ms)


def safe_batch_size(rows: int, cfg: Settings) -> int:
    if rows <= 0:
        raise RuntimeError(f"Result count must be positive, got {rows}")
    if rows > 250_000:
        raise RuntimeError("Matches exceed 250,000 rows; split Criteria into smaller partitions")
    # Every export contains identifier columns as well as selected data fields.
    available = math.floor(cfg.cell_limit / rows) - cfg.id_columns
    if available < 1:
        raise RuntimeError(
            f"{rows:,} rows × ({cfg.id_columns} ID columns + 1 field) exceeds "
            f"the {cfg.cell_limit:,}-cell limit. Narrow the screen first."
        )
    return min(cfg.target_fields_per_file, available, 200 - cfg.id_columns)


def open_columns(page: Page, cfg: Settings) -> None:
    click(page, cfg, "display_columns")
    first_visible(page, cfg.selectors["category_tree"] + [".modal-field-selector .non-shopping-cart-panel .ui-igtree"], timeout_ms=cfg.ui_timeout_ms)


def visible_leaf_fields(page: Page, cfg: Settings) -> list[Locator]:
    """Return selectable leaf fields in current visual top-to-bottom order."""
    tree = first_visible(page, cfg.selectors["category_tree"])
    leaves = tree.locator(", ".join(cfg.selectors["category_leaf_css"]))
    result: list[Locator] = []
    for i in range(leaves.count()):
        item = leaves.nth(i)
        try:
            if item.is_visible() and item.inner_text().strip():
                result.append(item)
        except Exception:
            continue
    return result


def expand_next_category(page: Page, cfg: Settings) -> bool:
    tree = first_visible(page, cfg.selectors["category_tree"])
    nodes = tree.locator("li.jstree-closed")
    for i in range(nodes.count()):
        node = nodes.nth(i)
        if node.is_visible():
            expand_category(node, cfg)
            return True
    return False


def expand_category(node: Locator, cfg: Settings) -> None:
    """Expand one stable node, clicking only its direct-child toggle."""
    node_id = node.get_attribute("id")
    anchor = node.locator(":scope > a.jstree-anchor")
    LOG.info("Expanding category: %s (id=%s)", anchor.inner_text(), node_id)
    node.scroll_into_view_if_needed()
    node.locator(":scope > i.jstree-ocl").click()
    deadline = time.monotonic() + cfg.ui_timeout_ms / 1000
    while time.monotonic() < deadline:
        classes = (node.get_attribute("class") or "").split()
        if "jstree-open" in classes or "jstree-leaf" in classes:
            return
        # Do not click again: delayed child loading must never toggle it closed.
        node.page.wait_for_timeout(100)
    raise RuntimeError(f"Category failed to expand: {node_id}")


def field_name(item: Locator) -> str:
    return re.sub(r"\s+", " ", item.inner_text()).strip()


def selected_fields_panel(page: Page) -> Locator:
    panel = page.locator(".SelectedFieldsColumn")
    if panel.count() == 0:
        panel = page.locator(".modal-field-selector:visible .shopping-cart-center")
    if panel.count() != 1:
        raise RuntimeError("Expected exactly one .SelectedFieldsColumn panel")
    return panel


def selected_field_names(page: Page) -> list[str]:
    cells = selected_fields_panel(page).locator(".ag-cell-value")
    names: list[str] = []
    for index in range(cells.count()):
        cell = cells.nth(index)
        try:
            if cell.is_visible():
                name = re.sub(r"\s+", " ", cell.inner_text()).strip()
                if name:
                    names.append(name)
        except Exception:
            continue
    defaults = selected_fields_panel(page).locator(".default-fields > div")
    for index in range(defaults.count()):
        if defaults.nth(index).is_visible():
            names.append(defaults.nth(index).inner_text().strip())
    return names


def field_is_already_selected(page: Page, name: str) -> bool:
    return name in selected_field_names(page) or name in {"Company Name", "SNL Institution Key"}


def field_task_key(field: str, field_id: str) -> str:
    return "field:" + json.dumps([field, field_id], ensure_ascii=False)


def period_task_key(field: str, period: str, field_id: str | None = None) -> str:
    return "period:" + json.dumps([field, period] + ([field_id] if field_id is not None else []), ensure_ascii=False)


def task_description(key: str) -> dict[str, Any]:
    if key.startswith("period:"):
        parts = json.loads(key[len("period:"):])
        return {"field": parts[0], "period": parts[1], **({"field_id": parts[2]} if len(parts) > 2 else {})}
    if key.startswith("field:"):
        field, field_id = json.loads(key[len("field:"):])
        return {"field": field, "period": None, "field_id": field_id}
    return {"field": key, "period": None}


def option_checked(option: Locator) -> bool:
    inputs = option.locator("input[type='checkbox']")
    if inputs.count() == 1:
        return inputs.is_checked()
    for attr in ("aria-selected", "aria-checked"):
        value = option.get_attribute(attr)
        if value in {"true", "false"}:
            return value == "true"
    raise RuntimeError("Time option exposes no checkbox or selection state; inspect the DOM diagnostic")


def set_period_option(option: Locator, checked: bool) -> None:
    """Use the visible checkbox label; aria-selected indicates focus, not a tick."""
    if option_checked(option) == checked:
        return
    label = option.locator("label[data-option-label]")
    if label.count() == 1:
        label.click()
    else:
        option.click()
    if option_checked(option) != checked:
        raise RuntimeError("Time checkbox did not reach the requested state")


def choose_next_period(page: Page, cfg: Settings, field: str, completed: set[str], required: list[str] | None = None, limit: int = 1) -> list[str]:
    """Choose a batch of unfinished time points for one field."""
    parameters = page.locator(".SelectedFieldsColumn").locator(
        "xpath=preceding-sibling::*[1]"
    ).locator("form")
    if parameters.count() != 1:
        raise RuntimeError(f"Could not uniquely locate Parameters form for {field}")
    # Period Type (Year/Quarter) and Period are different controls. Both may
    # contain 'Year' in their accessible name; prefer the actual Period selector.
    period_control = parameters.locator("[data-testid='periods'] button")
    menus = period_control if period_control.count() else parameters.get_by_role(
        "button", name=re.compile(r"(?:Proj|Year|Period).*Open menu", re.I)
    )
    visible_menus = [menus.nth(i) for i in range(menus.count()) if menus.nth(i).is_visible()]
    if not visible_menus:
        if required is not None and any(task_description(key)["period"] is not None for key in required):
            raise RuntimeError(f"Pending time parameter menu disappeared for {field}")
        # Other parameter menu names require explicit calibration, rather than
        # silently treating a temporal field as an ordinary default-period field.
        other = parameters.get_by_role("button", name=re.compile(r"Open menu", re.I))
        if any(other.nth(i).is_visible() for i in range(other.count())):
            raise RuntimeError(f"Unrecognized parameter menu for {field}; inspect the DOM diagnostic")
        return [field] if field not in completed else []
    if len(visible_menus) != 1:
        raise RuntimeError(f"Multiple parameter axes for {field}; automatic year-only selection is unsafe")
    menu = visible_menus[0]
    menu.click()
    options = page.get_by_role("option")
    options.first.wait_for(state="visible", timeout=cfg.ui_timeout_ms)
    years: dict[str, Locator] = {}
    selected_options: list[Locator] = []
    for i in range(options.count()):
        option = options.nth(i)
        if not option.is_visible():
            continue
        label = re.sub(r"\s+", " ", option.inner_text()).strip()
        if option.get_attribute("data-select-option-disabled") == "true":
            continue
        # Actual Period menus contain explicit years (2025 Y), quarters and
        # relative periods (MstRctYear, YTD, LTM). Their single checkbox is the
        # stable signal that an entry is a selectable time point.
        if option.locator("input[type='checkbox']").count() == 1 and label.casefold() not in {"proj", "select all", "all", "deselect all"}:
            if label in years:
                raise RuntimeError(f"Duplicate time option {label} for {field}")
            years[label] = option
            if option_checked(option):
                selected_options.append(option)
        elif label.casefold() not in {"proj", "select all", "all", "deselect all"}:
            raise RuntimeError(f"Unsupported time option {label!r} for {field}; refusing a partial export")
    if not years:
        raise RuntimeError(f"No selectable time points found for {field}; menu DOM must be calibrated")
    # A virtualized menu cannot be fully enumerated by inspecting its current DOM.
    # Detect it explicitly; never claim that only the rendered years are all years.
    virtualized = options.first.evaluate("""el => {
        for (let n = el; n; n = n.parentElement) {
            if (n.getAttribute('role') === 'listbox') {
                const total = Number(n.getAttribute('aria-setsize') || 0);
                const options = n.querySelectorAll('[role="option"]');
                return total > options.length || Array.from(options).some(o =>
                    Number(o.getAttribute('aria-setsize') || 0) > options.length) ||
                    !!n.querySelector('[class*="virtual"], [class*="Virtual"]');
            }
        }
        return false;
    }""")
    if virtualized:
        raise RuntimeError(f"Virtualized time menu for {field}; full scrolling enumeration is required")
    catalog_path = cfg.state_file.with_name("time_points.json")
    catalog = json.loads(catalog_path.read_text(encoding="utf-8")) if catalog_path.exists() else {}
    catalog[field] = sorted(years, reverse=True)
    save_state(catalog_path, catalog)
    remaining = [year for year in sorted(years, reverse=True) if period_task_key(field, year) not in completed]
    if required is not None:
        wanted = [task_description(key)["period"] for key in required]
        if any(period not in years for period in wanted):
            raise RuntimeError(f"Pending time points {wanted!r} are unavailable for {field}")
        remaining = wanted
    if not remaining:
        menu.click()
        return []
    targets = remaining[:limit]
    if required is not None and len(targets) != len(required):
        raise RuntimeError("Pending time-point batch exceeds the current export column capacity")
    # Change only checkboxes whose state differs from the desired batch.
    for label, option in years.items():
        set_period_option(option, label in targets)
    for label, option in years.items():
        if option_checked(option) != (label in targets):
            raise RuntimeError(f"Could not configure {field}; time point {label} has unexpected state")
    menu.click()
    LOG.info("Time field %s: selecting %s time points: %s", field, len(targets), ", ".join(targets))
    return [period_task_key(field, period) for period in targets]


def select_next_fields(page: Page, cfg: Settings, already: set[str], limit: int, required_tasks: list[str] | None = None) -> list[str]:
    if page.locator(".modal-field-selector:visible").count():
        return select_legacy_fields(page, cfg, already, limit, required_tasks, sys.modules[__name__])
    cache_path = cfg.state_file.with_name("field_cache.json")
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    catalog_path = cfg.state_file.with_name("time_points.json")
    catalog = json.loads(catalog_path.read_text(encoding="utf-8")) if catalog_path.exists() else {}
    chosen: list[str] = []
    seen_this_pass: set[str] = set()
    skipped_completed = 0
    expanded: set[str] = set()
    selected = selected_field_names(page)
    excluded = set(cfg.identifier_fields) | set(selected)
    if not cfg.all_time_points:
        excluded |= already
    # Revisit a partially downloaded temporal field directly. Its ancestor IDs
    # are stable even when the modal resets the tree to a collapsed state.
    preferred = next((name for name, entry in cache.items()
                      if cfg.all_time_points and entry.get("temporal") and entry.get("path")
                      and any(period_task_key(name, period) not in already
                              for period in catalog.get(name, []))), None)
    if required_tasks:
        preferred = task_description(required_tasks[0])["field"]
    if preferred and cache.get(preferred, {}).get("path"):
        tree = first_visible(page, cfg.selectors["category_tree"])
        for ancestor_id in cache[preferred]["path"]:
            ancestor = tree.locator(f"li[id={json.dumps(ancestor_id)}]")
            if not ancestor.count():
                preferred = None
                break
            if "jstree-closed" in (ancestor.get_attribute("class") or "").split():
                expand_category(ancestor, cfg)
                expanded.add(ancestor_id)
        if preferred:
            LOG.info("Returning directly to cached field: %s", preferred)
    while len(chosen) < limit:
        tree = first_visible(page, cfg.selectors["category_tree"])
        # Read the tree once per action, rather than making several driver
        # round trips for every already-downloaded field.
        nodes = tree.evaluate("""el => Array.from(el.querySelectorAll('li.jstree-node')).map(n => ({
            id: n.id, classes: n.className.split(/\\s+/),
            visible: n.getClientRects().length > 0,
            name: n.querySelector(':scope > a.jstree-anchor')?.textContent.trim() || ''
        }))""")
        if preferred:
            nodes.sort(key=lambda entry: entry["id"] != cache.get(preferred, {}).get("node_id"))
            preferred = None
        progressed = False
        for candidate in nodes:
            if not candidate["visible"]:
                continue
            node_id = candidate["id"]
            if not node_id:
                raise RuntimeError("Category tree node has no stable ID")
            # Pin the locator by ID before any click changes the tree's indices.
            node = tree.locator(f"li[id={json.dumps(node_id)}]")
            classes = candidate["classes"]
            if "jstree-closed" in classes:
                if node_id in expanded:
                    # Opening a sibling can automatically collapse a category
                    # whose available fields we already exhausted this pass.
                    # Reopening it would alternate forever between siblings.
                    continue
                if chosen:
                    # Commit the partial batch before changing categories;
                    # category changes may clear the tree's multi-selection.
                    LOG.info("Category boundary reached; exporting %s fields in this batch.", len(chosen))
                    break
                expand_category(node, cfg)
                expanded.add(node_id)
                progressed = True
                break
            if "jstree-leaf" not in classes:
                continue
            item = node.locator(":scope > a.jstree-anchor")
            name = re.sub(r"\s+", " ", candidate["name"]).strip()
            required = [key for key in required_tasks or [] if task_description(key)["field"] == name] or None
            if (
                not name
                or name in excluded
                or name in seen_this_pass
                or (required_tasks is not None and required is None)
            ):
                continue
            if required_tasks is None and cfg.all_time_points and name in cache:
                if cache[name].get("temporal"):
                    periods = catalog.get(name, [])
                    if periods and all(period_task_key(name, period) in already for period in periods):
                        continue
                elif name in already:
                    continue
            item.scroll_into_view_if_needed()
            if chosen:
                item.click(modifiers=["ControlOrMeta"])
            else:
                item.click()
            tasks = choose_next_period(page, cfg, name, already, required, limit=limit) if cfg.all_time_points else [name]
            path = node.evaluate("""el => {
                const ids = [];
                for (let n = el.parentElement; n; n = n.parentElement) {
                    if (n.matches('li.jstree-node')) ids.unshift(n.id);
                }
                return ids;
            }""")
            cache[name] = {"node_id": node_id, "path": path,
                           "temporal": bool(tasks and tasks[0].startswith("period:"))}
            # The catalog also records temporal fields whose periods are all done.
            if catalog_path.exists():
                catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
                if name in catalog:
                    cache[name]["temporal"] = True
            save_state(cache_path, cache)
            if not tasks:
                seen_this_pass.add(name)
                skipped_completed += 1
                if skipped_completed == 1 or skipped_completed % 10 == 0:
                    LOG.info("Skipped %s completed fields; latest checked: %s", skipped_completed, name)
                progressed = True
                break
            chosen.extend(tasks)
            seen_this_pass.add(name)
            progressed = True
            break
        if chosen and cfg.all_time_points:
            break
        if not progressed:
            break
    if chosen:
        LOG.info("Adding selected fields: %s", ", ".join(chosen))
        click(page, cfg, "add_selected_field")
        if cfg.all_time_points:
            actual = [name for name in selected_field_names(page) if name not in cfg.identifier_fields]
            if len(actual) != len(chosen):
                raise RuntimeError(f"Expected {len(chosen)} data columns for the time-point batch, found {actual!r}")
    return chosen


def wait_for_screen_calculation(page: Page, cfg: Settings, *, require_export: bool = True) -> None:
    """Wait until Capital IQ finishes recalculating the current screen."""
    LOG.info("Waiting for screen calculation to finish (up to %ss).", cfg.download_timeout_ms / 1000)
    page.locator(".modalBlockout:visible").wait_for(
        state="hidden", timeout=cfg.download_timeout_ms
    )
    if not require_export:
        return
    export = first_visible(page, cfg.selectors["export"], timeout_ms=cfg.download_timeout_ms)
    # Trial click waits for enabled state and absence of pointer interception
    # without initiating an export. It also covers overlays appearing late.
    export.click(trial=True, timeout=cfg.download_timeout_ms)


def apply_columns(page: Page, cfg: Settings) -> None:
    legacy = page.locator(".modal-field-selector:visible")
    if legacy.count():
        legacy.locator("button.fs-apply").click()
        legacy.wait_for(state="hidden", timeout=cfg.ui_timeout_ms)
        wait_for_screen_calculation(page, cfg)
        return
    tree = first_visible(page, cfg.selectors["category_tree"])
    click(page, cfg, "apply_columns")
    tree.wait_for(state="hidden", timeout=cfg.ui_timeout_ms)
    # Capital IQ is a single-page app; network idle or DOMContentLoaded does not
    # reliably indicate that the result grid has accepted the new columns.
    wait_for_screen_calculation(page, cfg)


def set_export_values(page: Page, cfg: Settings) -> None:
    wait_for_screen_calculation(page, cfg)
    mode = first_visible(page, cfg.selectors["export_mode"], timeout_ms=cfg.ui_timeout_ms)
    current = mode.get_attribute("title") or mode.inner_text()
    if re.search(r"Results As Values", current, re.I):
        LOG.info("Export mode is already Results As Values.")
        return
    click(page, cfg, "export_mode")
    option = first_visible(page, cfg.selectors["export_values_option"], timeout_ms=5_000)
    option.click()
    wait_for_screen_calculation(page, cfg)


def export_and_download(page: Page, cfg: Settings, number: int) -> Path:
    set_export_values(page, cfg)
    click(page, cfg, "export")
    ready = first_visible(page, cfg.selectors["download_ready"], timeout_ms=cfg.download_timeout_ms)
    with page.expect_download(timeout=cfg.download_timeout_ms) as info:
        ready.click()
    download = info.value
    suffix = Path(download.suggested_filename).suffix or ".xlsx"
    destination = cfg.output_dir / f"capitaliq_{number:04d}{suffix}"
    failure = download.failure()
    if failure:
        raise RuntimeError("Download failed: " + failure)
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite existing export: {destination}")
    temporary = destination.with_suffix(destination.suffix + ".part")
    download.save_as(temporary)
    if not temporary.exists() or temporary.stat().st_size == 0:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Downloaded file is empty: {destination.name}")
    temporary.replace(destination)
    return destination


def remove_selected_fields(page: Page, cfg: Settings, names: list[str]) -> None:
    """Remove only fields exported in the just-completed batch."""
    for name in names:
        panel = selected_fields_panel(page)
        matches = panel.locator(".ag-cell-value").filter(has_text=re.compile("^" + re.escape(name) + "$"))
        before = matches.count()
        if not before:
            raise RuntimeError(f"Downloaded field no longer present in Selected Fields: {name}")
        target = matches.first
        row = target.locator("xpath=ancestor::*[@role='row'][1]")
        row.locator('[data-testid="removebutton"], .removeButton').click(timeout=cfg.ui_timeout_ms)
        deadline = time.monotonic() + cfg.ui_timeout_ms / 1000
        while time.monotonic() < deadline:
            if selected_fields_panel(page).locator(".ag-cell-value").filter(
                has_text=re.compile("^" + re.escape(name) + "$")
            ).count() < before:
                break
            page.wait_for_timeout(100)
        else:
            raise RuntimeError(f"Selected field was not removed: {name}")


def remove_all_non_identifier_fields(page: Page, cfg: Settings) -> None:
    identifiers = set(cfg.identifier_fields)
    while True:
        names = selected_field_names(page)
        removable = next((name for name in names if name not in identifiers), None)
        if removable is None:
            return
        remove_selected_fields(page, cfg, [removable])


def normalize_selected_fields(page: Page, cfg: Settings) -> None:
    LOG.info("Removing initial fields; retaining only: %s", ", ".join(cfg.identifier_fields))
    open_columns(page, cfg)
    if cfg.dataset_name:
        locked = selected_fields_panel(page).evaluate("""panel => {
            const names = Array.from(panel.querySelectorAll('.default-fields > div')).map(n=>n.textContent.trim()).filter(Boolean);
            for (const row of panel.querySelectorAll('[role="row"]')) {
                const label = row.querySelector('[col-id="label-0"] .ag-cell-value');
                if (label && !row.querySelector('[data-testid="removebutton"]')) {
                    const text = label.textContent.trim();
                    if (text) names.push(text);
                }
            }
            return Array.from(new Set(names));
        }""")
        cfg.identifier_fields = list(dict.fromkeys(locked + cfg.identifier_fields))
        if not cfg.identifier_fields:
            raise RuntimeError("No mandatory identifier columns detected; set identifier_fields explicitly for this dataset")
        cfg.id_columns = len(cfg.identifier_fields)
        LOG.info("Dataset identifiers: %s", cfg.identifier_fields)
    remove_all_non_identifier_fields(page, cfg)
    apply_columns(page, cfg)


def mark_pending(state: dict[str, Any], path: Path, number: int, fields: list[str], explicit_periods: bool = False) -> None:
    for key in fields:
        description = task_description(key)
        if re.fullmatch(r"(?:pending|loading)[.\s…]*", description["field"], re.I) or "~pending" in description.get("field_id", "").lower():
            raise ValueError("Refusing to export a loading placeholder as a field")
    state["pending_batch"] = {"number": number, "fields": fields, "file": None,
                              "explicit_periods": explicit_periods}
    save_state(path, state)


def mark_downloaded(state: dict[str, Any], path: Path, saved: Path) -> None:
    state["pending_batch"]["file"] = str(saved)
    save_state(path, state)
    write_downloaded_fields_manifest(state, path)


def write_downloaded_fields_manifest(state: dict[str, Any], state_path: Path) -> None:
    """Write a separate, atomic inventory of confirmed downloads."""
    batches = list(state.get("download_history", []))
    pending = state.get("pending_batch")
    if pending and pending.get("file"):
        if not any(batch["number"] == pending["number"] for batch in batches):
            batches.append(dict(pending))
    confirmed = list(state.get("downloaded_fields", []))
    if pending and pending.get("file"):
        confirmed.extend(pending["fields"])
    save_state(state_path.with_name("downloaded_fields.json"), {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "fields": list(dict.fromkeys(confirmed)),
        "field_time_points": [task_description(key) for key in dict.fromkeys(confirmed)],
        "batches": batches,
        "suspect_loading_placeholders": [key for key in confirmed if re.fullmatch(
            r"(?:pending|loading)[.\s…]*", task_description(key)["field"], re.I)],
        "note": "Historical field names do not prove that every time period was exported."
    })


def complete_pending(state: dict[str, Any], path: Path) -> None:
    pending = state["pending_batch"]
    history = state.setdefault("download_history", [])
    if not any(batch["number"] == pending["number"] for batch in history):
        history.append(dict(pending))
    state["completed_batches"] = int(pending["number"])
    state["downloaded_fields"].extend(pending["fields"])
    state["last_file"] = pending["file"]
    state["pending_batch"] = None
    save_state(path, state)
    write_downloaded_fields_manifest(state, path)


def clean_selected_batch(page: Page, cfg: Settings, fields: list[str]) -> None:
    open_columns(page, cfg)
    remove_all_non_identifier_fields(page, cfg)
    apply_columns(page, cfg)


def resume_pending_download(page: Page, cfg: Settings, state: dict[str, Any]) -> None:
    pending = state["pending_batch"]
    number = int(pending["number"])
    existing = [
        path for path in cfg.output_dir.glob(f"capitaliq_{number:04d}.*")
        if not path.name.endswith(".part") and path.is_file() and path.stat().st_size > 0
    ]
    if len(existing) > 1:
        raise RuntimeError(f"Multiple files found for pending batch {number}: {existing}")
    if existing:
        saved = existing[0]
        LOG.info("Recovered existing file for pending batch %s: %s", number, saved)
    else:
        LOG.info("Retrying download for pending batch %s", number)
        tasks = pending["fields"]
        if cfg.all_time_points and not pending.get("explicit_periods"):
            raise RuntimeError(
                "Pending batch was created before explicit time-point mode. "
                "Complete it with all_time_points=false before enabling the new mode."
            )
        open_columns(page, cfg)
        restored = select_next_fields(page, cfg, set(), len(tasks), required_tasks=tasks)
        if restored != tasks:
            raise RuntimeError(f"Could not restore pending columns exactly: {tasks!r}; got {restored!r}")
        apply_columns(page, cfg)
        saved = export_and_download(page, cfg, number)
    mark_downloaded(state, cfg.state_file, saved)


def run(playwright: Playwright, cfg: Settings, *, reset: bool, dry_run: bool) -> None:
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    cfg.profile_dir.mkdir(parents=True, exist_ok=True)
    if reset and dry_run:
        raise ValueError("--reset cannot be used with dry-run")
    if reset and cfg.state_file.exists():
        cfg.state_file.unlink()
    state = load_state(cfg.state_file)
    state.setdefault("pending_batch", None)
    pending = state.get("pending_batch")
    if not dry_run and cfg.all_time_points and pending and not pending.get("explicit_periods") and not pending.get("file"):
        files = [p for p in cfg.output_dir.glob(f"capitaliq_{int(pending['number']):04d}.*")
                 if p.is_file() and not p.name.endswith(".part") and p.stat().st_size > 0]
        if files:
            raise RuntimeError("An unconfirmed legacy export exists; reconcile it before switching to all_time_points mode")
        # Keep the interrupted task in history, without claiming it downloaded.
        state.setdefault("superseded_default_batches", []).append(dict(pending))
        state["pending_batch"] = None
        save_state(cfg.state_file, state)
        LOG.info("Archived unfinished default-period batch; restarting it with explicit time-point tasks.")
    if not dry_run:
        write_downloaded_fields_manifest(state, cfg.state_file)
    context: BrowserContext = playwright.chromium.launch_persistent_context(
        str(cfg.profile_dir), headless=cfg.headless, slow_mo=cfg.slow_mo_ms,
        accept_downloads=True, viewport={"width": 1600, "height": 1000},
    )
    page = context.pages[0] if context.pages else context.new_page()
    page.set_default_timeout(cfg.ui_timeout_ms)
    try:
        page.goto(cfg.screener_url, wait_until="domcontentloaded")
        close_stale_field_selector(page, cfg)
        wait_for_screener(page, cfg)
        # The SPA may restore modal state after DOMContentLoaded.
        close_stale_field_selector(page, cfg)
        criteria_document = None
        if cfg.dataset_name:
            if not cfg.criteria_file:
                raise ValueError("Dataset requires criteria_file")
            if screen_identity(page.url) != screen_identity(cfg.screener_url):
                raise RuntimeError("Browser is on a different Screener than the selected dataset")
            criteria_document = prepare_dataset_criteria(page, cfg)
            if criteria_document is None:
                return
            fingerprint = criteria_fingerprint(criteria_document)
            if state.get("criteria_fingerprint") not in {None, fingerprint}:
                raise RuntimeError("Criteria changed since this checkpoint; create a new dataset to avoid mixing exports")
            state["criteria_fingerprint"] = fingerprint
        else:
            prepare_filters(page, cfg)
        run_screen(page, cfg)
        wait_for_screen_calculation(page, cfg, require_export=False)
        rows = detect_row_count(page, cfg)
        if cfg.expected_row_count is not None and rows != int(cfg.expected_row_count):
            raise RuntimeError(
                f"Expected {int(cfg.expected_row_count):,} matches after applying filters, "
                f"but detected {rows:,}."
            )
        if rows == 0:
            LOG.info("No matches; no columns selected and no files downloaded.")
            save_state(cfg.state_file.with_name("dry_run.json" if dry_run else "run_summary.json"), {"matches": 0, "downloaded": False})
            return
        normalize_selected_fields(page, cfg)
        batch_size = safe_batch_size(rows, cfg)
        report = {"dataset": cfg.dataset_name, "screener_url": cfg.screener_url,
                  "matches": rows, "identifier_fields": cfg.identifier_fields,
                  "identifier_columns": cfg.id_columns,
                  "max_total_columns": min(200, cfg.cell_limit // rows),
                  "max_data_columns": batch_size, "cell_limit": cfg.cell_limit,
                  "criteria_fingerprint": state.get("criteria_fingerprint"),
                  "dry_run": dry_run, "downloaded": False}
        report_path = cfg.state_file.with_name("dry_run.json" if dry_run else "run_summary.json")
        save_state(report_path, report)
        if not dry_run and cfg.dataset_name:
            save_state(cfg.state_file, state)
        LOG.info("Rows=%s; IDs=%s; data fields per file=%s", rows, cfg.id_columns, batch_size)
        pending = state.get("pending_batch")
        if pending and not dry_run:
            if not pending.get("file"):
                resume_pending_download(page, cfg, state)
            LOG.info("Resuming cleanup for downloaded batch %s", pending["number"])
            clean_selected_batch(page, cfg, pending["fields"])
            complete_pending(state, cfg.state_file)
        while True:
            open_columns(page, cfg)
            fields = select_next_fields(page, cfg, set(state["downloaded_fields"]), batch_size)
            if not fields:
                LOG.info("No unprocessed visible fields remain; finished.")
                break
            if dry_run:
                LOG.info("Dry run selected: %s", fields)
                remove_all_non_identifier_fields(page, cfg)
                apply_columns(page, cfg)
                report["selected_tasks"] = fields
                report["status"] = "selection-and-cleanup-passed"
                save_state(report_path, report)
                break
            apply_columns(page, cfg)
            number = int(state["completed_batches"]) + 1
            mark_pending(state, cfg.state_file, number, fields, explicit_periods=cfg.all_time_points)
            saved = export_and_download(page, cfg, number)
            mark_downloaded(state, cfg.state_file, saved)
            LOG.info("Saved %s (%s)", saved, ", ".join(fields))
            clean_selected_batch(page, cfg, fields)
            complete_pending(state, cfg.state_file)
    except Exception:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        failure_dir = cfg.output_dir / "failure"
        failure_dir.mkdir(parents=True, exist_ok=True)
        shot = failure_dir / f"failure-{stamp}.png"
        if dry_run and cfg.dataset_name:
            save_state(cfg.state_file.with_name("dry_run.json"), {
                "dataset": cfg.dataset_name, "status": "failed", "dry_run": True,
                "downloaded": False, "error": str(sys.exc_info()[1]), "screener_url": cfg.screener_url})
        if cfg.dataset_name:
            try:
                # Save only the query UI, not the outer page's authentication attributes.
                query = page.locator(".query-criteria")
                if query.count():
                    diagnostic_dir = cfg.state_file.parent / "failure"
                    save_state(diagnostic_dir / f"criteria-{stamp}.dom.json", {
                        "html": query.first.evaluate("element => element.outerHTML")})
                legacy = page.locator(".modal-field-selector:visible")
                if legacy.count():
                    save_state(cfg.state_file.parent / "failure" / f"selector-{stamp}.dom.json", {
                        "html": legacy.evaluate("element => element.outerHTML")})
            except Exception:
                LOG.warning("Could not save Criteria diagnostic", exc_info=True)
        try:
            labels = page.get_by_text("Selected Fields", exact=True)
            if labels.count():
                diagnostic = labels.first.evaluate("""element => {
                    const ancestors = [];
                    for (let node = element, i = 0; node && i < 6; node = node.parentElement, i++) {
                        ancestors.push(node.outerHTML);
                    }
                    return ancestors;
                }""")
                diagnostic_dir = cfg.state_file.parent / "failure"
                diagnostic_dir.mkdir(parents=True, exist_ok=True)
                diagnostic_path = diagnostic_dir / f"failure-{stamp}.dom.json"
                diagnostic_path.write_text(json.dumps(diagnostic, indent=2), encoding="utf-8")
                LOG.info("Field Selector DOM diagnostic: %s", diagnostic_path)
        except Exception:
            LOG.warning("Could not save Field Selector DOM diagnostic", exc_info=True)
        try:
            page.screenshot(path=str(shot), full_page=True)
            LOG.exception("Stopped safely. Screenshot: %s; URL: %s", shot, page.url)
        except Exception:
            LOG.exception("Stopped safely; failure screenshot could not be captured. URL: %s", page.url)
        raise
    finally:
        try:
            context.close()
        except Exception:
            LOG.warning("Browser connection already closed during cleanup", exc_info=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path(__file__).parent / "json" / "config.json")
    parser.add_argument("--reset", action="store_true", help="start again and clear checkpoint")
    parser.add_argument("--dry-run", action="store_true", help="select one batch but do not export")
    parser.add_argument("--dataset", help="independent dataset slug, e.g. real-estate")
    parser.add_argument("--url", help="exact Screener URL for a new dataset")
    parser.add_argument("--download", action="store_true", help="explicitly enable downloads for a dataset")
    parser.add_argument("--split-by", help="create partitions from a confirmed multi-select, e.g. Property Types")
    parser.add_argument("--partition", help="run one existing partition, e.g. office")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.download and args.dry_run:
        parser.error("--download and --dry-run are mutually exclusive")
    if args.url and not args.dataset:
        parser.error("--url requires --dataset")
    config_path = initialize_dataset(args.config.resolve(), args.dataset, args.url) if args.dataset else args.config
    if args.split_by:
        if not args.dataset:
            parser.error("--split-by requires --dataset")
        partitions = create_partitions(config_path, args.split_by)
        LOG.info("Created or reused %s independent partitions", len(partitions))
        if not args.partition:
            for path in partitions:
                print(path)
            return
    if args.partition:
        if not args.dataset or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", args.partition):
            parser.error("--partition requires --dataset and a valid partition slug")
        config_path = config_path.parent.parent / "partitions" / args.partition / "json" / "config.json"
    cfg = Settings.load(config_path)
    dry_run = args.dry_run or bool(cfg.dataset_name and not args.download)
    with sync_playwright() as playwright:
        run(playwright, cfg, reset=args.reset, dry_run=dry_run)


if __name__ == "__main__":
    main()
