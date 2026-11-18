<p align="center"><img src="icon.png" width="160" alt="米家智能家居插件图标"></p>

# 米家智能家居插件 (Mijia Plugin)

[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)

基于小米 MiOT 协议，通过 **N.E.K.O AI 伙伴**用**自然语言**控制米家智能设备。无需记忆设备 ID 或技术参数，像聊天一样下达指令即可完成开关灯、调空调、切换模式、查状态、执行场景。

## 功能概览

| 能力 | 说明 |
|------|------|
| 账号登录 | 扫码登录小米账号，凭据本地安全存储（0600 文件权限，Windows icacls） |
| 设备发现 | 获取家庭下全部设备，自动缓存规格（properties / actions，含 value_list 描述） |
| 智能控制 | 一句话开关 / 调属性 / 切模式 / 相对调整 / 极值 / 颜色 / 动作动词 / 场景 |
| 多设备联动 | 一句话控多台（`打开客厅灯和卧室灯`、`关掉插座，把客厅灯调到50%`），逐条执行并汇总结果 |
| 状态查询 | 按名称批量读取设备所有可读属性，中文名 + 单位本地化 |
| 别名 | `set_device_alias` 给设备设自定义别名，语音控制更方便 |
| 场景联动 | 执行米家 App 中预设的智能场景（`执行回家模式`） |

## 代码结构

```
./
├── plugin.toml          # 插件清单（id=mijia，Hosted UI dashboard + 快速开始）
├── __init__.py          # MijiaPlugin：入口 + 执行分支 + 缓存/凭据管理
├── property_tables.py   # 静态表：设备名 / 单位 / 属性关键词
├── conftest.py          # 独立仓库测试桩（宿主内自动跳过）
├── nlp/                 # 自然语言规则引擎（纯函数，无 IO/API 依赖）
│   ├── router.py            # 五步短路：场景→开关标记→查询→动作→属性控制
│   ├── control_parser.py    # 分界 + 意图解析 → ParseResult(含 unit)
│   ├── device_matcher.py    # 匹配：精确别名>精确名>区域拆分>模糊 → MatchResult
│   ├── intent_terms.py      # 词表 + detect_scene（口语场景别名）
│   ├── action_verbs.py      # 动作动词 + VERB_TO_ACTION 映射
│   ├── value_resolver.py    # 数值抽取 / 单位推断 / 相对量 / 钳值+步长对齐
│   ├── normalizer.py        # 把/请/帮我 前缀剥离（不碰实体内部文本）
│   ├── chinese_number.py    # 中文数字 → 阿拉伯（两=2、二十三=23）
│   ├── positions.py         # 多控开关方位词 → spec 关键词
│   ├── splitter.py          # 多指令拆分（一句话控多设备）+ 共享动词补全
│   └── capability.py        # 设备能力校验（单位/范围）+ CommandIR
├── mijia_api/           # 自包含的米家 API 客户端（分层：domain/infrastructure/
│                        #   services/repositories，纯异步）
├── i18n/                # 8 语言（en/ja/ko/zh-CN/zh-TW/ru/es/pt）
├── static/              # Hosted UI 控制面板
├── docs/                # 快速开始（8 语言）
├── tests/               # 261 个测试（能力层/路由/匹配/数值/拆分/执行分支/回归基线）
├── .github/workflows/   # verify.yml + release.yml（官方插件仓库工作流）
└── pyproject.toml
```

## 快速开始

1. 把本仓库克隆/解压后，放到 N.E.K.O 的插件目录（`plugin/plugins/mijia/`）。
2. 在插件 UI 扫码登录米家账号（凭据存宿主数据目录下的 `data/credential.json`，即
   Windows 的 `%LOCALAPPDATA%\N.E.K.O\plugins\mijia\data\credential.json`；
   写入时先收紧权限再落盘并原子替换，权限收紧失败会直接报错而不是降级为明文）。
3. 让 AI 伙伴用自然语言控制，例如：

```text
用户：打开卧室灯，把空调调到26度
用户：加湿器切换到睡眠模式
用户：执行回家模式
```

## 对话期工具（`@llm_tool`）

| 工具名 | 说明 |
|--------|------|
| `smart_control` | **核心**：自然语言统一控制（开关/属性/模式/动作/场景）。由 AI 伙伴在对话中直接调用，参数 `{ "command": "打开卧室灯" }`。 |

> 它不是 `@plugin_entry`：宿主会把 `__llm_tool__*` 动态入口从 analyzer 的入口列表里剥离，
> 所以同一条指令不会被 analyzer 和对话 LLM 各执行一次；相应地也不能用 `/runs` +
> `entry_id=smart_control` 触发（控制能力只在对话里用）。

## 入口（plugin_entry）

| 入口 ID | 说明 |
|---------|------|
| `query_device_state` | 按名称查询设备所有可读属性 |
| `list_devices` / `get_cached_devices` | 设备列表（带缓存 + user_id 归属校验） |
| `list_homes` / `list_scenes` | 家庭列表 / 智能场景列表 |
| `set_device_alias` / `get_device_aliases` | 设置 / 读取设备别名 |
| `start_qrcode_login` / `check_login_status` / `logout` | 扫码登录 / 轮询状态 / 登出 |
| `reload_credential` | 重新加载凭据（前端刷新状态前调用） |
| `open_ui` | 打开配置页面 |

## 测试

```bash
python -m pytest tests/ -q                      # 依赖 pytest + pytest-asyncio（见 pyproject.toml dev 组）
python -m plugin.neko_plugin_cli.cli precheck <plugin路径>   # 本地复刻 Market CI 门禁（在 N.E.K.O 目录运行）
```

覆盖：路由五步短路、设备匹配优先级（含房间+部分别名）、数值解析与钳值、模式词前缀分界、能力层单位/范围校验、五个执行分支（含多控开关方位）、资源回收与工具结果信封等，共 **261 个用例**；其中 `tests/test_trace_baseline.py` 用 192 条真实中文指令钉住解析基线，改动 NLP 时防止无声漂移。

## 已知限制

- 部分第三方网关（如 `api.mijia.tech`）的 `/miotspec/action` 动作端点不可用（全线 `-6`），扫地机 start/pause 等**动作类**控制会回落失败；属性通道（`/miotspec/prop/set`）正常。
- 电视类设备（`xiaomi.tv.rmh1` 等）云端无电源属性，`Turn On` 仅在 BLE 服务——云端开机可能不可用，`Turn Off` 依赖动作端点。
- 只写（access="write"）开关属性读不回来，无法回读确认，会返回"已发送但无法确认"。
- 目前仅支持中国大陆区账号：网关与登录端点均为国内地址，海外（如新加坡/欧洲）账号尚无法登录。
- 多指令支持"打开客厅灯和卧室灯"（共享动词）与"关掉插座，把客厅灯调到50%"（各段自带动词）；但**对同一设备连续下多个动作**（"打开客厅灯并调到50%"）仍需分两次调用——后段没有设备名时（"打开客厅灯，然后调到50%"）会整句回退，不做跨段继承。
- 设备名以动作动词/模式词开头等长尾句式仍在持续完善。

## 更新日志

### v3.1.0 (多指令控制 + 凭据落盘加固)

**多指令（一句话控多设备）**
- 新增 `nlp/splitter.py`：按标点（`，` `、` `；`）、连接词（然后 / 接着 / 顺便 / 同时 / 并且 / 还有）与并列连词（和 / 跟 / 与 / 以及）切分，并支持共享动词补全（`打开客厅灯和卧室灯` → 第二段补成 `打开卧室灯`）。
- `smart_control` 先尝试拆分，逐段**串行**执行后汇总：全成功返回 `total` 与逐条 `results`；有失败走 `multi_control_failed` / `multi_control_partial_failure`，`detail.results` 保留每一条的结果，模型能准确说出哪几条已生效。
- 拆分带**预检闸门**：任一段路由不到明确设备就整句回退。设备名含连词（`和风空调`）被切坏时不会误执行半句；192 条回归基线无一被拆分，单指令行为零变化。

**凭据落盘加固**
- 改为 `mkstemp` 建临时文件 → **先收紧权限** → 再写内容 → `fsync` → `os.replace` 原子替换。原实现先按父目录默认权限创建再 chmod，中间存在明文可读窗口，中途失败还会留半截 JSON。
- 权限收紧失败直接抛错，不再静默降级为明文；`icacls` 输出改按字节容错解码，修掉中文 Windows + `PYTHONUTF8` 下的 `UnicodeDecodeError`。

### v3.0.0 (工具面迁移 + 执行层重构 + 错误结构化)
- **[BREAKING]** `smart_control` 由 `@plugin_entry` 改为 `@llm_tool`：只在对话中由 AI 伙伴调用，不再能通过 `/runs` + `entry_id=smart_control` 触发（宿主会把 `__llm_tool__*` 从 analyzer 入口剥离，避免同一条指令被执行两次）。
- **[FEAT]** 执行层引入 `CommandIR` 接缝：路由结果拍平为 IR，`_execute_*` 只吃 IR、不再扫描原话，为后续结构化前端复用同一后端。
- **[FEAT]** 多控开关方位改由前端解析（`nlp/positions.py` → `CommandIR.gang`），后端不再从原话里找"左/右/二键"；缺方位时可按需返回 `gang_ambiguous` + 候选方位。
- **[FEAT]** 工具错误结构化：`is_error` 信封 + 稳定错误码 + `detail`（允许范围 / 期望单位 / 候选设备 / 可选方位），供模型自纠重试，而不是只看到一句散文。
- **[FEAT]** 运行时语言透传：`self.i18n.t(key, locale=self._lang)`，不再恒定回落到 zh-CN。
- **[REFACTOR]** 移除同步 API 半边与死代码（净减约 2.8k 行），`mijia_api` 纯异步。
- **[FIX]** 修复三处资源泄漏（凭据 provider 连接、缓存句柄、后台任务）。
- **[FIX]** 静态表提到模块层（`property_tables.py`），设备规格并行拉取，缓存错误语义修正。
- **[TEST]** 用例 45 → 209：补齐五个执行分支、接缝前置、设备列表并发与缓存语义、资源回收、工具结果契约，以及 192 条真实指令的解析回归基线。

### v2.0.1 (NLP 查询增强)
- **[FIX]** 开关/控制分支剥离尾部询问词后缀（"关闭卧室灯怎么样"→设备名"卧室灯"），修复 device_hint 被询问词污染。
- **[FEAT]** 查询分支新增 `query_prop` 字段：从设备名尾部属性词（"卧室空调温度多少度"→温度）与查询关键词（"多少度/多亮/多冷"）推断具体查询属性，便于服务端直接返回对应属性值。

### v2.0.0 (NLP 引擎重构 + 能力校验)
- **[REFACTOR]** 2356 行 `__init__.py` 拆出 `nlp/` 子包，`smart_control` 改为路由分发 + 执行分支。
- **[FEAT]** 新增 Normalizer（把/请/帮我前缀剥离）、ChineseNumberParser（中文数字）、Capability 层（单位/范围校验，如"亮度调到三档"→INVALID_UNIT）。
- **[FEAT]** 开关执行三级回落：电源动作 → 开关属性（bool 过滤）+ 写后回读校验，杜绝"属性合法但非电源"的假成功。
- **[FEAT]** 场景口语别名（我回家了/晚安/打开回家模式）、句末开关动词、设备名尾部属性词剥离。
- **[FIX]** spec 解析器保留 value_list 描述；`validate_value` 兼容 dict 枚举；动作调用校验 per-item 码；设备缓存 schema 版本化 + user_id 归属校验。

### v1.1.0
- 多设备消歧、区域+设备名解析、匹配优先级重构。

### v1.0.0
- 基础设备控制、场景执行、扫码登录。

## 反馈

- **仓库**: https://github.com/wangjunyu200708/n.e.k.o_plugin_mijia
- 遇到无法识别的方言指令欢迎提交 Issue / PR。
