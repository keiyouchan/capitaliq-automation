# Capital IQ Screener 自动导出工具

[English](README.md)

使用 Python Playwright 分批下载 S&P Capital IQ Pro Screener 数据。支持原 Regulated Depositories (U.S.) 流程，以及按数据集独立保存和确认 Criteria 的新流程（已加入 Real Estate）。程序添加字段、以 **Results As Values** 导出，下载后清理字段并继续下一批；新数据集默认只做 dry-run。

## 目录结构

```text
capitaliq-automation/
├── requirements.txt
├── .gitignore
└── capitaliq-automation/
    ├── README.md / README_zh.md
    ├── capitaliq_screener_export.py
    ├── .cvenv/                   # 当前使用的 Python 环境
    ├── reference/                # recorded.py、recorded_time.py
    ├── json/
    │   ├── config.example.json   # 可分享的配置模板
    │   ├── config.json           # 本地运行配置
    │   ├── progress.json         # 断点、未完成批次
    │   ├── downloaded_fields.json # 已下载字段、时间点与批次
    │   ├── time_points.json      # 已发现的时间选项
    │   ├── field_cache.json      # 字段 ID、分类路径缓存
    │   └── failure/              # failure-*.dom.json 诊断
    ├── downloads/
    │   ├── capitaliq_0001.xls    # 扩展名采用网站实际下载文件的后缀
    │   └── failure/              # failure-*.png 错误截图
    └── browser-profile/          # Chromium 登录状态
```

项目配置及运行记录统一放在 `json/`。浏览器 Profile 内部的 JSON 等文件由 Chromium 管理，不要单独移动。

## 安装依赖（Dependencies）

需要 Python 3.10+、`requirements.txt` 中锁定的 Python 依赖（`playwright==1.64.1`）、Playwright 安装的 Chromium，以及有 Screener／导出权限的 Capital IQ 账号。以下命令适用于 macOS/Linux，从项目根目录执行：

```bash
cd capitaliq-automation/capitaliq-automation
python3 -m venv .cvenv
.cvenv/bin/python -m pip install --upgrade pip
.cvenv/bin/python -m pip install -r ../requirements.txt
.cvenv/bin/python -m playwright install chromium
cp -n json/config.example.json json/config.json
```

如果已有 `.cvenv`，跳过创建环境这一步，直接使用其中的 Python 安装或更新依赖。`cp -n` 不会覆盖已有配置。无需激活环境，也不需要 Node.js 或 npm。首次运行前检查 `json/config.json`；模板中的预期 Matches 是银行页面的数量。

## 快速开始（Quick Start）

以下命令均在程序目录执行，与上面安装后的目录一致。新开终端时，先从项目根目录进入：

```bash
cd capitaliq-automation/capitaliq-automation
```

### 银行数据：Regulated Depositories (U.S.)

```bash
# 仅测试字段选择、清理和 Apply，不下载
.cvenv/bin/python capitaliq_screener_export.py --dry-run

# 正式导出下载
.cvenv/bin/python capitaliq_screener_export.py --config json/config.json
```

银行旧模式去掉 `--dry-run` 就会正式下载。文件保存到 `downloads/`，读取 `json/progress.json` 继续已有进度；续跑时不要添加 `--reset`。

### 已配置的 Real Estate Office 子任务

当前工作目录已保存确认后的筛选标准和 Office 子任务：

```bash
# 只测试选择、清理和 Apply
.cvenv/bin/python capitaliq_screener_export.py --dataset real-estate --partition office --dry-run

# 持续下载，不是只下载一个测试文件
.cvenv/bin/python capitaliq_screener_export.py --dataset real-estate --partition office --download
```

文件保存到 `datasets/real-estate/partitions/office/downloads/`。新数据集模式默认 dry-run，仅去掉 `--dry-run` 不会下载，必须显式添加 `--download`。

### 首次配置其他数据集

将下面的 `YOUR_FULL_SCREENER_URL` 替换成目标页面的完整 URL，包含 `perspective=...`：

```bash
.cvenv/bin/python capitaliq_screener_export.py --dataset energy-utilities \
  --url 'YOUR_FULL_SCREENER_URL' --dry-run
```

检查生成的 `datasets/energy-utilities/json/criteria.json`，调整每项 `selection`，确认后将 `confirmed` 设为 `true`。之后无需重复填写 URL：

```bash
.cvenv/bin/python capitaliq_screener_export.py --dataset energy-utilities --dry-run
```

通过 dry-run 后，显式添加 `--download` 才正式导出。全新目录首次设置 Real Estate 时，也应先完成标准确认，再生成拆分子任务；详见[拓展到其他 Screener](#拓展到其他-screener)。

需要时在弹出的浏览器完成登录。交互终端会提示按 Enter；无标准输入的运行器会自动等待 Screener 控件出现。直接在终端运行时，命令前不要添加 `!`。

旧命令中的 `--config .../config.json` 在文件已移到相邻 `json/` 目录时仍可使用。建议新命令写成 `json/config.json`，或省略 `--config` 使用新默认路径。

## 配置与分批规则

`{CONFIG_DIR}` 代表配置文件所在目录。当前配置分别使用 `../downloads`、`../browser-profile` 和 `progress.json`，迁移后下载目录及登录状态的位置保持不变。

- `expected_row_count`：预期筛选结果，当前为 46,403。数量不符会停止；数据范围发生变化时应更新。
- `identifier_fields`、`id_columns`：每次保留 Company Name 和 SNL Institution Key 两列。
- `target_fields_per_file`、`cell_limit`：目标批量与配置的单元格上限。46,403 行、两列标识、500,000 单元格上限时，每批最多八个数据列。
- `all_time_points`：按剩余列容量多选尚未下载的时间点。Real Estate 旧版选择器会在同一批中累积普通字段和时间点列；新版 React 选择器目前仍按一个来源字段组织批次。
- `selectors`：网页定位器，网站改版后可能需要调整。
- `download_timeout_ms`：页面计算及下载等待上限，当前为 300 秒。

“全部时间点”指字段当前 Period Type 下菜单提供的选项，包含明确日期以及 MstRctYear、YTD 等相对期间。程序尚未自动遍历 Year/Quarter 等多个参数轴的所有组合。相对期间可能与明确年份重叠。遇到未适配或虚拟滚动菜单时，会停止并保存诊断，不会直接标记全部完成。

## 断点与已下载清单

继续使用同一配置运行即可保留进度。`progress.json` 是主断点文件；`downloaded_fields.json` 是方便查看的字段／时间点与批次清单。文件下载确认后立即记录，随后才清理页面字段。程序会在支持的情况下重建未完成批次，并保护已有同名导出文件。

旧版仅记录字段名的任务不能证明其历史时间点已全部下载。普通报错后不要删除断点或使用 `--reset`：该参数清除断点但不删除下载文件，可能产生编号冲突。全新任务应使用独立的状态目录和下载目录。同一 Profile 和断点目录只运行一个进程。

## 拓展到其他 Screener

每个数据集需要独立名称和完整 URL。新数据集默认执行 **dry-run**；只有显式添加 `--download` 才允许下载。在程序目录运行：

```bash
.cvenv/bin/python capitaliq_screener_export.py --dataset real-estate \
  --url 'https://www.capitaliq.spglobal.com/web/client?auth=inherit#office/screener?perspective=289' \
  --dry-run
```

首次访问会枚举 Criteria 控件与选项，保存到 `datasets/<名称>/json/criteria.json`，初始 `confirmed` 为 `false`。检查终端列出的选项，修改每个条件的 `selection`，确认后设为 `true`。原生多选框可以使用 `{"mode":"all"}` 全选，或 `{"mode":"values","values":["原生选项值"]}`；文本框使用 `{"mode":"value","value":""}`。Return 等单选框只能保存一个选项。本次 Real Estate 的全选范围已由用户确认并保存。

后续自动读取标准。各数据集拥有独立 `json/`、`downloads/`、断点和诊断文件，登录 Profile 共享，因此一次只运行一个任务。若条件指纹与断点不符，程序会停止，避免混入另一筛选范围的数据。无法完整识别的自定义控件会保存诊断并停止。

Matches 从实际结果读取，不再沿用银行页面的 46,403。固定列从当前字段选择器自动识别；总列上限按 `min(200, floor(cell_limit / matches))` 计算，再扣除固定列。已兼容新版 React/jsTree 及旧版 Ignite 字段选择器；旧版尚未识别的时间参数控件仍需校准。

### 超过行数上限时拆分

Real Estate 全选在真实页面超过 250,000 行，网站拒绝运行。总标准保持全选，并经用户确认按 15 种 Property Types 拆分：

```bash
# 只生成独立子任务，不访问网页、不下载
.cvenv/bin/python capitaliq_screener_export.py --dataset real-estate --split-by 'Property Types'

# 先测试 Office 子任务
.cvenv/bin/python capitaliq_screener_export.py --dataset real-estate --partition office --dry-run
```

子任务位于 `datasets/real-estate/partitions/office/`，各有独立的 `json/` 与 `downloads/`。继承五种 Investment Status 全选、All Geographies、文本条件为空、All results。Office 已在浏览器验证为 34,507 行；保留 Institution Name、Property Key 两列，按保守的 500,000 单元格上限，每批最多 14 总列／12 数据列。其他类型需分别验证，超过行数上限的子任务还需进一步拆分。不同类型可能重叠，合并数据时应按 Property Key 去重。

`json/dry_run.json` 记录验证方式、Matches、列容量及是否下载。通过现有浏览器进行的验证与完整 Playwright 自动运行会明确区分。

旧版字段树展开后会出现 `Pending...` 占位符。程序会等待加载结束、列表稳定后再遍历，不将占位符作为字段。新任务按字段路径／ID 区分同名字段；旧的名称记录仅在路径缓存一致时复用，无法对应的字段可能重新下载以避免漏项。`json/field_inventory.json` 列出已发现的字段和状态，不代表尚未展开的分类已完整枚举。历史清单若含 `Pending...`，会在 `downloaded_fields.json` 的 `suspect_loading_placeholders` 中提示；已有导出文件不会自动删除。

## 排错与测试

- `ERR_CONNECTION_CLOSED`：先用普通浏览器访问同一地址，检查网络或代理连接。
- 加载遮罩：等待页面计算完成；数据处理较慢时可增加等待上限。
- 定位或时间参数错误：查看 `downloads/failure/` 截图及 `json/failure/` 中同时间戳的 DOM 文件。

在程序目录执行 `.cvenv/bin/python -m py_compile capitaliq_screener_export.py dataset_workflow.py legacy_selector.py` 检查语法，再运行 `--dry-run`。dry-run 不验证实际导出格式、文件内容或整棵分类树；长时间运行前应检查首个下载文件及列名。

登录 Profile、断点、专有下载数据及诊断文件应留在本地；`.gitignore` 已排除这些运行文件，保留可分享的配置模板。
