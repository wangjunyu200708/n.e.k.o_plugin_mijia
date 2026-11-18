# 米家智能家居插件（mijia）开发指南

> 覆盖：代码结构、怎么改代码、怎么测试、怎么发布。
> 最后更新：2026-11-18（v3.0.0）

---

## 1. 插件概览

| 项 | 值 |
|---|---|
| 插件 id | `mijia` |
| 当前版本 | v3.0.0（`plugin.toml` 与 `pyproject.toml` 必须同步） |
| 独立仓库（= 插件根） | https://github.com/wangjunyu200708/n.e.k.o_plugin_mijia |
| 开发位置 | `E:\n.e.k.o_plugin_mijia` |
| 挂载测试位置 | `E:\NEKOdm\N.E.K.O\plugin\plugins\mijia` |
| 宿主源码 | `E:\NEKOdm\N.E.K.O`（`origin` = 个人 fork，`upstream` = 官方） |
| 测试规模 | 209 用例（`pytest` 全绿） |
| 功能 | 自然语言控制米家设备：开关/属性/模式/相对调整/场景/查询 |

**工作流**：在 `E:\n.e.k.o_plugin_mijia` 开发 → 整包拷到 `plugin/plugins/mijia` 挂宿主做实机测试 → **测试完删除该拷贝**（宿主保持与上游一致）→ 提交推送 → `publish` 发版。

---

## 2. 代码结构

### 2.1 独立仓库布局（= 插件根）

```
n.e.k.o_plugin_mijia/
├── plugin.toml          # 插件清单（id/entry/version/UI/权限）
├── __init__.py          # 2161 行：MijiaPlugin 入口 + smart_control 分发 + 执行分支 + 缓存/凭据
├── property_tables.py   # 静态表：NAME_MAP(205) / UNIT_MAP(41) / PROP_KEYWORDS(16)
├── conftest.py          # 独立仓库测试桩：无宿主也能跑 pytest（宿主内自动跳过）
├── nlp/                 # 自然语言规则引擎（纯函数，无 IO/API 依赖）
│   ├── router.py            # 路由五步短路 + RouteResult
│   ├── control_parser.py    # 分界 + 意图解析 → ParseResult(含 unit/direction/delta)
│   ├── device_matcher.py    # 匹配：别名>精确名>房间拆分>模糊 → MatchResult(含 candidates)
│   ├── intent_terms.py      # 词表 + detect_scene（口语场景别名）
│   ├── action_verbs.py      # 动作动词 + VERB_TO_ACTION
│   ├── value_resolver.py    # 数值抽取/单位推断/相对量/钳值+步长对齐
│   ├── normalizer.py        # 把/请/帮我 前缀剥离（不碰实体内部文本）
│   ├── chinese_number.py    # 中文数字 → 阿拉伯（两=2、二十三=23）
│   ├── positions.py         # 多控开关方位词 → service_desc 关键词（前端资产）
│   └── capability.py        # 能力校验（单位/范围）+ CommandIR 定义
├── mijia_api/           # 米家 API 客户端（分层，**纯异步**，4665 行）
│   ├── core/                # config（网关/登录端点硬编码在此）/ logging
│   ├── domain/              # models/exceptions
│   ├── infrastructure/      # http/credential/crypto/cache
│   ├── repositories/        # device/spec/home/scene 仓库
│   ├── services/            # auth/device/scene/statistics 服务（async_* 前缀）
│   └── factory.py           # 组合根 create_async_api_client()
├── i18n/                # 8 语言 × 171 key（en/ja/ko/zh-CN/zh-TW/ru/es/pt）
├── static/              # Hosted UI 控制面板
├── docs/                # 快速开始（8 语言）
├── tests/               # 209 用例 + 回归基线 commands_execution_trace.json
├── .github/workflows/   # verify.yml + release.yml（官方可复用工作流）
├── ruff.toml            # E4,E7,E9,F,I（官方插件规则）
└── pyproject.toml       # pytest 配置（testpaths/pythonpath/asyncio_mode）
```

> 仓库内**不放**任何一次性调试脚本。它们统一放在与仓库平行的 `E:\n.e.k.o_plugin_mijia_local\`，原因见 5.2 / 5.6。

### 2.2 与宿主的关系

宿主插件目录是**整包拷贝**，不是符号链接：宿主加载的是 `plugin/plugins/mijia/` 下的副本，改完源码必须重新拷贝才生效。

`conftest.py` 让独立仓库自足：宿主模块（`config` / `plugin.sdk.plugin` / `utils.file_utils`）缺失时注入最小桩件，并用 `mijia_plugin` 包名加载根 `__init__.py`。**宿主树里三个模块都存在 → 桩件自动跳过**，因此同一套测试两边都能跑，`import` 也不需要改写。

---

## 3. 核心架构：一条指令怎么跑

### 3.1 路由五步短路（`nlp/router.py` 的 `route()`）

```
原始输入 → normalize_utterance（剥 把/请/帮我，保留 raw_text）
  ① scene：detect_scene（我回家了/晚安/打开回家模式/开启睡眠模式）
  ② switch 标记：命令以开关动词开头？→ 跳过动作分支
  ③ query：含 几度/多少度/状态 等查询词 → query 分支
  ④ action：匹配动作动词（开始/暂停/停止/回充…）→ 仅设备匹配成功才提交
  ⑤ control：parse_control_command → switch / set_prop / adjust_prop
```

### 3.2 执行分支：CommandIR 贯穿

`to_command_ir(result, raw_text=...)` 把路由结果拍平成 `CommandIR`，后端所有执行方法只吃 IR、不扫描原话：

```
CommandIR.branch
├── scene    → _execute_scene_by_name(scene_name)
├── query    → _execute_query
├── action   → _execute_action_branch(ir)          # 动作动词 → aiid
├── switch   → _execute_switch(ir)                 # 电源动作 → 开关属性+回读校验
└── control  → _execute_control_branch(ir)
                ├── set_prop    → _execute_set_prop(props, did, name, ir)
                └── adjust_prop → _execute_adjust_prop(props, did, name, ir)
                     └─ 能力校验 validate_command（单位/范围）→ invalid_unit / invalid_value
                     └─ 枚举解析 / 钳值 → control_device（写后回读轮询校验）
```

`CommandIR` 字段：`raw_text`、`action`、`room`、`device`、`device_id`、`device_info`、`attribute`、`value`、`unit`、`direction`、`delta`、`verb`、`match_status`、`match_message`、`gang`、`candidates`、`require_explicit_gang`。

**多控开关**：方位不再由后端扫原话。`to_command_ir` 调 `nlp.positions.detect_position()` 抽出 `ir.gang`（"右"/"二键"），后端只读该字段；`require_explicit_gang=True` 时缺方位返回 `gang_ambiguous` + 候选，让调用方追问而不是静默选主开关。

### 3.3 两个工具面（不可混用）

| 装饰器 | 谁调用 | 当前内容 |
|---|---|---|
| `@llm_tool` | 对话 LLM | `smart_control(command: str)` |
| `@plugin_entry` | analyzer / UI `/runs` | `open_ui`、`reload_credential`、`start_qrcode_login`、`check_login_status`、`logout`、`list_homes`、`list_devices`、`get_cached_devices`、`list_scenes`、`set_device_alias`、`get_device_aliases`、`query_device_state` |

宿主会把 `__llm_tool__*` 从 analyzer 的入口列表里剥掉，所以同一个能力**只能二选一**：给了 `@llm_tool` 就不能再用 `/runs` + `entry_id=smart_control` 触发。

### 3.4 错误约定（改错信息前必读）

内部一律 `Err(SdkError(...))`，工具层统一转信封：

| 层 | 形态 | 作用 |
|---|---|---|
| 语义失败 | `_err(code, message, **detail)` | 稳定错误码 + 结构化 detail |
| 工具信封 | `_tool_envelope(result)` → `{"output": {...}, "is_error": bool}` | 避免 IPC 异常路径把栈信息灌进模型上下文 |
| 错误内容 | `output.code` / `output.detail` / `output.message` | 码给程序与模型重试判断，detail 给自纠数据，message 给人话 |

**纪律**：语义失败必须走 `Err`。用 `Ok({"success": False})` 表示失败会被翻成 `is_error=false`，模型会以为成功了。

### 3.5 能力校验边界

- **NLP 只产语义候选，不替设备决定单位**。`亮度调到三档` → `attribute=亮度, value=3, unit=档`，由 `validate_command` 判 `invalid_unit`（亮度单位是 percent，不接受 level）。
- 校验失败时 `ValidationResult.detail` 会带出允许范围 / 期望单位，模型可据此自行重试。
- **Unknown/Incomplete 是正常解析态**（如 `调到三档` 无设备 = MISSING_DEVICE），不是 NLP 失败。
- **多指令不支持**（无 Splitter），一条指令只解析一个设备动作。

### 3.6 运行时语言

`self.i18n.t(key)` 不传 `locale` 会退回 `plugin.toml` 的 `default_locale`（zh-CN），所以必须显式透传宿主语言：`self.i18n.t(key, locale=self._lang)`。`self._lang` 在启动时解析，失败回落到 zh-CN。

---

## 4. 怎么改代码

### 4.1 新增场景（口语别名 / XX模式）

`nlp/intent_terms.py`：
- `SCENE_PHRASE_MAP`：口语别名（`"我回家了" → "home_arrival"`）
- `SCENE_MODE_RE`：`打开XX模式/开启XX模式` 识别词
- 场景名必须与 `list_scenes` 返回的名称一致（`_execute_scene_by_name` 按名查）

### 4.2 新增属性词（NLP 识别新设备属性）

三处同步改：
1. `nlp/control_parser.py` 的 `PROP_TERMS`：正则词表 —— 用于"设备名尾部属性剥离"
2. `property_tables.py` 的 `PROP_KEYWORDS`：中文属性 → 英文 spec 属性名关键词
3. `nlp/capability.py` 的 `_ATTR_KEYWORDS` + `_UNIT_BY_ATTR`：属性 → 语义单位

### 4.3 新增单位 / 调整单位校验

`nlp/capability.py`：
- `_COMMAND_UNIT`：命令单位 → 语义单位（`档/档位 → level`、`度/℃ → celsius`、`% → percent`）
- `_UNIT_BY_ATTR`：中文属性 → 期望单位
- `_UNITS_FOR_SEMANTIC`：语义单位 → 可接受字面单位（写进错误 detail）

### 4.4 新增动词（动作类指令）

`nlp/action_verbs.py`：`ACTION_VERB_RE` + `VERB_TO_ACTION`。
`nlp/control_parser.py` 的 `SPLIT_VERBS`：分界动词，**最长优先**。

### 4.5 新增执行逻辑

`_dispatch_smart_control` 按 branch 分发：新分支需在 `route()` 加短路 + 在 `__init__.py` 加 `_execute_xxx(ir)` + 返回 `Ok(...)` / `Err(SdkError(...))`。执行方法签名保持只吃 `CommandIR`，不要回退去读原话。

### 4.6 新增语言 / 改文案

- `i18n/<locale>.json`：**8 语言同步改**，当前各 171 key
- 运行时：`self.i18n.t(key, locale=self._lang, default="...", **params)`
- `tr()` 只用于装饰器/schema；`@llm_tool` 的描述必须是**纯字符串**，`tr()` 返回的 `{"$i18n": ...}` 经 IPC 会被 `str()` 拍扁成垃圾

### 4.7 新增错误码

`_err("some_code", "中文人话", key=value, ...)`。码用下划线小写；detail 只放模型自纠真正需要的东西（候选设备、允许范围、可选方位）。`@plugin_entry` 侧仍直接用 `SdkError("中文")`（analyzer/UI 面向中文用户）。

### 4.8 改 API 客户端

`mijia_api/` 分层：infrastructure（http/credential）→ repositories（spec/device）→ services（业务）→ factory（组合）。
**纯异步**：同步实现已于 3.0.0 删除，不要重新引入。宿主 event loop 内禁止同步文件 IO / SQLite / HTTP / `time.sleep`，一律用 async 助手或 `asyncio.to_thread`。

### 4.9 拷贝到宿主做测试

```bash
# Windows（Git Bash），整包覆盖
rm -rf /e/NEKOdm/N.E.K.O/plugin/plugins/mijia
cp -r /e/n.e.k.o_plugin_mijia /e/NEKOdm/N.E.K.O/plugin/plugins/mijia
rm -rf /e/NEKOdm/N.E.K.O/plugin/plugins/mijia/{.git,__pycache__,.pytest_cache,.ruff_cache}
```
测试完**必须删除**，宿主保持与上游一致（`git status` 干净）。

---

## 5. 怎么测试

### 5.1 单元测试（主要手段，独立仓库自足）

```bash
cd /e/n.e.k.o_plugin_mijia
/c/Users/25672/AppData/Local/Python/pythoncore-3.11-64/python.exe -m pytest tests/ -q
# 期望：209 passed
```

> 用系统 Python 3.11（`py -3.11`）。WorkBuddy 内置的 3.13 没装 pytest，别用它。

### 5.2 静态检查 / 本地复刻 CI（发布前必跑）

官方 CLI 已经内置 CI 同款门禁，**优先用它**，不要手写 uvx 命令：

```bash
cd /e/NEKOdm/N.E.K.O
./.venv/Scripts/python.exe -m plugin.neko_plugin_cli.cli lint     E:/n.e.k.o_plugin_mijia
./.venv/Scripts/python.exe -m plugin.neko_plugin_cli.cli precheck E:/n.e.k.o_plugin_mijia
```

- `lint` = Market CI 那条固定的 ruff（`--ignore-noqa --isolated --no-respect-gitignore --select E4,E7,E9,F,I`）
- `precheck` = lint + sync + release check 三件套
- `check <plugin> -r` = 严格发版检查（含打包与 payload hash 校验），`--market-release` 额外校验市场仓库/标签约定

⚠️ **`--no-respect-gitignore` 意味着 `.gitignore` 挡不住它**：仓库里任何 `.py`（哪怕未跟踪）都会被扫。所以**调试脚本一律不放仓库内**，统一放在仓库外的 `E:\n.e.k.o_plugin_mijia_local\`（见 5.6）。

判据很简单：`lint` 报错但 CI 是绿的，说明仓库里有未跟踪的 `.py` 被扫到了；两者结论必须一致才能发版。

### 5.3 宿主挂载测试

拷贝到 `plugin/plugins/mijia` 后，宿主目录同样能跑（`conftest.py` 检测到宿主模块即跳过桩件）：

```bash
cd /e/NEKOdm/N.E.K.O
./.venv/Scripts/python.exe -m pytest plugin/plugins/mijia/tests -q
```

### 5.4 实机测试（Windows 宿主）

改 `__init__.py` / `nlp/*` 后**必须重启 N.E.K.O**（进程缓存的旧模块不会热更新）。

```bash
# 1) 启动宿主（宿主目录）
cd /e/NEKOdm/N.E.K.O && ./.venv/Scripts/python.exe launcher.py

# 2) 插件加载 → 启动 → 重载凭据（用户插件服务端口 48916）
curl -s -X POST http://127.0.0.1:48916/plugins/refresh
curl -s -X POST http://127.0.0.1:48916/plugin/mijia/start
curl -s -X POST http://127.0.0.1:48916/runs -H "Content-Type: application/json" \
  -d '{"plugin_id":"mijia","entry_id":"reload_credential","args":{}}'

# 3) 单指令调试
curl -s -X POST http://127.0.0.1:48916/runs -H "Content-Type: application/json" \
  -d '{"plugin_id":"mijia","entry_id":"query_device_state","args":{"device":"主卧空调"}}'
curl -s http://127.0.0.1:48916/runs/<run_id>
```

`smart_control` 是 `@llm_tool`，**不能**用 `/runs` 触发，只能在对话里让 AI 伙伴调用。

日志：`%LOCALAPPDATA%\N.E.K.O\logs\plugin\`（按插件名+日期分文件）。

### 5.5 回归基线

`tests/test_trace_baseline.py` 用 `commands_execution_trace.json` 钉住 192 条真实中文指令的解析/路由结果。改 `nlp/` 后若基线漂移，先确认是**有意的行为变化**再更新基线，不要直接重刷。

### 5.6 本地一次性调试脚本（不进仓库）

三个脚本放在 **`E:\n.e.k.o_plugin_mijia_local\`**，与仓库平行、不在其内部。它们内部用绝对路径 `E:\n.e.k.o_plugin_mijia`，所以放在哪儿都能跑，不受工作目录影响。

```bash
cd /e/n.e.k.o_plugin_mijia_local
PY=/c/Users/25672/AppData/Local/Python/pythoncore-3.11-64/python.exe

$PY _verify_switch18.py         # 单条：switch-18 防查询劫持，打印 branch/did
$PY _debug_compare_trace.py     # 100 条 trace 逐条比对解析结果，列出 mismatch
$PY _semantic_check.py          # 扫 trace 里可疑的 device_hint 抽取
```

| 脚本 | 作用 | 输入 |
|---|---|---|
| `_verify_switch18.py` | 验证 `关闭卧室灯怎么样` 不被查询意图劫持 | 无（内联指令） |
| `_debug_compare_trace.py` | 逻辑层结果 vs trace 期望逐条 diff | `tests/nlp_engine_trace.json` |
| `_semantic_check.py` | 设备名抽取的语义可疑项统计 | `tests/nlp_engine_trace.json` |

后两个读的 `tests/nlp_engine_trace.json` 仍留在仓库 `tests/` 下（已在 `.gitignore`，且是 `.json`，ruff 不会扫）。`_debug_compare_trace.py` 当前报 1 条 mismatch（`switch-18`）属于**已知长尾**，用 `_verify_switch18.py` 单独看实际分支。

---

## 6. 发布流程（Release）

```bash
# 0) 代码已同步、209 测试全绿、precheck 干净

# 1) 发版前严格检查（宿主目录）
cd /e/NEKOdm/N.E.K.O
./.venv/Scripts/python.exe -m plugin.neko_plugin_cli.cli check E:/n.e.k.o_plugin_mijia -r --market-release
#   期望：check --release 通过 + package 构建 + payload_hash 校验 + tests passed

# 2) 提交推送（独立仓库；本机无全局 git 身份，用一次性 env）
cd /e/n.e.k.o_plugin_mijia
GIT_AUTHOR_NAME="wangjunyu200708" GIT_AUTHOR_EMAIL="2567232225@qq.com" \
GIT_COMMITTER_NAME="wangjunyu200708" GIT_COMMITTER_EMAIL="2567232225@qq.com" \
git add -A && git commit -m "..."

# 3) 推送（走本机 7897 代理，沙箱默认代理访问 GitHub 会 502）
git -c http.proxy=http://127.0.0.1:7897 -c https.proxy=http://127.0.0.1:7897 push origin main

# 4) 发 Release（建 tag v<version> + 通知 Market）
./../NEKOdm/N.E.K.O/.venv/Scripts/python.exe -m plugin.neko_plugin_cli.cli publish E:/n.e.k.o_plugin_mijia
#   或分开：publish github <path> / publish market <release-url>
```

**版本纪律**：`plugin.toml` 与 `pyproject.toml` 的 `version` 必须一起改，tag 必须与 `plugin.toml` 版本一致（CI 会校验，不一致直接失败）。破坏性改动升主版本。

**产物**：Release 附件 `mijia.neko-plugin`（zip）、`mijia.market-release-check.txt`、`market-evidence.json`。

**CI**：
- `verify.yml`：push/PR → 官方 `plugin-market-verify.yml` → ruff + 挂载 N.E.K.O + sync + `check -r`
- `release.yml`：`v*` tag → 官方 `plugin-market-release.yml` → 构建 zip + 创建 Release
- 排查：GitHub → Actions → 第一个失败 step → 本地用 `precheck` 复现

---

## 7. 常见坑 / 已知限制

| 坑 | 说明 |
|---|---|
| 本地 lint 与 CI 结果不一致 | `lint` 用 `--no-respect-gitignore` 扫本地全目录，CI 只 checkout 已跟踪文件。调试脚本已搬到仓库外 `E:\n.e.k.o_plugin_mijia_local\`，勿再放回 `tests/` |
| `/miotspec/action` 死端点 | 部分网关 action 调用全线 -6，动作类控制（扫地机 start/pause）会失败；属性通道 `/miotspec/prop/set` 正常 |
| 电视云端无电源 | `Turn On` 在 BLE 服务（siid 6），云端不可用；`Turn Off` 依赖已死的 action 端点。开关走 2 级链 |
| 只写属性无法回读 | access="write" 的开关属性读不回来 → 返回"已发送但无法确认"（Err），绝不假报成功 |
| 缓存 schema 版本 | `_DEVICES_CACHE_SCHEMA=2`；部分刷新**必须沿用旧版本号**写入，否则冻结损坏缓存 |
| 跨账号缓存 | home_id/别名/场景缓存复用必须严格 `cache_user_id == current_user_id` |
| async 阻塞 | event loop 内禁止同步文件 IO/HTTP/sleep；读缓存用 `read_json_async()`，顶层 `isinstance(cached, dict)` 防非 dict |
| 中文歧义 | `灯亮度调高一点` 会解析成温度（`度` 先于 `亮` 匹配）。**故意不修**：语义理解归 LLM 前端，规则层冻结 |
| 多指令 | 无 Splitter，`打开客厅灯，关闭卧室灯` 不支持（上层 LLM 负责拆句） |
| 归一化边界 | normalizer 只剥前缀（把/请/帮我），不插入空格、不改实体内部 |
| 匹配歧义 | 设备名以动作动词/模式词开头（`烘干机温度50度`）有 offset-0 跳过防护，改词表时注意 |
| 工具面混用 | 同一能力不能既是 `@llm_tool` 又是 `@plugin_entry` |
| git 身份 | 本机无全局 git 身份，提交必须用一次性 env，禁止 `git config` |
| GitHub 网络 | 沙箱代理访问 GitHub 返回 502，必须显式 `-c http.proxy=http://127.0.0.1:7897` |

---

## 8. 关键文件速查

| 想改什么 | 打开哪个文件 |
|---|---|
| 自然语言解析规则 | `nlp/control_parser.py`（分界/意图） |
| 设备匹配逻辑 | `nlp/device_matcher.py` |
| 场景别名 | `nlp/intent_terms.py` |
| 动作动词 | `nlp/action_verbs.py` |
| 数值/单位/相对量 | `nlp/value_resolver.py` |
| 前缀归一化 | `nlp/normalizer.py` |
| 中文数字 | `nlp/chinese_number.py` |
| 多控开关方位 | `nlp/positions.py` |
| 能力校验 / CommandIR | `nlp/capability.py` |
| 路由分发 | `nlp/router.py` |
| 执行分支/缓存/凭据/错误信封 | `__init__.py` |
| 静态表（名称/单位/属性关键词） | `property_tables.py` |
| 米家 API 客户端 | `mijia_api/`（infrastructure→repositories→services，纯异步） |
| 文案/多语言 | `i18n/*.json`（8 语言 × 171 key） |
| 单元测试 | `tests/test_*.py`（209 用例） |
| 回归基线 | `tests/commands_execution_trace.json`（192 条指令，**要入库**） |
