"""Dataset setup and user-reviewed Criteria discovery for Capital IQ screeners."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def save_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def screen_identity(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    query = parsed.fragment.partition("?")[2]
    perspective = parse_qs(query).get("perspective", [""])[0]
    if parsed.scheme != "https" or parsed.hostname != "www.capitaliq.spglobal.com" or not perspective:
        raise ValueError("Use the full https://www.capitaliq.spglobal.com Screener URL with perspective=…")
    return parsed.hostname, perspective


def create_partitions(config_path: Path, criterion_label: str) -> list[Path]:
    """Split one confirmed multi-select into independent single-option tasks."""
    config = json.loads(config_path.read_text(encoding="utf-8"))
    criteria_path = config_path.parent / "criteria.json"
    document = json.loads(criteria_path.read_text(encoding="utf-8"))
    if document.get("confirmed") is not True:
        raise ValueError("Confirm parent Criteria before partitioning")
    criterion = next((c for c in document["criteria"] if c["label"] == criterion_label), None)
    if not criterion or criterion["kind"] != "select" or not criterion.get("multiple"):
        raise ValueError("Partitioning currently requires a native multi-select criterion")
    options = [o for o in criterion["options"] if not o.get("disabled")]
    if criterion["selection"]["mode"] != "all":
        options = [o for o in options if o["value"] in criterion["selection"].get("values", [])]
    paths, index, slugs = [], [], set()
    for option in options:
        slug = re.sub(r"[^a-z0-9]+", "-", option["label"].lower()).strip("-")
        if not slug or slug in slugs:
            raise ValueError("Partition names are ambiguous")
        slugs.add(slug)
        destination = config_path.parent.parent / "partitions" / slug / "json" / "config.json"
        child = dict(config, dataset_name=config["dataset_name"] + "/" + slug,
                     parent_dataset=config["dataset_name"], partition={"criterion": criterion_label, "value": option["value"], "label": option["label"]})
        child_document = json.loads(json.dumps(document))
        child_document["dataset"] = child["dataset_name"]
        child_document["confirmation_source"] = "User authorized splitting the confirmed parent scope by " + criterion_label
        for item in child_document["criteria"]:
            if item["label"] == criterion_label:
                item["selection"] = {"mode": "values", "values": [option["value"]]}
        # Existing partition runs and checkpoints must never be overwritten.
        if destination.exists():
            existing = json.loads(destination.read_text(encoding="utf-8"))
            if existing.get("partition") != child["partition"]:
                raise ValueError(f"Partition collision: {slug}")
            existing_document = json.loads(destination.with_name("criteria.json").read_text(encoding="utf-8"))
            if criteria_fingerprint(existing_document) != criteria_fingerprint(child_document):
                raise ValueError(f"Parent Criteria changed for existing partition {slug}; create a new dataset")
        else:
            save_json(destination, child)
            save_json(destination.with_name("criteria.json"), child_document)
        paths.append(destination)
        index.append({"name": slug, "label": option["label"], "config": str(destination)})
    save_json(config_path.with_name("partitions.json"), {"criterion": criterion_label,
              "note": "Partitions may overlap; deduplicate by stable identifier when merging files.", "partitions": index})
    return paths


def initialize_dataset(base_config: Path, name: str, url: str | None) -> Path:
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
        raise ValueError("Dataset name must be a lowercase slug, for example real-estate")
    app = Path(__file__).resolve().parent
    config = app / "datasets" / name / "json" / "config.json"
    if config.exists():
        existing = json.loads(config.read_text(encoding="utf-8"))
        if url and screen_identity(existing["screener_url"]) != screen_identity(url):
            raise ValueError("Dataset already belongs to another Screener; use a new dataset name")
        return config
    if not url:
        raise ValueError("A new dataset requires --url with its exact Screener URL")
    screen_identity(url)
    base = json.loads(base_config.read_text(encoding="utf-8"))
    # Resolve the existing login profile before relocating the configuration.
    profile = Path(base["profile_dir"].replace("{CONFIG_DIR}", str(base_config.parent.resolve()))).expanduser().resolve()
    base.update({
        "dataset_name": name,
        "screener_url": url,
        "profile_dir": str(profile),
        "output_dir": "{CONFIG_DIR}/../downloads",
        "state_file": "{CONFIG_DIR}/progress.json",
        "criteria_file": "{CONFIG_DIR}/criteria.json",
        "row_count": None,
        "expected_row_count": None,
        "identifier_fields": [],
        "id_columns": 0,
        "initial_selected_fields": [],
        "target_fields_per_file": 1000,
    })
    save_json(config, base)
    return config


def criteria_root(page):
    heading = page.get_by_text(re.compile(r"^CRITERIA$", re.I)).first
    heading.wait_for(state="visible")
    container = heading
    for _ in range(12):
        container = container.locator("xpath=..")
        if container.get_by_role("button", name=re.compile(r"^Run Screen$", re.I)).count():
            return container
    raise RuntimeError("Could not isolate the Criteria region containing Run Screen")


def controls_snapshot(root) -> list[dict]:
    """Inspect controls before Run Screen, retaining labels rather than menu text."""
    return root.evaluate(r"""root => {
        function path(el) {
            const parts = [];
            for (let n = el; n && n !== root; n = n.parentElement) {
                const siblings = Array.from(n.parentElement.children).filter(x => x.tagName === n.tagName);
                parts.unshift(n.tagName.toLowerCase() + ':nth-of-type(' + (siblings.indexOf(n) + 1) + ')');
            }
            return ':scope > ' + parts.join(' > ');
        }
        const visible = el => el.getClientRects().length > 0;
        const clean = s => (s || '').replace(/\s+/g, ' ').trim();
        const run = Array.from(root.querySelectorAll('button')).find(b => visible(b) && /^run screen$/i.test(clean(b.textContent)));
        const heading = Array.from(root.querySelectorAll('*')).find(n => n.children.length === 0 && /^criteria$/i.test(clean(n.textContent)));
        const found = [];
        for (const el of root.querySelectorAll('button, input, select, textarea, [role="combobox"]')) {
            const nativeSelect = el.tagName === 'SELECT' && visible(el.closest('.value-column, .bootstrap-select') || el);
            if ((!visible(el) && !nativeSelect) || !run || !(el.compareDocumentPosition(run) & Node.DOCUMENT_POSITION_FOLLOWING)) continue;
            if (heading && !(heading.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING)) continue;
            if (el.matches('button') && (el.closest('.value-column')?.querySelector('select') || el.closest('.bootstrap-select')?.querySelector('select'))) continue;
            if (el.matches('input[type="hidden"]') || el.closest('[role="option"], [role="listbox"]')) continue;
            if (el.closest('.add-criteria-row') || el.matches('input[type="submit"]')) continue;
            if (el.closest('[role="combobox"]') && el.getAttribute('role') !== 'combobox') continue;
            const column = el.closest('.value-column');
            const rowLabel = column?.parentElement.querySelector('.label-column') || el.closest('.top-n-section')?.querySelector('.control-label');
            let label = clean(rowLabel?.textContent || el.labels?.[0]?.textContent || el.getAttribute('aria-label'));
            if (!label) {
                for (let n = el, depth = 0; n && n !== root && depth < 10; n = n.parentElement, depth++) {
                    const previous = n.previousElementSibling;
                    if (previous && previous.getClientRects().length && !previous.querySelector('button,input,select,textarea')) {
                        const text = clean(previous.textContent);
                        if (text && text.length < 100) { label = text; break; }
                    }
                    const group = n.matches('.form-group') ? n : null;
                    const text = clean(group?.querySelector('label, .control-label, .form-label')?.textContent);
                    if (text) { label = text; break; }
                }
            }
            if (!label || /^(add|select all|deselect all|run screen)$/i.test(clean(el.textContent))) continue;
            const kind = el.tagName === 'SELECT' ? 'select' : el.matches('input[type="checkbox"]') ? 'checkbox' :
                el.matches('input,textarea') && !el.matches('[role="combobox"]') ? 'text' : 'menu';
            found.push({label, locator: path(el), kind, disabled: !!el.disabled,
                current: kind === 'checkbox' ? el.checked : kind === 'text' ? el.value : clean(el.textContent),
                input_type: el.getAttribute('type') || '', readonly: !!el.readOnly,
                multiple: el.tagName === 'SELECT' ? el.multiple : null});
        }
        return found;
    }""")


def option_snapshot(page) -> list[dict]:
    return page.locator('[role="option"]:visible').evaluate_all(r"""nodes => nodes.map(el => {
        const input = el.querySelector('input[type="checkbox"],input[type="radio"]');
        const named = el.querySelector('[title]');
        return {label: (named?.getAttribute('title') || el.textContent || '').replace(/\s+/g,' ').trim(),
            selected: input ? input.checked : el.getAttribute('aria-selected') === 'true',
            checkbox: !!el.querySelector('input[type="checkbox"]'),
            disabled: el.getAttribute('aria-disabled') === 'true' || el.getAttribute('data-select-option-disabled') === 'true'};
    })""")


def menu_complete(page) -> bool:
    return not page.locator('[role="option"]:visible').evaluate_all("""nodes => nodes.some(el => {
        for (let n = el; n; n = n.parentElement) {
            if (/virtual/i.test(n.className || '')) return true;
            const total = Number(n.getAttribute('aria-setsize') || 0);
            if (total > nodes.length) return true;
        }
        return false;
    })""")


def close_menu(page, control) -> None:
    if page.locator('[role="option"]:visible').count():
        control.click()
    if page.locator('[role="option"]:visible').count():
        page.keyboard.press("Escape")
    if page.locator('[role="option"]:visible').count():
        raise RuntimeError("Criteria dropdown did not close")


def discover_criteria(page, cfg) -> dict:
    root = criteria_root(page)
    controls = controls_snapshot(root)
    if not controls:
        raise RuntimeError("No Criteria controls discovered; inspect criteria DOM before proceeding")
    criteria = []
    for index, item in enumerate(controls):
        control = root.locator(item["locator"])
        record = dict(item, id=f"criterion-{index + 1}", options=[], complete=True)
        if item["kind"] == "select":
            record["options"] = control.locator("option").evaluate_all("""nodes => nodes.map(o =>
                ({label:o.text, value:o.value, selected:o.selected, disabled:o.disabled}))""")
            values = [o["value"] for o in record["options"] if o["selected"]]
            record["selection"] = {"mode": "values", "values": values}
        elif item["kind"] == "menu" and not item["disabled"]:
            control.click()
            try:
                parent = control.locator("xpath=..")
                links = parent.locator(".dropdown-menu [data-role='node'] > a")
                if links.count():
                    record["kind"] = "tree"
                    record["options"] = links.evaluate_all("""links => links.map(a => ({
                        label:a.textContent.trim(), path:a.parentElement.getAttribute('data-path'),
                        selected:a.parentElement.querySelector(':scope > [data-role="checkbox"]')?.getAttribute('data-chk') === 'on',
                        disabled:false}))""")
                    current = re.sub(r"[\ue000-\uf8ff]", "", str(item["current"])).strip()
                    record["selection"] = {"mode": "values", "values": [o["label"] for o in record["options"] if o["label"] == current]}
                    record["enumeration_scope"] = "all currently loaded hierarchy nodes, including collapsed descendants"
                    record["complete"] = False
                    # A named universe root is an exact, reproducible selection
                    # even when its many descendants have not been enumerated.
                    if current == "All Geographies" and current in record["selection"]["values"]:
                        record["complete"] = True
                        record["universe_root"] = "All Geographies"
                    continue_record = True
                else:
                    continue_record = False
                    page.locator('[role="option"]:visible').first.wait_for(state="visible", timeout=cfg.ui_timeout_ms)
                    record["options"] = option_snapshot(page)
                    record["complete"] = menu_complete(page)
                labels = [o.get("path", o["label"]) for o in record["options"] if not o["disabled"]]
                if len(labels) != len(set(labels)):
                    record["complete"] = False
                if not continue_record:
                    record["selection"] = {"mode": "values", "values": [o["label"] for o in record["options"] if o["selected"]]}
            finally:
                close_menu(page, control)
                if control.locator("xpath=..").locator(".dropdown-menu:visible").count():
                    control.click()
        else:
            record["selection"] = {"mode": "value", "value": item["current"]}
        if item["disabled"] or item["readonly"]:
            record["selection"] = {"mode": "keep"}
        criteria.append(record)
    return {"version": 1, "dataset": cfg.dataset_name, "screener_url": cfg.screener_url,
            "confirmed": False, "criteria": criteria,
            "instructions": "Review every selection. Use values/all for menus, value for inputs. Set confirmed=true only after review. Incomplete inventories require UI calibration."}


def validate_criteria(document: dict, cfg) -> None:
    if document.get("dataset") != cfg.dataset_name or screen_identity(document["screener_url"]) != screen_identity(cfg.screener_url):
        raise ValueError("Criteria belongs to another dataset or Screener")
    if document.get("confirmed") is not True:
        raise ValueError(f"Review {cfg.criteria_file} and set confirmed=true before continuing")
    if not document.get("criteria"):
        raise ValueError("Confirmed Criteria cannot be empty")
    ids = [item["id"] for item in document["criteria"]]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate criterion IDs")
    for item in document["criteria"]:
        selection = item["selection"]
        if selection.get("mode") not in {"values", "all", "value", "keep"}:
            raise ValueError(f"Invalid selection mode for {item['label']}")
        if item.get("complete") is not True:
            raise ValueError(f"Incomplete option inventory for {item['label']}; calibrate this control first")
        if selection["mode"] == "keep" and not (item.get("disabled") or item.get("readonly")):
            raise ValueError(f"Editable criterion {item['label']} needs an explicit saved selection")
        if item["kind"] in {"text", "checkbox"} and selection["mode"] != "keep":
            if selection["mode"] != "value":
                raise ValueError(f"Input {item['label']} requires mode=value")
            if item["kind"] == "checkbox" and not isinstance(selection.get("value"), bool):
                raise ValueError(f"Checkbox {item['label']} requires true or false")
        if item["kind"] == "select" and not item.get("multiple"):
            if selection["mode"] == "all" or len(selection.get("values", [])) != 1:
                raise ValueError(f"Single-choice control {item['label']} needs exactly one value")
        if item["kind"] == "tree" and item.get("universe_root"):
            if selection != {"mode": "values", "values": [item["universe_root"]]}:
                raise ValueError("This tree inventory covers All Geographies only; discover child locations for narrower selections")
        if item["kind"] in {"menu", "select", "tree"} and selection["mode"] == "values":
            key = "value" if item["kind"] == "select" else "label"
            known = {o[key] for o in item["options"] if not o.get("disabled")}
            if not set(selection.get("values", [])) <= known:
                raise ValueError(f"Unknown selected option for {item['label']}")


def criteria_fingerprint(document: dict) -> str:
    relevant = {"url": screen_identity(document["screener_url"]),
                "criteria": [{"label": c["label"], "selection": c["selection"]} for c in document["criteria"]]}
    return hashlib.sha256(json.dumps(relevant, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def apply_criteria(page, cfg, document: dict) -> None:
    validate_criteria(document, cfg)
    root = criteria_root(page)
    live = controls_snapshot(root)
    for index, item in enumerate(document["criteria"]):
        expected_kind = "menu" if item["kind"] == "tree" else item["kind"]
        if len(live) != len(document["criteria"]):
            raise RuntimeError("Criteria control count changed; rediscover the options before applying")
        if index >= len(live) or live[index]["label"] != item["label"] or live[index]["kind"] != expected_kind:
            raise RuntimeError(f"Criteria layout changed at {item['label']}; rediscover before applying")
        control = root.locator(live[index]["locator"])
        selection = item["selection"]
        mode = selection["mode"]
        if mode == "keep":
            continue
        if item["kind"] == "text":
            control.fill(str(selection.get("value", "")))
        elif item["kind"] == "checkbox":
            control.set_checked(bool(selection["value"]))
        elif item["kind"] == "select":
            values = [o["value"] for o in item["options"] if not o.get("disabled")] if mode == "all" else selection["values"]
            control.select_option(values, force=True)
            selected = control.locator("option").evaluate_all("nodes => nodes.filter(o => o.selected).map(o => o.value)")
            if set(selected) != set(values):
                raise RuntimeError(f"Could not apply native selection for {item['label']}")
        elif item["kind"] == "tree":
            control.click()
            parent = control.locator("xpath=..")
            for value in selection["values"]:
                target = parent.locator('.dropdown-menu [data-role="node"]').filter(
                    has=page.get_by_role("link", name=value, exact=True)
                )
                # Use the universe root directly: selecting its anchor can
                # toggle an already-selected root off in the Ignite tree.
                if value == item.get("universe_root"):
                    target = parent.locator('.dropdown-menu [data-role="node"]').first
                checkbox = target.locator(':scope > [data-role="checkbox"]')
                if checkbox.count() != 1:
                    raise RuntimeError(f"Ambiguous tree checkbox for {value}")
                if checkbox.get_attribute("data-chk") != "on":
                    checkbox.click()
                if checkbox.get_attribute("data-chk") != "on":
                    raise RuntimeError(f"Could not select geographic scope {value}")
            if parent.locator(".dropdown-menu:visible").count():
                control.click()
        else:
            control.click()
            try:
                page.locator('[role="option"]:visible').first.wait_for(state="visible", timeout=cfg.ui_timeout_ms)
                available = option_snapshot(page)
                wanted = {o["label"] for o in available if not o["disabled"]} if mode == "all" else set(selection["values"])
                if not wanted <= {o["label"] for o in available if not o["disabled"]}:
                    raise RuntimeError(f"Saved option disappeared from {item['label']}")
                for option in available:
                    if option["disabled"] or option["selected"] == (option["label"] in wanted):
                        continue
                    target = page.get_by_role("option", name=option["label"], exact=True)
                    if target.count() != 1:
                        raise RuntimeError(f"Option locator is ambiguous: {item['label']} / {option['label']}")
                    if option["checkbox"]:
                        label = target.locator("label[data-option-label]")
                        if label.count() == 1:
                            label.click()
                        else:
                            target.locator("input[type='checkbox']").set_checked(option["label"] in wanted)
                    elif option["label"] in wanted:
                        target.click()
                actual = {o["label"] for o in option_snapshot(page) if o["selected"] and not o["disabled"]}
                if actual != wanted:
                    raise RuntimeError(f"Could not verify Criteria selection for {item['label']}: {actual!r} != {wanted!r}")
            finally:
                close_menu(page, control)


def prepare_dataset_criteria(page, cfg) -> dict | None:
    if not cfg.criteria_file.exists():
        draft = discover_criteria(page, cfg)
        save_json(cfg.criteria_file, draft)
        for item in draft["criteria"]:
            print(f"\n{item['id']}: {item['label']} ({item['kind']})")
            for option in item["options"]:
                print(f"  {'[x]' if option['selected'] else '[ ]'} {option['label']}")
        print(f"\nReview selections in {cfg.criteria_file}; set confirmed=true, then rerun. No data downloaded.")
        return None
    document = json.loads(cfg.criteria_file.read_text(encoding="utf-8"))
    validate_criteria(document, cfg)
    apply_criteria(page, cfg, document)
    return document
