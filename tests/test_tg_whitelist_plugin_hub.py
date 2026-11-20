"""
插件中心 ↔ TgtoDrive 联动完善回归（v2.7，版本号不变）
==========================================================
覆盖四项（全部纯标准库，独立桩，不依赖 pytest / fastapi / 网络）：

1) 无 TMDB ID 的幂等键兜底：kw_tg_<标题slug>
   - _rule_id 有 id 时行为与旧版逐字一致 kw_<tmdb>_<season>
   - 无 id（0/None/非法）时用标题 slug；标题规范化稳定、空标题兜底
   - build_rule 无 tmdb_id 时字段写空串、标题兜底，不抛异常
   - remove_rules / merge_and_save 复用同一 _rule_id（slug 与 kw_ 都能走）

2) 审核态/同步态解耦：插件侧独立同步台账
   - SYNC_STATES / SYNC_LEDGER_MAX 常量存在
   - _record_sync_ledger 只收合法 state、淘汰最旧 1/5
   - _set_last_sync 结构化回执带 applied/conflict/verified
   - get_sync_states 只读快照；状态文件持久化 last_sync/sync_ledger 且向后兼容

3) 管理端诊断贯通
   - /status 返回 last_sync；新增 /sync_states 路由
   - plugins.html 渲染 applied/conflict/verified 诊断块
   - requests_admin.html 合并台账并按规则 id 展示同步态徽标

4) 零回归约束
   - 插件版本仍 2.2.0、APP 版本点 NOT touched（由 verify_patches 覆盖）
   - source 字段仍 "tmdb"（TgtoDrive 契约字段，不照搬 ya-tg）
"""

import sys
import types
import importlib.util
import os as _os

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {msg}")
    else:
        FAIL += 1
        print(f"  ✗ {msg}")


def _make(name):
    m = types.ModuleType(name)
    sys.modules[name] = m
    return m


# --- 依赖桩（与 test_tg_whitelist_meta_verify.py 同款） ---
_make("fastapi")
fastapi = sys.modules["fastapi"]
class _Request: pass
fastapi.Request = _Request
class _APIRouter:
    def __init__(self, *a, **k): pass
fastapi.APIRouter = _APIRouter
class _HTTPException(Exception): pass
fastapi.HTTPException = _HTTPException
fastapi.Depends = lambda *a, **k: None

_make("app")
_make("app.domains")
_make("app.domains.media_requests")
app_plugins = _make("app.plugins")
_make("app.infra")
app_infra_clients = _make("app.infra.clients")

plugin_dao = _make("app.plugins.plugin_dao")
plugin_dao.ensure_plugin_tables = lambda: None
plugin_dao.get_plugin_config = lambda pid: {}
plugin_dao.add_plugin_log = lambda *a, **k: None
plugin_dao.list_plugin_logs = lambda *a, **k: []
app_plugins.plugin_dao = plugin_dao

base = _make("app.plugins.base")
class FakeRouter:
    def __init__(self, prefix): self.prefix = prefix; self.routes = []
    def get(self, path):
        def d(f): self.routes.append(("GET", path, f)); return f
        return d
    def post(self, path):
        def d(f): self.routes.append(("POST", path, f)); return f
        return d
class PluginBase:
    id = ""; name = ""; description = ""; icon = ""; icon_color = ""
    version = "0.0.0"; author = ""; pro_only = False; permissions = []
    def __init__(self):
        self.router = FakeRouter(f"/api/plugins/{self.id}")
        self._enabled = False
        self._config = {}
    def _init_logs_table(self): pass
    def _load_config_to_cache(self): pass
    def _get_config(self): return dict(self._config)
    def set_config(self, cfg): self._config = cfg
    @property
    def enabled(self): return self._enabled
    def on_enable(self): pass
    def on_disable(self): pass
    def log(self, *a, **k): pass
    def get_meta(self): return {}
base.PluginBase = PluginBase

mrd = _make("app.domains.media_requests.media_request_dao")
mrd.list_all_requests = lambda: []
mrd.update_media_request_status = lambda *a, **k: None
mrd.get_media_request = lambda *a, **k: None
sys.modules["app.domains.media_requests"].media_request_dao = mrd

msc = _make("app.infra.clients.media_server_client")
msc.media_api = None
app_infra_clients.media_server_client = msc

tc_mod = _make("app.infra.clients.tmdb_client")
tc_mod.tmdb_client = None
app_infra_clients.tmdb_client = tc_mod

fake_requests = types.ModuleType("requests")
class _RequestException(Exception): pass
fake_requests.RequestException = _RequestException
sys.modules["requests"] = fake_requests
fake_requests.get = lambda *a, **k: None
fake_requests.post = lambda *a, **k: None

_REPO_ROOT = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
_PLUGIN_PATH = _os.path.join(_REPO_ROOT, "app", "plugins", "tg_whitelist_sync", "plugin.py")
SPEC = importlib.util.spec_from_file_location("tg_hub_test", _PLUGIN_PATH)
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)

C = mod.TgtoDriveClient


# ============================================================
print("\n== 1. 无 TMDB ID 的幂等键兜底 ==")
check(C._rule_id(901, 1) == "kw_901_1", "有 TMDB ID 时 id 与旧版逐字一致 kw_901_1")
check(C._rule_id(1507696, 0) == "kw_1507696_0", "movie season=0 id 与旧版一致")
check(C._rule_id("42", 2) == "kw_42_2", "字符串型 tmdb_id 仍能转 int")
slug = C._rule_id(0, 0, "赛博朋克 边缘行者")
check(slug.startswith("kw_tg_") and "赛博朋克" in slug, f"无 TMDB ID 用标题 slug 兜底: {slug}")
check(C._rule_id(None, 0, "Some Movie") == C._rule_id(0, 0, "Some Movie"),
      "None 与 0 的无 id 规则生成相同 slug（幂等）")
check(C._rule_id(0, 0, "A/B: Test!!") == C._rule_id(0, 0, "a-b-test"),
      "slug 规范化稳定（小写/标点折叠）")
check(C._rule_id(0, 0, "") == "kw_tg_untitled", "空标题兜底 untitled")
check(len(C._rule_id(0, 0, "标" * 100)) <= len("kw_tg_") + 40,
      "slug 截断到 40 字符内")
# build_rule 无 tmdb_id 不抛异常、字段写空串
item = {"tmdb_id": 0, "season": 0, "title": "无 ID 剧集", "keyword": "无 ID 剧集",
        "rule_fields": {"resolution": "4k", "effect": "all", "required": "265"}}
r = C("http://tg", "tok", "/api/visual-filter/config", "ENV_FILTER_GUANGYA").build_rule(item)
check(r.get("tmdb_id") == "", "build_rule 无 tmdb_id 时字段写空串（不写 None/0）")
check(r.get("id", "").startswith("kw_tg_"), "build_rule 无 tmdb_id 时 id 用 slug")
check(r.get("title") == "无 ID 剧集", "build_rule 标题正常")
check(r.get("source") == "tmdb", "source 仍为 TgtoDrive 契约字段 tmdb（未照搬 ya-tg）")


# ============================================================
print("\n== 2. 独立同步态台账（审核态/同步态解耦） ==")
# 用一个隔离的临时状态文件初始化，避免读到仓库根目录真实运行态
_ISOLATED_STATE = _os.path.join(_os.path.dirname(_PLUGIN_PATH), "_test_sync_state.json")
mod.TgWhitelistSyncPlugin._resolve_state_file = lambda self: _ISOLATED_STATE
inst = mod.TgWhitelistSyncPlugin()
check(hasattr(inst, "SYNC_STATES") and set(inst.SYNC_STATES) == {"synced", "failed", "conflict", "skipped", "removed"},
      "SYNC_STATES 五种同步态齐全")
check(inst.SYNC_LEDGER_MAX == 500, "台账上限 500")
check(inst._last_sync is None and inst._sync_ledger == {}, "初始台账为空")

# _record_sync_ledger 只收合法 state
inst._sync_ledger = {}
inst._record_sync_ledger([
    {"key": "kw_1_0", "state": "synced"},
    {"key": "kw_2_0", "state": "bogus"},   # 非法，忽略
    {"key": "", "state": "failed"},        # 无 key，忽略
])
check(set(inst._sync_ledger.keys()) == {"kw_1_0"}, "台账忽略非法 state 与空 key")
check(inst._sync_ledger["kw_1_0"]["state"] == "synced" and "at" in inst._sync_ledger["kw_1_0"],
      "台账记录 state + at")

# 淘汰最旧 1/5
inst._sync_ledger = {}
for i in range(600):
    inst._sync_ledger[f"kw_{i}_0"] = {"state": "synced", "at": f"2026-11-20 00:{i // 60:02d}:{i % 60:02d}"}
inst._record_sync_ledger([{"key": "kw_new_0", "state": "synced", "at": "x"}])
check(len(inst._sync_ledger) <= 500, f"超限时淘汰最旧 1/5，回落到上限内: {len(inst._sync_ledger)}")
check("kw_new_0" in inst._sync_ledger, "淘汰不影响最新写入的记录")

# _set_last_sync 结构化回执
diag = inst._set_last_sync("push", {
    "ok": True, "detail": "已追加 2 条", "added": ["kw_1_0"], "skipped": [],
    "applied": [{"id": "kw_1_0", "resolution": "4k"}],
    "conflicted": [{"id": "kw_9_0", "source": "manual"}],
    "verified": True, "verify_detail": "回读校验通过",
})
check(diag["kind"] == "push" and diag["ok"] is True, "last_sync 含 kind/ok")
check(diag["applied"][0]["id"] == "kw_1_0", "last_sync 带 applied 参数快照")
check(diag["conflicted"][0]["id"] == "kw_9_0", "last_sync 带 conflict 诊断")
check(diag["verified"] is True, "last_sync 带 verified 回读结果")

snap = inst.get_sync_states()
check(snap["last_sync"] is diag and isinstance(snap["ledger"], dict), "get_sync_states 返回只读快照")
check(snap["ledger"] is not inst._sync_ledger, "get_sync_states ledger 是副本（不暴露内部引用）")

# 状态文件持久化 + 向后兼容
inst._last_sync = diag
inst._sync_ledger = {"kw_1_0": {"state": "synced", "at": "2026-11-20 00:00:00"}}
state_path = _ISOLATED_STATE
inst._state_file = state_path
inst._save_state()
with open(state_path, encoding="utf-8") as f:
    import json
    saved = json.load(f)
check(saved.get("last_sync", {}).get("kind") == "push", "状态文件写入 last_sync")
check(saved.get("sync_ledger", {}).get("kw_1_0", {}).get("state") == "synced", "状态文件写入 sync_ledger")
inst2 = mod.TgWhitelistSyncPlugin()
inst2._state_file = state_path
with open(state_path, "w", encoding="utf-8") as f:
    json.dump({"last_run": "x", "last_result": "y", "stats": {}}, f)
inst2._load_state()
check(inst2._last_sync is None and inst2._sync_ledger == {}, "老状态文件无新字段时向后兼容（不报错）")
_os.remove(state_path)


# ============================================================
print("\n== 3. 管理端诊断贯通 + 路由 ==")
routes = [(m, p) for m, p, _ in inst.router.routes]
check(("GET", "/status") in routes, "status 路由已注册")
check(("GET", "/sync_states") in routes, "sync_states 路由已注册")
plugins_html = open(_os.path.join(_REPO_ROOT, "templates", "plugins.html"), encoding="utf-8").read()
check("renderTgWhitelistDiag" in plugins_html, "plugins.html 渲染诊断回执函数存在")
check("tgwhitelist-sync-diag" in plugins_html, "plugins.html 诊断展示容器存在")
check("最近一次写入诊断" in plugins_html, "plugins.html 诊断区块标题存在")
check("renderTgWhitelistDiag(d.last_sync)" in plugins_html, "status 加载后调用诊断渲染")
req_html = open(_os.path.join(_REPO_ROOT, "templates", "requests_admin.html"), encoding="utf-8").read()
check("attachTgSyncState" in req_html, "工单列表合并同步态函数存在")
check("/api/plugins/tg_whitelist_sync/sync_states" in req_html, "工单列表拉取 sync_states 接口")
check("item.tg_sync" in req_html, "工单列表展示独立同步态徽标")
check("'kw_' + (r.tmdb_id || 0) + '_' + (r.season || 0)" in req_html, "工单按 kw_<tmdb>_<season> 关联台账")


# ============================================================
print("\n== 4. 零回归约束 ==")
src = open(_PLUGIN_PATH, encoding="utf-8").read()
check('version = "2.2.0"' in src, "插件内部版本仍 2.2.0（不动 CI 断言）")
check('"source": "tmdb"' in src, "规则 source 仍 tmdb（TgtoDrive 契约，verify_patches 断言）")
check("kw_ya-tg" not in src, "未照搬 ya-tg 来源标记（语义不同）")

print(f"\n==== 结果：通过 {PASS} / 失败 {FAIL} ====")
sys.exit(1 if FAIL else 0)
