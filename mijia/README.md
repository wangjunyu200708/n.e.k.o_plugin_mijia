# 米家智能家居插件 (Mijia Plugin)

[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)

基于小米 MiOT 协议，通过 **N.E.K.O AI 伙伴**用**自然语言**控制米家智能设备。无需记忆设备 ID 或技术参数，像聊天一样下达指令即可完成开关灯、调空调、切换模式、查状态、执行场景。

## 功能概览

| 能力 | 说明 |
|------|------|
| 账号登录 | 扫码登录小米账号，凭据本地安全存储（0600 文件权限，Windows icacls） |
| 设备发现 | 获取家庭下全部设备，自动缓存规格（properties / actions，含 value_list 描述） |
| 智能控制 | 一句话开关 / 调属性 / 切模式 / 相对调整 / 极值 / 颜色 / 动作动词 / 场景 |
| 状态查询 | 按名称批量读取设备所有可读属性，中文名 + 单位本地化 |
| 别名 | `set_device_alias` 给设备设自定义别名，语音控制更方便 |
| 场景联动 | 执行米家 App 中预设的智能场景（`执行回家模式`） |

## 代码结构

```
mijia/
├── __init__.py        # MijiaPlugin：入口 + 执行分支 + 缓存/凭据管理
├── plugin.toml        # 插件清单（id=mijia，Hosted UI dashboard + 快速开始）
├── nlp/               # 自然语言规则引擎（纯函数，无 IO/API 依赖）
│   ├── router.py          # 五步短路：场景→开关标记→查询→动作→属性控制
│   ├── control_parser.py  # 分界 + 意图解析 → ParseResult
│   ├── device_matcher.py  # 匹配：精确别名>精确名>区域拆分>模糊 → MatchResult
│   ├── intent_terms.py    # 词表（开关/查询/属性/模式/分界动词/颜色）
│   ├── action_verbs.py    # 动作动词 + VERB_TO_ACTION 映射
│   ├── value_resolver.py  # 数值抽取 / 单位推断 / 相对量 / 钳值+步长对齐
│   └── test_*.py          # 33 个单元测试
├── mijia_api/         # 自包含的米家 API 客户端（分层：domain/infrastructure/
│   │                  #   services/repositories，同步 + 异步双实现）
├── i18n/              # 8 语言（en/ja/ko/zh-CN/zh-TW/ru/es/pt）
├── static/            # Hosted UI 控制面板
└── docs/              # 快速开始（8 语言）
```

## 快速开始

1. 把本插件目录放到 N.E.K.O 的插件目录（`plugin/plugins/mijia/`）。
2. 在插件 UI 扫码登录米家账号（凭据存 `data/credential.json`）。
3. 让 AI 伙伴用自然语言控制，例如：

```text
用户：打开卧室灯，把空调调到26度
用户：加湿器切换到睡眠模式
用户：执行回家模式
```

## 入口（plugin_entry）

| 入口 ID | 说明 |
|---------|------|
| `smart_control` | **核心**：自然语言统一控制（开关/属性/模式/动作/场景） |
| `query_device_state` | 按名称查询设备所有可读属性 |
| `list_devices` / `get_cached_devices` | 设备列表（带缓存 + user_id 归属校验） |
| `list_homes` / `list_scenes` | 家庭列表 / 智能场景列表 |
| `set_device_alias` / `get_device_aliases` | 设置 / 读取设备别名 |
| `start_qrcode_login` / `check_login_status` / `logout` | 扫码登录 / 轮询状态 / 登出 |
| `reload_credential` | 重新加载凭据（前端刷新状态前调用） |
| `open_ui` | 打开配置页面 |

## 测试

```bash
# 在 N.E.K.O 仓库内运行（依赖平台 import 链）
uv run pytest plugin/plugins/mijia/nlp -q
```

覆盖：路由五步短路、设备匹配优先级（含房间+部分别名）、数值解析与钳值、模式词前缀分界等，共 33 个用例。

## 已知限制

- 部分第三方网关（如 `api.mijia.tech`）的 `/miotspec/action` 动作端点不可用（全线 `-6`），扫地机 start/pause 等**动作类**控制会回落失败；属性通道（`/miotspec/prop/set`）正常。
- 电视类设备（`xiaomi.tv.rmh1` 等）云端无电源属性，`Turn On` 仅在 BLE 服务——云端开机可能不可用，`Turn Off` 依赖动作端点。
- 只写（access="write"）开关属性读不回来，无法回读确认，会返回"已发送但无法确认"。

## 更新日志

### v2.0.0 (NLP 重构)
- **[REFACTOR]** 2356 行 `__init__.py` 拆出 `nlp/` 子包（router / control_parser / device_matcher / action_verbs / intent_terms / value_resolver），`smart_control` 改为路由分发 + 执行分支。
- **[FEAT]** 开关执行三级回落：电源动作 → 开关属性（bool 过滤）+ 写后回读校验，杜绝"属性合法但非电源"的假成功。
- **[FIX]** spec 解析器保留 value_list 描述（中文模式名映射不再失效）；`validate_value` 兼容 dict 枚举；动作调用校验 per-item 码。
- **[FIX]** 设备缓存 schema 版本化（v2）+ 刷新保留别名/home_id + user_id 归属校验，防跨账号泄露。
- **[TEST]** 新增 33 个单元测试。

### v1.1.0
- 多设备消歧、区域+设备名解析、匹配优先级重构。

### v1.0.0
- 基础设备控制、场景执行、扫码登录。

## 反馈

- **仓库**: https://github.com/wangjunyu200708/n.e.k.o_plugin_mijia
- 遇到无法识别的方言指令欢迎提交 Issue / PR。
