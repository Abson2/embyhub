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
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code
        self.text = ""
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


# ============================================================
print("\n== 4. 最新经验：缓存层升级（v0.9.20 阶段五 B-07）==")
check(mod._TMDB_META_TTL == 24 * 3600, f"TTL 为 24h: {mod._TMDB_META_TTL}")
check(mod._TMDB_META_MAX == 500, f"缓存硬上限 500: {mod._TMDB_META_MAX}")
check(hasattr(mod, "clear_tmdb_meta_cache"), "提供 clear_tmdb_meta_cache 失效接口")
check(hasattr(mod, "_tmdb_meta_cache_get") and hasattr(mod, "_tmdb_meta_cache_put"),
      "提供 _tmdb_meta_cache_get/put（对齐代码地图）")

# 空结果不缓存：全空 meta 调 put 后缓存仍为空
mod.clear_tmdb_meta_cache()
mod._tmdb_meta_cache_put((900, "movie"), {k: "" for k in mod.RULE_META_KEYS})
check(len(mod._TMDB_META_CACHE) == 0, "全空结果不缓存（避免固化临时抖动）")
# 成功结果缓存
mod._tmdb_meta_cache_put((901, "movie"), {"poster_path": "/x.jpg", "year": "2026"})
check(len(mod._TMDB_META_CACHE) == 1, "成功结果才缓存")
# clear 接口生效
mod.clear_tmdb_meta_cache()
check(len(mod._TMDB_META_CACHE) == 0, "clear_tmdb_meta_cache 清空全部缓存")
# 超限淘汰 1/5：塞入 505 条，淘汰后应 <= 500 且保留最近的
mod.clear_tmdb_meta_cache()
for i in range(505):
    mod._tmdb_meta_cache_put((2000 + i, "movie"), {"poster_path": "/p%d.jpg" % i}, now=1000 + i)
check(len(mod._TMDB_META_CACHE) <= mod._TMDB_META_MAX,
      f"超限后缓存数 <= 500: {len(mod._TMDB_META_CACHE)}")
check((2504, "movie") in mod._TMDB_META_CACHE, "淘汰最旧的 1/5，保留近期热数据")

# 配置变更触发缓存失效：插件 _get_config 指纹变化
mod.clear_tmdb_meta_cache()
mod._tmdb_meta_cache_put((3000, "movie"), {"poster_path": "/y.jpg"})
pinst = mod.TgWhitelistSyncPlugin()
pinst.set_config({"tmdb_meta_enabled": True})
pinst._get_config()  # 首次建立指纹
pinst.set_config({"tmdb_meta_enabled": False})
pinst._get_config()  # 指纹变化 -> clear
check(len(mod._TMDB_META_CACHE) == 0, "配置变更后 _get_config 自动清缓存")


# ============================================================
print("\n== 5. 最新经验：年份补全（v0.9.17 参数黑洞修复）==")
mod.clear_tmdb_meta_cache()
cli_year = FakeTmdb({"poster_path": "/a.jpg", "release_date": "2024-03-15"})
meta_year = mod._tmdb_meta(555, "movie", client=cli_year)
check(meta_year.get("year") == "2024", f"movie 从 release_date 补年份: {meta_year.get('year')}")
mod.clear_tmdb_meta_cache()
cli_year_tv = FakeTmdb({"poster_path": "/b.jpg", "first_air_date": "2022-11-01"})
meta_year_tv = mod._tmdb_meta(556, "tv", client=cli_year_tv)
check(meta_year_tv.get("year") == "2022", f"tv 从 first_air_date 补年份: {meta_year_tv.get('year')}")
# include_year 开关由 build_rule 依据 year  bool 决定（既有逻辑）
item_year = {"tmdb_id": 555, "season": 0, "title": "T", "keyword": "T", "year": "2024",
             "media_type": "movie", "rule_fields": {"resolution": "4k", "effect": "all", "required": "265"}}
r_year = client.build_rule(item_year)
check(r_year.get("year") == "2024" and r_year.get("include_year") is True,
      "年份写入并打开 include_year 开关")


# ============================================================
print("\n== 6. 最新经验：conflict / applied 诊断回执 ==")
# 构造 client：GET 返回含他人规则占位；POST 捕获；注入 _tmdb_meta 关闭走空
c2 = mod.TgtoDriveClient("http://tg", "tok", "/api/visual-filter/config", "ENV_FILTER_GUANGYA")
c2.verify_write = lambda **kw: (True, "ok")  # 跳过回读网络
c2._url = lambda: "http://tg/api/visual-filter/config"
c2._headers = lambda: {}
# 目标 id kw_700_0 被他人规则(source=manual)占位；另一个新 id kw_701_0 可写
c2.get_config = lambda: {"rules": [
    {"id": "kw_700_0", "title": "Others", "keywords": ["X"], "source": "manual"},
]}
posted = {}
def _post_capture(url, **kw):
    posted["payload"] = kw.get("json")
    return _Resp({"ok": True})
mod.requests.post = _post_capture
items = [
    {"tmdb_id": 700, "season": 0, "title": "Occupied", "keyword": "Occupied",
     "media_type": "movie", "year": "2020", "tmdb_meta": {}},
    {"tmdb_id": 701, "season": 0, "title": "Fresh", "keyword": "Fresh",
     "media_type": "movie", "year": "2021",
     "rule_fields": {"resolution": "4k", "effect": "all", "required": "265"},
     "tmdb_meta": {}},
]
res = c2.merge_and_save(items)
check(any(c.get("id") == "kw_700_0" for c in (res.get("conflicted") or [])),
      "他人规则占位 id 标记为 conflict")
check((res.get("conflicted") or [{}])[0].get("source") == "manual",
      "conflict 回执带占位规则的 source")
check("kw_700_0" in [r["id"] for r in posted.get("payload", {}).get("rules", [])]
      and posted["payload"]["rules"][0].get("source") == "manual",
      "conflict 规则原样保留，未被覆盖（零功能变化）")
applied = res.get("applied") or []
check(len(applied) == 1 and applied[0].get("id") == "kw_701_0",
      "applied 只记录本次真实写入的规则")
check(applied and applied[0].get("resolution") == "4k"
      and applied[0].get("required_keyword") == "265"
      and applied[0].get("year") == "2021",
      "applied 带分辨率/效果/必含/年份参数快照")
# 全部 conflict 时不应 POST
c3 = mod.TgtoDriveClient("http://tg", "tok", "/api/visual-filter/config", "ENV_FILTER_GUANGYA")
c3.get_config = lambda: {"rules": [{"id": "kw_800_0", "source": "manual", "keywords": []}]}
c3.verify_write = lambda **kw: (True, "ok")
c3._url = lambda: "http://tg/api/visual-filter/config"
c3._headers = lambda: {}
posted.clear()
mod.requests.post = lambda url, **kw: (posted.setdefault("called", True) or _Resp({"ok": True}))
res3 = c3.merge_and_save([{"tmdb_id": 800, "season": 0, "title": "X", "keyword": "X",
                           "media_type": "movie", "year": "", "tmdb_meta": {}}])
check("called" not in posted and res3.get("changed") is False,
      "全部 conflict 时不 POST（零净改动）")

print(f"\n==== 结果：通过 {PASS} / 失败 {FAIL} ====")
sys.exit(1 if FAIL else 0)
