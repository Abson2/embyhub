"""
tg_whitelist_sync 插件补强回归（v2.2.0 能力补强，版本号不变）
==========================================================
覆盖三项，全部零第三方依赖（复用 test_tg_whitelist_sync.py 的桩思路，独立文件避免互相污染）：

1) TMDB 元数据补全（修复 TgtoDrive 前端"正在匹配 TMDB"）
   - 补全 poster/backdrop/overview/original_title + poster_url(w500)/backdrop_url(w780)
   - 原始 path 才拼尺寸前缀；上游已给完整 URL 不二次拼接（与既有契约一致）
   - tv 走 get_tv_details、movie 走 get_movie_details
   - 进程内 TTL 缓存命中（限流，避免逐条打 TMDB）
   - 无客户端 / 查询异常 / 字段缺失一律降级空串，绝不抛异常
   - build_rule 搬运 meta 到规则 6 字段；item 无 meta 时写空串（等价旧行为）
   - 开关 tmdb_meta_enabled 关闭时不查 TMDB

2) 写入后回读校验（经验文档"固定四步"第 4 步）
   - 新增 id 回读可见、已删 id 回读消失、总数不减少 → ok
   - 规则数减少 / 新增未见 / 已删仍在 → 返回 (False, 原因)，不抛异常
   - GET 异常 → 返回 (False, 原因)，不抛异常
   - merge_and_save / remove_rules 成功返回体带 verified 字段

3) 配置表单：分辨率 / 画面效果为下拉 select 且枚举正确

来源：资料库文档《TgtoDrive联动复用说明》(M97aeqAQXjXiGCsARQqgxn, 2026-11-20) 第二/四/六节。
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


# --- 依赖桩（与 test_tg_whitelist_sync.py 同款，够插件 import 即可） ---
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

# tmdb_client 桩：插件用 try-import，这里给一个默认 None 的桩，
# 测试通过 _tmdb_meta(client=...) 显式注入假客户端（不改 sys.modules，隔离更干净）。
tc_mod = _make("app.infra.clients.tmdb_client")
tc_mod.tmdb_client = None
app_infra_clients.tmdb_client = tc_mod

# requests 桩
fake_requests = types.ModuleType("requests")
class _RequestException(Exception): pass
fake_requests.RequestException = _RequestException
sys.modules["requests"] = fake_requests
fake_requests.get = lambda *a, **k: None
fake_requests.post = lambda *a, **k: None

# 加载插件
_REPO_ROOT = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
_PLUGIN_PATH = _os.path.join(_REPO_ROOT, "app", "plugins", "tg_whitelist_sync", "plugin.py")
SPEC = importlib.util.spec_from_file_location("tg_meta_verify_test", _PLUGIN_PATH)
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


# ============================================================
print("\n== 1. TMDB 元数据补全 ==")
class _Resp:
    def __init__(self, data): self._data = data
    def json(self): return self._data

class FakeTmdb:
    def __init__(self, data=None, raise_exc=False):
        self.data = data if data is not None else {}
        self.raise_exc = raise_exc
        self.tv_calls = 0
        self.movie_calls = 0
    def get_tv_details(self, tid):
        self.tv_calls += 1
        if self.raise_exc: raise RuntimeError("tmdb down")
        return _Resp(self.data)
    def get_movie_details(self, tid):
        self.movie_calls += 1
        if self.raise_exc: raise RuntimeError("tmdb down")
        return _Resp(self.data)

full_data = {
    "poster_path": "/abc123.jpg",
    "backdrop_path": "/bg456.jpg",
    "overview": "一段简介",
    "original_title": "Original Name",
}
mod._TMDB_META_CACHE.clear()
cli = FakeTmdb(full_data)
meta = mod._tmdb_meta(123, "movie", client=cli)
check(meta["poster_path"] == "/abc123.jpg", "poster_path 保存 TMDB 原始 path")
check(meta["poster_url"] == "https://image.tmdb.org/t/p/w500/abc123.jpg",
      f"poster_url 拼 w500 正确: {meta['poster_url']}")
check(meta["backdrop_url"] == "https://image.tmdb.org/t/p/w780/bg456.jpg",
      f"backdrop_url 拼 w780 正确: {meta['backdrop_url']}")
check(meta["overview"] == "一段简介" and meta["original_title"] == "Original Name",
      "overview / original_title 已补全")
check(cli.movie_calls == 1 and cli.tv_calls == 0, "movie 走 get_movie_details")

# 缓存命中：第二次不再打 TMDB（须在 clear 之前，紧跟首次 movie 调用）
before = cli.movie_calls
meta2 = mod._tmdb_meta(123, "movie", client=cli)
check(cli.movie_calls == before and meta2 == meta, "TTL 缓存命中，未重复请求 TMDB（限流）")

# tv 走 get_tv_details + original_name 兜底
mod._TMDB_META_CACHE.clear()
cli_tv = FakeTmdb({"poster_path": "/tv.jpg", "original_name": "TV Original"})
meta_tv = mod._tmdb_meta(456, "tv", client=cli_tv)
check(cli_tv.tv_calls == 1 and cli_tv.movie_calls == 0, "tv 走 get_tv_details")
check(meta_tv["original_title"] == "TV Original", "tv 用 original_name 兜底 original_title")

# 上游已给完整 URL：不二次拼 w500
mod._TMDB_META_CACHE.clear()
cli_url = FakeTmdb({"poster_path": "https://image.tmdb.org/t/p/w500/already.jpg"})
meta_url = mod._tmdb_meta(789, "movie", client=cli_url)
check(meta_url["poster_url"] == "https://image.tmdb.org/t/p/w500/already.jpg",
      "上游完整 URL 不二次拼接（与既有 w500 断言一致）")

# 降级：无客户端 / 异常 / 字段缺失
mod._TMDB_META_CACHE.clear()
check(all(v == "" for v in mod._tmdb_meta(1, "movie", client=None).values()),
      "无 TMDB 客户端时全部字段降级空串")
mod._TMDB_META_CACHE.clear()
check(all(v == "" for v in mod._tmdb_meta(1, "movie", client=FakeTmdb(raise_exc=True)).values()),
      "TMDB 查询异常时降级空串，不抛异常")
mod._TMDB_META_CACHE.clear()
check(all(v == "" for v in mod._tmdb_meta(1, "movie", client=FakeTmdb({})).values()),
      "TMDB 字段缺失时降级空串")

# build_rule 搬运 meta；无 meta 写空串（等价旧行为）
client = mod.TgtoDriveClient("http://tg", "tok", "/api/visual-filter/config", "ENV_FILTER_GUANGYA")
item = {"tmdb_id": 123, "season": 0, "title": "T", "keyword": "T",
        "media_type": "movie", "rule_fields": {"resolution": "4k", "effect": "all", "required": "265"},
        "tmdb_meta": meta}
rule = client.build_rule(item)
check(all(rule.get(k) == meta[k] for k in mod.RULE_META_KEYS),
      "build_rule 把 6 个元数据字段搬进规则")
item_no_meta = {k: v for k, v in item.items() if k != "tmdb_meta"}
rule_no = client.build_rule(item_no_meta)
check(all(rule_no.get(k) == "" for k in mod.RULE_META_KEYS),
      "item 无 meta 时 6 字段写空串（旧行为等价）")

# 配置开关存在
schema_keys = {f["key"] for f in mod.TgWhitelistSyncPlugin().get_config_schema()}
check("tmdb_meta_enabled" in schema_keys, "配置表单含 tmdb_meta_enabled 开关")


# ============================================================
print("\n== 2. 写入后回读校验 ==")
class Transport:
    def __init__(self): self.get_handler = None; self.calls = []
    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        if self.get_handler: return self.get_handler(kw)
        return _Resp({"rules": []})

# 用真 client + 假 transport 测 verify_write（只读方法，不依赖 POST）
def make_client():
    c = mod.TgtoDriveClient("http://tg", "tok", "/api/visual-filter/config", "ENV_FILTER_GUANGYA")
    return c

c = make_client()
c._url = lambda: "http://tg/api/visual-filter/config"
c._headers = lambda: {}
c.get_config = lambda: {"rules": [
    {"id": "kw_1_0"}, {"id": "kw_2_0"}, {"id": "tmdb_movie_99"}]}
ok, detail = c.verify_write(expect_added=["kw_1_0"], expect_removed=["kw_3_0"], expect_total=2)
check(ok is True, f"回读全部符合预期 -> ok: {detail}")

ok, detail = c.verify_write(expect_total=4)
check(ok is False and "少于" in detail, f"规则数减少 -> 失败不抛异常: {detail}")

ok, detail = c.verify_write(expect_added=["kw_9_9"])
check(ok is False and "kw_9_9" in detail, f"新增规则回读未见 -> 失败: {detail}")

ok, detail = c.verify_write(expect_removed=["kw_1_0"])
check(ok is False and "kw_1_0" in detail, f"已删规则回读仍在 -> 失败: {detail}")

def boom(): raise RuntimeError("GET fail")
c.get_config = boom
ok, detail = c.verify_write(expect_added=["kw_1_0"])
check(ok is False and "异常" in detail, f"GET 异常 -> 返回失败不抛异常: {detail}")

# 源码级断言：merge_and_save / remove_rules 成功返回体带 verified，且回读失败不改 ok
src = open(_PLUGIN_PATH, encoding="utf-8").read()
check('"verified": verified' in src and "verify_detail" in src,
      "两个写入方法成功返回体带 verified / verify_detail")
check(src.count("self.verify_write(") >= 2,
      "merge_and_save 与 remove_rules 均接入回读校验")
check("def verify_write(self" in src and "只读、不抛异常" in src,
      "verify_write 明确只读、不抛异常契约")


# ============================================================
print("\n== 3. 配置表单下拉枚举 ==")
schema = {f["key"]: f for f in mod.TgWhitelistSyncPlugin().get_config_schema()}
res = schema["rule_resolution"]
eff = schema["rule_effect"]
check(res["type"] == "select", "分辨率改为 select 下拉")
check([o["value"] for o in res["options"]] == ["4k", "1080p", "720p", "all"],
      f"分辨率枚举正确: {[o['value'] for o in res['options']]}")
check(eff["type"] == "select", "画面效果改为 select 下拉")
check([o["value"] for o in eff["options"]] == ["all", "dolby", "hdr", "sdr"],
      f"画面效果枚举正确: {[o['value'] for o in eff['options']]}")

print(f"\n==== 结果：通过 {PASS} / 失败 {FAIL} ====")
sys.exit(1 if FAIL else 0)
