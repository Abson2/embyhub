# TgtoDrive 联动 · 真机验证清单

> 用途：资料库《TgtoDrive联动复用说明》（最新复用版，Ya-TG v0.9.20 整改完成版，2026-11-20）第十节如实标注了 4 项「沙箱连不上用户 TgtoDrive，未实测，勿标 ✅」的待验证项。本清单把它们转成可在你自己的 NAS / TgtoDrive 环境逐条执行的验证步骤与判定标准。
>
> **重要边界**：TgtoDrive 是独立外部系统，embyhub 只通过它暴露的 OpenAPI 交互（`GET/POST /api/visual-filter/config`），不集成源码、不部署容器、不读写其数据库。以下验证全部在**真实 TgtoDrive** 上观察结果，不在 embyhub 沙箱内模拟。
>
> 版本背景：本清单对应 embyhub `2.7`（含 2.7 插件中心联动完善补丁），插件内部版本仍 `2.2.0`。执行前请确认已重新 pull `2.7` 镜像（mutable tag 覆盖不会自动更新已运行容器）。

---

## 前置准备（一次性）

1. 插件中心 → 「求片同步 TgtoDrive 白名单」→ 配置：
   - `tgtdrive_base_url`：TgtoDrive 服务根地址（末尾不带 `/`）。
   - `tgtdrive_token`：在「系统工具 → 实用工具 → OpenAPI」创建的 Bearer 令牌。
   - `tgtdrive_scene`：默认 `ENV_FILTER_GUANGYA`（光鸭频道监控场景）。
   - `rule_resolution` / `rule_effect` / `rule_required_keyword`：按需求设置（默认 `4k` / `all` / `265`）。
2. 打开插件中心 → 「求片 → TgtoDrive 同步」面板，先点 **测试连接**：
   - 判定：返回「可达，场景 ENV_FILTER_GUANGYA 当前 N 条规则」，且 N 与 TgtoDrive 页面当前白条数一致。
   - 若提示鉴权失败 → 检查 Bearer 令牌；若连通但读不到规则 → 检查 `tgtdrive_scene` 是否拼错。

---

## 验证项 1：全量回写的字段兼容性与回读一致性

> 对应经验文档第十节：`POST /api/visual-filter/config` 全量回写的真机字段兼容性与回读一致性。

**步骤**

1. 在 TgtoDrive 页面手动添加 1 条用户规则（例如关键词 `MANUAL_KEEP_TEST`），记下当前规则总数 `N`。
2. 在 embyhub 工单大厅审批通过 1 条求片，触发白名单推送。
3. 回到 TgtoDrive 白名单页刷新，观察：
   - 手动规则 `MANUAL_KEEP_TEST` 是否仍在；
   - 新求片规则是否出现；
   - 规则总数是否为 `N + 1`（未被清空/未翻倍）。
4. 打开插件中心同步面板 → 「最近一次写入诊断」，查看 `verified` 字段。

**判定**

- ✅ 通过：手动规则保留、新规则出现、总数正确，诊断显示「回读通过」。
- ❌ 不通过：任一用户规则消失、总数骤减、或诊断显示「回读未通过」并给出具体 id → 记录 `verify_detail`，检查 `write_mode`（flat/envelope）是否与真机接口匹配，切换后重试。

---

## 验证项 2：元数据补全后前端从「正在匹配 TMDB」变为「已匹配」

> 对应经验文档第十节：规则元数据补全后，前端是否确实从"正在匹配 TMDB"变为"已匹配"。这是 2.7 的核心修复点。

**步骤**

1. 确认 embyhub 已配置 TMDB API Key（系统设置 → TMDB）。
2. 审批通过 1 条带 TMDB ID 的求片，等待推送完成。
3. 在 TgtoDrive 白名单页找到该条新规则，查看其展示状态。
4. 展开规则详情，核对 `poster_url`（应为 TMDB 图片 CDN 的 `w500` 海报完整地址）、`backdrop_url`（`w780`）、`overview`、`original_title` 是否有值。

**判定**

- ✅ 通过：前端显示「已匹配」，海报/背景/简介有值。
- ❌ 不通过（仍「正在匹配 TMDB」）：
  - 先看同步面板 `applied` 是否带了该规则（确认真的写入）；
  - 若 `applied` 有但元数据空 → 检查 TMDB Key 是否有效、网络是否能访问 TMDB；
  - 若为**存量规则**（v2.7 之前写入的）→ 属预期：存量规则需**重新采纳一次**（删除工单再重推，或 TgtoDrive 后台补跑匹配）才会补上元数据，不是 bug；
  - 可关闭 `tmdb_meta_enabled` 回退旧行为做对照。

---

## 验证项 3：分辨率 / 效果 / 必含词 / 年份在真机编译进正则后的过滤效果

> 对应经验文档第十节：分辨率/效果/必含词/年份在真机编译进正则后的实际过滤效果。

**步骤**

1. 配置一组筛选条件，例如 `rule_resolution=4k`、`rule_effect=dolby`、`rule_required_keyword=265`。
2. 审批通过求片，推送后打开插件同步面板「最近一次写入诊断」，核对 `applied` 里该规则的四个真实值：
   - `resolution`、`effect`、`required_keyword`、`year`（及 `include_year`）。
3. 在 TgtoDrive 白名单页查看该规则编译出的 `raw_regex` / `meta_raw_regex`，确认上述条件都被编进正则。
4. 用一条**不满足**条件的资源文件名做频道监控命中测试，确认不入库；再用一条**满足**的，确认入库。

**判定**

- ✅ 通过：`applied` 四值与配置一致，正则含对应条件，命中/不命中行为符合预期。
- ❌ 不通过：
  - `applied` 显示值正确但正则缺失 → TgtoDrive 后端规范化问题，记录规则 id 与编译后正则；
  - `applied` 里 `required_keyword` 为空或 `include_required_keyword=false` → 检查必含词配置是否留空（留空会显式写空串关闭开关，属设计行为）；
  - 命令式求片缺年份 → 确认 `_tmdb_meta` 是否从 `release_date`/`first_air_date` 补了 year（2.7 补丁已实现）。

---

## 验证项 4：TgtoDrive 白名单真机写入（矩阵 V64/V65）

> 对应经验文档第十节：TgtoDrive 白名单真机写入（原矩阵 V64/V65 标 🟡）。

**步骤**

1. 审批通过求片 → 观察白名单是否新增对应 `kw_<tmdb>_<season>` 规则。
2. 手动接单（`manual`）另一条 → 观察是否同样写入，且工单流转到 status=1（自动下载中）。
3. 删除已同步的工单 → 观察对应 `kw_` 规则是否被移除，且**其它规则完整保留**。
4. 拒绝（`reject`）一条未同步工单 → 观察**不写**白名单。
5. 重复审批同一条 → 观察幂等（不重复新增规则）。
6. 工单大厅列表刷新，查看每条的独立同步态徽标（已入白名单 / 同步失败 / 规则冲突 / 已跳过 / 已移除）。

**判定**

- ✅ 通过：写入/移除/跳过/幂等行为符合上表，删单只删目标 `kw_` 规则，同步态徽标与白名单实际状态一致。
- ❌ 不通过：
  - 删单误删用户规则 → **严重**，立即停止并反馈（护栏应保证只删 `kw_` 前缀）；
  - 徽标显示「同步失败」→ 点开同步面板看 `last_sync.detail` 与 `conflicted`；
  - 幂等失效（重复写入）→ 核对规则 id 是否为 `kw_<tmdb>_<season>`，无 TMDB ID 时应为 `kw_tg_<slug>`。

---

## 验证结果记录模板

| 验证项 | 日期 | TgtoDrive 版本 | 结果(✅/❌) | 现象 / 证据 | 处理 |
|---|---|---|---|---|---|
| 1 全量回写 | | | | | |
| 2 元数据匹配 | | | | | |
| 3 正则过滤 | | | | | |
| 4 白名单写入 | | | | | |

> 若任一项 ❌，请把「现象 / 证据」连同插件同步面板的诊断回执（`last_sync` 的 `applied`/`conflicted`/`verify_detail`）一起反馈，便于定位是 embyhub 写入问题还是 TgtoDrive 真机兼容问题。
