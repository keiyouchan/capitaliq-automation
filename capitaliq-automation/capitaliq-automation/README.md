# Capital IQ Screener Exporter

[中文文档](README_zh.md)

A Python Playwright tool for exporting S&P Capital IQ Pro Screener data in resumable batches. It supports the original Regulated Depositories (U.S.) workflow and independent datasets with reviewed Criteria, including Real Estate. It selects columns, exports **Results As Values**, saves downloads, and removes exported columns before continuing. New datasets default to dry-run.

## Project layout

```text
playwright-project/
├── AGENTS.md
├── requirements.txt
├── tests/
└── capitaliq-automation/capitaliq-automation/
    ├── README.md / README_zh.md
    ├── capitaliq_screener_export.py
    ├── .cvenv/                   # active local Python environment
    ├── reference/                # recorded.py and recorded_time.py
    ├── json/
    │   ├── config.example.json
    │   ├── config.json           # local configuration
    │   ├── progress.json         # checkpoint and pending batch
    │   ├── downloaded_fields.json
    │   ├── time_points.json      # discovered time options
    │   ├── field_cache.json      # field IDs and ancestor paths
    │   └── failure/              # failure-*.dom.json diagnostics
    ├── downloads/
    │   ├── capitaliq_0001.xls    # suffix follows the downloaded file
    │   └── failure/              # failure-*.png screenshots
    └── browser-profile/          # persistent login session
```

Application JSON files belong in `json/`. Chromium manages its own internal files inside `browser-profile/`; do not move those individually.

## Install dependencies

Requirements: Python 3.10+, the Python package pinned in `requirements.txt` (`playwright==1.64.1`), Chromium installed by Playwright, and a Capital IQ account with Screener/export access. The following commands target macOS/Linux; start at the repository root:

```bash
cd capitaliq-automation/capitaliq-automation
python3 -m venv .cvenv
.cvenv/bin/python -m pip install --upgrade pip
.cvenv/bin/python -m pip install -r ../../requirements.txt
.cvenv/bin/python -m playwright install chromium
cp -n json/config.example.json json/config.json
```

If `.cvenv` already exists, skip environment creation and use its Python to install/update dependencies. `cp -n` preserves an existing local configuration. No activation, Node.js or npm is required. Review `json/config.json` before the first run; the template contains the bank Screener's expected match count.

## Quick start

All commands below assume you are in `capitaliq-automation/capitaliq-automation/`, as after installation. From a new terminal at the repository root, enter that directory first:

```bash
cd capitaliq-automation/capitaliq-automation
```

### Bank data: Regulated Depositories (U.S.)

```bash
# Validate selection and cleanup without downloading
.cvenv/bin/python capitaliq_screener_export.py --dry-run

# Export and download
.cvenv/bin/python capitaliq_screener_export.py --config json/config.json
```

The bank workflow downloads when `--dry-run` is omitted. Files go to `downloads/`; existing progress is read from `json/progress.json`. Do not add `--reset` when continuing an existing task.

### Existing Real Estate Office partition

For this checkout, the reviewed Criteria and Office partition already exist:

```bash
# Selection/cleanup test only
.cvenv/bin/python capitaliq_screener_export.py --dataset real-estate --partition office --dry-run

# Continuous download; not limited to a single test file
.cvenv/bin/python capitaliq_screener_export.py --dataset real-estate --partition office --download
```

Files go to `datasets/real-estate/partitions/office/downloads/`. Dataset mode defaults to dry-run, so removing `--dry-run` alone does not enable downloads.

### Start another dataset

Replace `YOUR_FULL_SCREENER_URL` with the URL of the exact page, including `perspective=...`:

```bash
.cvenv/bin/python capitaliq_screener_export.py --dataset energy-utilities \
  --url 'YOUR_FULL_SCREENER_URL' --dry-run
```

Review `datasets/energy-utilities/json/criteria.json`, adjust `selection` values, and set `confirmed` to `true`. Then rerun without supplying the URL:

```bash
.cvenv/bin/python capitaliq_screener_export.py --dataset energy-utilities --dry-run
```

After dry-run succeeds, use `--download` explicitly to export. A fresh Real Estate setup follows the same Criteria confirmation process before creating its partitions; see [Additional screeners and saved Criteria](#additional-screeners-and-saved-criteria).

Complete login in the opened browser. An interactive terminal prompts for Enter; a non-interactive runner waits for Screener controls. In a shell, type the command without a leading `!`.

The previous `--config .../config.json` path remains supported when the file has moved to its sibling `json/` directory. New commands should use `json/config.json` or omit `--config`.

## Configuration and batching

Paths containing `{CONFIG_DIR}` resolve relative to the configuration directory. The supplied configuration uses `../downloads`, `../browser-profile`, and `progress.json`, keeping the existing exports and session in place.

- `expected_row_count`: expected filter result, currently 46,403; a mismatch stops the run. Update this when the intended dataset changes.
- `identifier_fields` / `id_columns`: retain Company Name and SNL Institution Key in every export.
- `target_fields_per_file` / `cell_limit`: requested batch size and configured cell ceiling. With 46,403 rows, two identifier columns, and a 500,000-cell ceiling, at most eight data columns fit.
- `all_time_points`: selects multiple unfinished time options up to the remaining column capacity. The classic Real Estate selector accumulates ordinary fields and time-point columns in the same batch; the React selector still processes one source field per batch.
- `selectors`: UI locators; site changes may require recalibration.
- `download_timeout_ms`: loading/download wait, currently 300 seconds.

Time coverage means options exposed by the field's current Period Type, including explicit dates and relative periods such as MstRctYear or YTD. The tool does not automatically enumerate every combination of Year/Quarter and other parameter axes. Relative periods can overlap explicit dates. Unsupported or virtualized menus stop with diagnostics rather than being treated as complete.

## Resume and downloaded-field inventory

Rerun the same command with the same configuration to retain progress. `downloaded_fields.json` lists field/time-point tasks and known batch files; `progress.json` is the authoritative checkpoint. A download is recorded before column cleanup. Pending tasks are reconstructed when supported, and existing output filenames are protected against overwrite.

Legacy name-only records do not prove historical coverage. Do not delete checkpoints or use `--reset` to resolve routine failures: reset clears the checkpoint but leaves exported files, which can cause filename conflicts. For a separate export, use a separate state directory and output directory. Run only one exporter per profile/state directory.

## Additional screeners and saved Criteria

Create a dataset with its exact Screener URL. New datasets default to **dry-run**; downloading requires an explicit `--download` flag. From the application directory:

```bash
.cvenv/bin/python capitaliq_screener_export.py --dataset real-estate \
  --url 'https://www.capitaliq.spglobal.com/web/client?auth=inherit#office/screener?perspective=289' \
  --dry-run
```

On the first visit the tool inventories the page's Criteria controls and saves `datasets/<name>/json/criteria.json` with `confirmed: false`. Review the printed options, edit each `selection`, then set `confirmed: true`. Native multi-selects support `{"mode":"all"}` or `{"mode":"values","values":["option-value"]}`; text inputs use `{"mode":"value","value":""}`. Single-choice controls, including Return, require one value. The Real Estate criteria in this checkout have already been confirmed with the user.

Later runs reuse these selections. Each dataset has its own `json/`, `downloads/`, failure diagnostics, caches and checkpoint. The browser login profile is shared, so run one dataset at a time. Criteria fingerprints prevent accidentally resuming a checkpoint with changed filters. Custom or incomplete widgets stop with diagnostics rather than silently applying guessed values.

Matches are read from the actual results, with mandatory identifier columns discovered in the field selector. Column capacity is `min(200, floor(cell_limit / matches))`, including identifiers. New datasets do not inherit the bank Screener's 46,403-row expectation. Both the React/jsTree and classic Ignite field selectors are supported; unrecognized classic time-parameter widgets still need calibration.

### Large results: partitions

The website rejected the all-inclusive Real Estate screen as over 250,000 rows. The user-approved scope is retained at the parent level and split into 15 Property Types:

```bash
# Create isolated partitions without visiting the website or downloading
.cvenv/bin/python capitaliq_screener_export.py --dataset real-estate --split-by 'Property Types'

# Test one partition; leave out --url after initialization
.cvenv/bin/python capitaliq_screener_export.py --dataset real-estate --partition office --dry-run
```

Partition files live in `datasets/real-estate/partitions/office/{json,downloads}/`. Its inherited Investment Status includes all five options, and geography includes All Geographies. The Office browser check returned 34,507 rows: with two identifiers and the conservative 500,000-cell ceiling, 14 total columns / 12 data columns fit. Other partitions need their own checks and may require further narrowing if they exceed the row limit. Partitions may overlap; deduplicate by Property Key when merging exports.

`json/dry_run.json` records the validation method, matches, column budget and whether a download occurred. A native browser check is distinguished from a full automated Playwright run.

Classic trees are consumed only after loading placeholders disappear and the node list stabilizes. New tasks distinguish identically named fields by node path/ID. Legacy name-only records are reused only when their cached paths agree; uncertain mappings may be exported again to avoid omissions. `json/field_inventory.json` lists discovered fields and their status, without claiming that unopened branches are fully enumerated. Historical loading placeholders are flagged under `suspect_loading_placeholders` in `downloaded_fields.json`; existing exports are not deleted automatically.

## Troubleshooting and verification

- `ERR_CONNECTION_CLOSED`: check the same URL in a normal browser and inspect network/proxy connectivity.
- Loading overlay: wait for calculation; increasing the timeout may help a slow screen.
- Locator or parameter errors: inspect matching files in `downloads/failure/` and `json/failure/`.

From the application directory, run `.cvenv/bin/python -B -m unittest discover -s ../../tests -v`. Tests cover Criteria confirmation, dataset isolation, partition reuse, column budgets and ensuring pending batches cannot download during dry-run. Check syntax with `.cvenv/bin/python -m py_compile capitaliq_screener_export.py`, then run `--dry-run`. Dry-run verifies selection and cleanup, not export format, file validity, or the entire category tree. Confirm the first real file and its columns before relying on a long run.

Keep profiles, checkpoints, proprietary downloads, and failure diagnostics local. `.gitignore` excludes these runtime artifacts; the shareable configuration template remains included.
