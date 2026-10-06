"""后端只读 SQL 工作台验证（无需 Flask）：用最小 stub 导入 app.py。

通过 stub 的最小路由分发驱动 **app.py 注册的实际路由**（真实经过
admit_experiment 准入门控、query_lock 与真实 SQLite，不另写模拟准入
逻辑），覆盖只读 SQL 工作台的全部约定：

- 文本校验：1～4000 字符、单条 SELECT / WITH，禁止写入 / PRAGMA /
  ATTACH / 扩展加载（字面量、引用标识符与注释先剥离），非法一律 400；
- 提交立即返回查询 ID；独立只读连接真实执行，返回列名、前 100 行与
  截断标志；整数无损编码、BLOB 转十六进制；
- 每实验只保留最新查询：运行中再提交 409；旧查询 ID 一律 404；
- 取消真实中断 SQL（长查询被 SQLITE_INTERRUPT 打断）、尚未开跑也取消、
  取消先被确认则不得成功、已终结则不改结果、连接关闭后才公布终态；
- 查询不触碰两栏事务、读快照与最近显式读值；失败 / 取消不改值及时间线；
- 删除先取消在途查询、等连接关闭再删文件，不阻塞其他实验；删除后 410；
- 查询状态不跨重启保留。
"""
import sys, types, json, os, re, tempfile, threading, time

# ---- 最小 Flask stub：路由注册 / 请求体注入 / 响应包装（仅 Web 框架管道） ----
flask = types.ModuleType("flask")


class _Resp:
    """仿 Flask 响应：视图返回 jsonify(...) 或 (jsonify(...), status)。"""

    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def get_json(self):
        return self._payload


def _jsonify(*args, **kwargs):
    if args and kwargs:
        raise TypeError("jsonify: cannot mix positional and keyword")
    if len(args) == 1:
        payload = args[0]
    elif args:
        payload = list(args)
    else:
        payload = kwargs
    return _Resp(payload)


def _compile_path(path):
    # /api/experiments/<exp_id>/query/<query_id>/cancel → 命名分组正则
    return re.compile("^" + re.sub(r"<(\w+)>", r"(?P<\1>[^/]+)", path) + "$")


class _App:
    def __init__(self, name):
        self.routes = []  # [(method, regex, view_fn)]，按注册顺序匹配

    def _reg(self, method, path):
        def deco(fn):
            self.routes.append((method, _compile_path(path), fn))
            return fn
        return deco

    def get(self, path, **k):
        return self._reg("GET", path)

    def post(self, path, **k):
        return self._reg("POST", path)

    def delete(self, path, **k):
        return self._reg("DELETE", path)

    def run(self, **k):
        pass


class _Req(threading.local):
    # 请求体按线程隔离：并发交错测试里各线程看到各自的 JSON body
    def __init__(self):
        self._json = None

    def get_json(self, silent=False):
        return self._json


flask.Flask = _App
flask.jsonify = _jsonify
flask.render_template = lambda *a, **k: "html"
flask.request = _Req()
sys.modules["flask"] = flask


class TestClient:
    """最小路由分发：直接调用 app.py 注册的实际视图函数。

    请求真实经过 admit_experiment 准入门控与全部实验级锁；
    这里只替代 Flask 的 URL 匹配与请求 / 响应包装，不另写模拟准入逻辑。
    """

    def __init__(self, app):
        self.app = app

    def open(self, method, path, json_body=None):
        flask.request._json = json_body
        try:
            for registered_method, rx, fn in self.app.routes:
                if registered_method != method:
                    continue
                match = rx.match(path)
                if match:
                    return self._normalize(fn(**match.groupdict()))
            raise AssertionError("no route: %s %s" % (method, path))
        finally:
            flask.request._json = None

    @staticmethod
    def _normalize(rv):
        if isinstance(rv, tuple):
            resp, status = rv[0], rv[1]
            resp.status_code = status
            return resp
        if isinstance(rv, _Resp):
            return rv
        return _Resp(rv)

    def get(self, path):
        return self.open("GET", path)

    def post(self, path, json=None):
        return self.open("POST", path, json)

    def delete(self, path):
        return self.open("DELETE", path)


_tmp = tempfile.mkdtemp()
os.environ["LAB_DATA_DIR"] = _tmp
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
import app as A

fails = []


def check(name, cond, extra=""):
    print(("  ok  " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


client = TestClient(A.app)
ROUTE_TIMEOUT = 5.0  # 正常路由调用为毫秒级；超时即视为「卡住」回归


def route_call(fn, *args, **kwargs):
    """在守护线程中执行路由调用并限时等待；卡住 / 异常变成明确的失败。"""
    box = {}

    def runner():
        try:
            box["resp"] = fn(*args, **kwargs)
        except Exception as exc:
            box["exc"] = exc

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join(ROUTE_TIMEOUT)
    if t.is_alive():
        return None, None, True
    if "exc" in box:
        return None, box["exc"], False
    return box["resp"], None, False


def r_post(path, json_body=None):
    return route_call(client.post, path, json_body)


def r_get(path):
    return route_call(client.get, path)


def r_delete(path):
    return route_call(client.delete, path)


def create_exp():
    resp, exc, stuck = r_post("/api/experiments")
    assert not stuck and exc is None and resp.status_code == 200
    return resp.get_json()["id"]


def submit(exp_id, sql):
    return r_post("/api/experiments/%s/query" % exp_id, {"sql": sql})


def get_query(exp_id, query_id):
    return r_get("/api/experiments/%s/query/%s" % (exp_id, query_id))


def get_current(exp_id):
    return r_get("/api/experiments/%s/query" % exp_id)


def cancel(exp_id, query_id):
    return r_post("/api/experiments/%s/query/%s/cancel" % (exp_id, query_id))


def wait_final(exp_id, query_id, timeout=10.0):
    """轮询直到查询终结（成功 / 失败 / 取消），返回最终视图。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        resp, exc, stuck = get_query(exp_id, query_id)
        assert not stuck and exc is None and resp.status_code == 200
        view = resp.get_json()["query"]
        if view["status"] != "running":
            return view
        time.sleep(0.01)
    raise AssertionError("query %s did not finish in %.1fs" % (query_id, timeout))


def committed(exp_id):
    resp, exc, stuck = r_get("/api/experiments/%s/committed" % exp_id)
    assert not stuck and exc is None and resp.status_code == 200
    return resp.get_json()["committed"]


def timeline(exp_id):
    resp, exc, stuck = r_get("/api/experiments/%s/timeline" % exp_id)
    assert not stuck and exc is None and resp.status_code == 200
    return resp.get_json()["timeline"]


def status(exp_id):
    resp, exc, stuck = r_get("/api/experiments/%s/status" % exp_id)
    assert not stuck and exc is None and resp.status_code == 200
    return resp.get_json()


def op(exp_id, side, body):
    resp, exc, stuck = r_post("/api/experiments/%s/conn/%s/op" % (exp_id, side), body)
    assert not stuck and exc is None and resp.status_code == 200, (stuck, exc)
    return resp.get_json()


# 长查询：递归数到 5 亿，未被打断需要数十秒，用于取消 / 删除交错
LONG_SQL = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c"
    " WHERE x < 500000000) SELECT count(*) FROM c"
)


def wait_conn_published(exp, timeout=5.0):
    """等待 worker 把执行连接发布到查询字典（取消方据此 interrupt）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        with exp.query_lock:
            conn = exp.query["conn"] if exp.query else None
        if conn is not None:
            return conn
        time.sleep(0.01)
    raise AssertionError("query connection was not published")


def submit_long_and_wait_running(exp_id):
    """提交长查询并等到它真实进入执行（conn.execute 进行中）再返回查询 ID。

    sqlite3 的 interrupt 只影响正在运行的语句：取消若落在「连接已发布、
    conn.execute 尚未开始」的微小窗口会空转（终态仍会在执行完后公布为
    已取消，但要等整个长查询跑完）。这里用包装 _execute_query 的信号
    事件确定性地等到执行开始，避免测试依赖时序运气。
    """
    entered = threading.Event()
    orig_exec = A.Experiment._execute_query

    def signaling_exec(self, conn, sql):
        entered.set()
        return orig_exec(self, conn, sql)

    A.Experiment._execute_query = signaling_exec
    try:
        resp, exc, stuck = submit(exp_id, LONG_SQL)
        assert not stuck and exc is None and resp.status_code == 200
        run_id = resp.get_json()["query"]["id"]
        assert entered.wait(5.0), "worker did not enter execute"
        time.sleep(0.1)  # 进入执行后稍候，确保 conn.execute 已在运行
        return run_id
    finally:
        A.Experiment._execute_query = orig_exec


# ================= 文本校验：非法一律 400，不执行 =================
exp_v = create_exp()
bad_sqls = [
    None,  # 缺字段
    123,  # 非字符串
    True,  # 布尔不是字符串
    "",  # 空
    "   \n\t  ",  # 纯空白
    "SELECT 1; SELECT 2",  # 多条
    "SELECT 1; DELETE FROM kv",  # 多条（第二条写入）
    "UPDATE kv SET val = 1",  # 写入
    "INSERT INTO kv VALUES (1, 2)",
    "DELETE FROM kv",
    "CREATE TABLE x (a)",
    "DROP TABLE kv",
    "ALTER TABLE kv ADD COLUMN x",
    "REPLACE INTO kv VALUES (1, 2)",
    "VACUUM",
    "PRAGMA journal_mode",  # PRAGMA
    "PRAGMA query_only=0",
    "ATTACH DATABASE 'x.db' AS y",  # ATTACH
    "DETACH DATABASE y",
    "WITH x AS (SELECT 1) DELETE FROM kv",  # WITH 开头的写入
    "SELECT load_extension('/tmp/evil.so')",  # 扩展加载
    "EXPLAIN SELECT 1",  # 非 SELECT / WITH
    "VALUES (1, 2)",
    "SELECT " + "1" * 4000,  # 超过 4000 字符
]
for sql in bad_sqls:
    resp, exc, stuck = submit(exp_v, sql)
    check("校验·400：%r" % (str(sql)[:40] if sql is not None else None),
          not stuck and exc is None and resp.status_code == 400
          and "error" in resp.get_json(),
          "%s %s" % (getattr(resp, "status_code", None), exc))
check("校验·全部拒绝后无查询", get_current(exp_v)[0].get_json()["query"] is None)

# 合法边界：恰好 4000 字符、末尾分号、字面量中的分号、注释、WITH
ok_sqls = [
    "SELECT 1;" + " " * (4000 - len("SELECT 1;")),  # 恰好 4000
    "SELECT ';'",  # 字面量中的分号不是语句分隔
    "SELECT 'it''s' AS t",  # 转义引号
    '-- 注释\nSELECT 2 /* 块注释 ; */',
    "WITH x AS (SELECT 3) SELECT * FROM x;",
]
for sql in ok_sqls:
    resp, exc, stuck = submit(exp_v, sql)
    ok = not stuck and exc is None and resp.status_code == 200
    check("校验·接受：%r" % sql[:30], ok,
          "%s %s" % (getattr(resp, "status_code", None), resp.get_json() if not stuck and exc is None else exc))
    if ok:
        view = wait_final(exp_v, resp.get_json()["query"]["id"])
        check("校验·执行成功：%r" % sql[:30], view["status"] == "success", str(view))

# ================= 基本执行：列名、行、截断标志、编码 =================
exp1 = create_exp()
resp, exc, stuck = submit(exp1, "SELECT id, val FROM kv")
check("基本·提交及时返回200", not stuck and exc is None and resp.status_code == 200)
body = resp.get_json()
check("基本·提交立即返回查询ID与running",
      body["ok"] is True and body["query"]["status"] == "running"
      and isinstance(body["query"]["id"], str) and body["query"]["id"],
      str(body))
qid = body["query"]["id"]
view = wait_final(exp1, qid)
check("基本·成功：列名+行+截断标志",
      view["status"] == "success" and view["columns"] == ["id", "val"]
      and view["rows"] == [[1, 100]] and view["truncated"] is False, str(view))
resp, exc, stuck = get_current(exp1)
check("基本·GET当前查询与按ID一致",
      resp.get_json()["query"]["id"] == qid and resp.get_json()["query"]["status"] == "success")
st = status(exp1)
check("基本·状态接口携带query字段", st["query"]["id"] == qid and st["query"]["status"] == "success")

# 编码：大整数无损（十进制字符串）、安全整数为数字、BLOB 十六进制、NULL / REAL / TEXT
resp, _, _ = submit(exp1, "SELECT 9007199254741093 AS big, 9223372036854775807 AS max64,"
                          " -9223372036854775808 AS min64, 7 AS small, x'AB01FF' AS blob,"
                          " NULL AS nul, 1.5 AS real, 'txt' AS txt")
view = wait_final(exp1, resp.get_json()["query"]["id"])
check("编码·整数无损+BLOB十六进制+NULL/REAL/TEXT",
      view["rows"] == [["9007199254741093", "9223372036854775807",
                        "-9223372036854775808", 7, "AB01FF", None, 1.5, "txt"]],
      str(view["rows"]))

# 截断：150 行只回前 100 行并标记
resp, _, _ = submit(exp1, "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c"
                          " WHERE x < 150) SELECT x FROM c")
view = wait_final(exp1, resp.get_json()["query"]["id"])
check("截断·150行只回前100行",
      view["status"] == "success" and len(view["rows"]) == 100
      and view["rows"][0] == [1] and view["rows"][99] == [100]
      and view["truncated"] is True, str(view.get("truncated")))
resp, _, _ = submit(exp1, "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c"
                          " WHERE x < 100) SELECT x FROM c")
view = wait_final(exp1, resp.get_json()["query"]["id"])
check("截断·恰好100行不标记", view["truncated"] is False and len(view["rows"]) == 100)

# 失败：不存在的表；失败不改值及时间线
before_committed, before_timeline = committed(exp1), timeline(exp1)
resp, _, _ = submit(exp1, "SELECT * FROM missing_table")
view = wait_final(exp1, resp.get_json()["query"]["id"])
check("失败·如实携带SQLite错误名/码",
      view["status"] == "failed" and view["error"]["name"] == "SQLITE_ERROR"
      and view["error"]["code"] == 1 and "missing_table" in view["error"]["message"],
      str(view.get("error")))
check("失败·已提交值与时间线不变",
      committed(exp1) == before_committed and timeline(exp1) == before_timeline)

# ================= 运行中再提交 409；旧查询 ID 404 =================
run_id = submit_long_and_wait_running(exp1)
resp, exc, stuck = submit(exp1, "SELECT 1")
check("互斥·运行中再提交409且附当前视图",
      not stuck and exc is None and resp.status_code == 409
      and resp.get_json()["query"]["id"] == run_id
      and resp.get_json()["query"]["status"] == "running", str(resp.get_json() if not stuck else None))
# 并发提交：恰好全部 409（运行中的仍是同一个）
results = []
threads = [threading.Thread(target=lambda: results.append(submit(exp1, "SELECT 1"))) for _ in range(5)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("互斥·并发再提交全部409",
      all(not s and e is None and r.status_code == 409 for r, e, s in results), str(results))
# 取消后终结，新提交替换：旧 ID 404，不影响新查询
cancel(exp1, run_id)
view = wait_final(exp1, run_id)
check("互斥·取消后终态为已取消", view["status"] == "cancelled", str(view))
resp, _, _ = submit(exp1, "SELECT 42")
new_id = resp.get_json()["query"]["id"]
check("互斥·取消后可再提交", new_id != run_id)
resp, exc, stuck = get_query(exp1, run_id)
check("互斥·旧查询ID查询404", not stuck and exc is None and resp.status_code == 404)
resp, exc, stuck = cancel(exp1, run_id)
check("互斥·旧查询ID取消404", not stuck and exc is None and resp.status_code == 404)
resp, exc, stuck = get_query(exp1, new_id)
check("互斥·新查询ID不受影响", not stuck and exc is None and resp.status_code == 200)
wait_final(exp1, new_id)

# ================= 取消：真实中断运行中的 SQL =================
run_id = submit_long_and_wait_running(exp1)
exp_obj = A.experiments[exp1]
t0 = time.time()
resp, exc, stuck = cancel(exp1, run_id)
check("取消·及时返回200", not stuck and exc is None and resp.status_code == 200)
view = wait_final(exp1, run_id)
elapsed = time.time() - t0
check("取消·运行中长查询被真实中断（SQLITE_INTERRUPT）",
      view["status"] == "cancelled" and elapsed < 5.0,
      "%s %.2fs" % (view["status"], elapsed))
check("取消·连接关闭后才公布终态（终态时连接已清空）",
      exp_obj.query["conn"] is None and exp_obj.query["done"].is_set())
# 已终结再取消：200 且不改结果
resp, exc, stuck = cancel(exp1, run_id)
check("取消·已终结不改结果",
      not stuck and exc is None and resp.status_code == 200
      and resp.get_json()["query"]["status"] == "cancelled")
check("取消·已提交值与时间线不变",
      committed(exp1) == 100 and timeline(exp1) == [])

# ================= 尚未开跑也须取消 =================
# 用包装 _query_worker 的闸门把 worker 按在启动前：取消确认后放行，
# worker 开跑前看到取消标志，不再执行直接公布取消
orig_worker = A.Experiment._query_worker
gate = threading.Event()
seen = []


def gated_worker(self, query):
    seen.append(query["id"])
    gate.wait()
    return orig_worker(self, query)


A.Experiment._query_worker = gated_worker
try:
    resp, _, _ = submit(exp1, "SELECT 1")
    pre_id = resp.get_json()["query"]["id"]
    time.sleep(0.1)  # worker 已被闸门按住，尚未开跑
    resp, exc, stuck = cancel(exp1, pre_id)
    check("未开跑·取消及时确认", not stuck and exc is None and resp.status_code == 200)
    gate.set()
    view = wait_final(exp1, pre_id)
    check("未开跑·放行后公布已取消（未执行）", view["status"] == "cancelled", str(view))
finally:
    A.Experiment._query_worker = orig_worker
    gate.set()

# ================= 取消先被确认则不得成功 =================
# 用包装 _execute_query 的闸门把 worker 按在「已执行完成、尚未公布」：
# 此时取消确认，放行后结果必须作废，公布取消而不是成功
orig_exec = A.Experiment._execute_query
gate2 = threading.Event()


def gated_exec(self, conn, sql):
    outcome = orig_exec(self, conn, sql)
    gate2.wait()  # 结果已算出但暂不返回，等待取消确认
    return outcome


A.Experiment._execute_query = gated_exec
try:
    resp, _, _ = submit(exp1, "SELECT 1")
    race_id = resp.get_json()["query"]["id"]
    wait_conn_published(A.experiments[exp1])
    time.sleep(0.1)  # worker 已执行完 SELECT 1，按在公布前
    resp, exc, stuck = cancel(exp1, race_id)
    check("竞态·公布前取消被确认", not stuck and exc is None and resp.status_code == 200)
    gate2.set()
    view = wait_final(exp1, race_id)
    check("竞态·取消先被确认则不得成功", view["status"] == "cancelled", str(view))
finally:
    A.Experiment._execute_query = orig_exec
    gate2.set()

# ================= 查询不触碰两栏事务、快照与最近读值 =================
exp2 = create_exp()
op(exp2, "a", {"op": "begin"})
body = op(exp2, "a", {"op": "read"})
check("隔离·甲读值100", body["state"]["last_read"] == 100)
body = op(exp2, "a", {"op": "add", "value": 5})
check("隔离·甲+5未提交", body["ok"] is True)
resp, _, _ = submit(exp2, "SELECT val FROM kv WHERE id = 1")
view = wait_final(exp2, resp.get_json()["query"]["id"])
check("隔离·查询只见已提交值100（不见未提交105）",
      view["status"] == "success" and view["rows"] == [[100]], str(view))
st = status(exp2)
check("隔离·两栏事务/读值/快照不受影响",
      st["a"]["in_transaction"] is True and st["a"]["last_read"] == 100)
body = op(exp2, "a", {"op": "commit"})
check("隔离·甲原事务提交成功105", body["ok"] is True and committed(exp2) == 105)
check("隔离·时间线正常记录", timeline(exp2)[-1]["after"] == 105)

# ================= 删除：先取消在途查询、等连接关闭再删文件 =================
exp3 = create_exp()
db_path = A.experiments[exp3].db_path
del_id = submit_long_and_wait_running(exp3)
t0 = time.time()
resp, exc, stuck = r_delete("/api/experiments/%s" % exp3)
elapsed = time.time() - t0
check("删除·在途查询被快速取消后删除成功",
      not stuck and exc is None and resp.status_code == 200
      and resp.get_json()["deleted"] is True and elapsed < 5.0,
      "%.2fs" % elapsed)
check("删除·在途查询终态为已取消",
      A.experiments[exp3].query["status"] == "cancelled")
check("删除·库文件与WAL/SHM已移除",
      not os.path.exists(db_path)
      and not os.path.exists(db_path + "-wal")
      and not os.path.exists(db_path + "-shm"))
resp, exc, stuck = submit(exp3, "SELECT 1")
check("删除·提交410", not stuck and exc is None and resp.status_code == 410)
resp, exc, stuck = get_current(exp3)
check("删除·查询当前410", not stuck and exc is None and resp.status_code == 410)
resp, exc, stuck = get_query(exp3, del_id)
check("删除·按ID查询410", not stuck and exc is None and resp.status_code == 410)
resp, exc, stuck = cancel(exp3, del_id)
check("删除·取消410", not stuck and exc is None and resp.status_code == 410)

# 删除不阻塞其他实验：exp4 在 exp5 删除等待期间照常查询与操作
exp4 = create_exp()
exp5 = create_exp()
submit_long_and_wait_running(exp5)
box = {}
t = threading.Thread(
    target=lambda: box.setdefault("resp", client.delete("/api/experiments/%s" % exp5)),
    daemon=True,
)
t.start()
resp, _, _ = submit(exp4, "SELECT 1")
view = wait_final(exp4, resp.get_json()["query"]["id"])
check("删除·不阻塞其他实验的查询", view["status"] == "success" and view["rows"] == [[1]])
body = op(exp4, "a", {"op": "begin"})
check("删除·不阻塞其他实验的操作", body["ok"] is True)
op(exp4, "a", {"op": "rollback"})
t.join(5.0)
check("删除·等待中的删除收尾完成",
      not t.is_alive() and box["resp"].status_code == 200)

# ================= 未知实验 404；查询不跨重启保留 =================
for fn, path in [
    (r_post, "/api/experiments/deadbeef0000/query"),
    (r_get, "/api/experiments/deadbeef0000/query"),
    (r_get, "/api/experiments/deadbeef0000/query/abc"),
    (r_post, "/api/experiments/deadbeef0000/query/abc/cancel"),
]:
    resp, exc, stuck = fn(path, {"sql": "SELECT 1"}) if fn is r_post else fn(path)
    check("未知实验·404：%s" % path, not stuck and exc is None and resp.status_code == 404)

# 重启：关闭并丢弃内存对象、重新扫描数据目录（查询状态只保存在内存）
exp6 = create_exp()
resp, _, _ = submit(exp6, "SELECT 1")
wait_final(exp6, resp.get_json()["query"]["id"])
exp_obj = A.experiments[exp6]
with A.experiments_lock:
    A.experiments.pop(exp6, None)
for side in ("a", "b"):
    exp_obj.conns[side].close()
exp_obj.observer.close()
A.restore_experiments()
resp, exc, stuck = get_current(exp6)
check("重启·查询不跨重启保留",
      not stuck and exc is None and resp.status_code == 200
      and resp.get_json()["query"] is None)
st = status(exp6)
check("重启·状态接口query为null", st["query"] is None)
check("重启·实验本身按原ID恢复", committed(exp6) == 100)

print()
if fails:
    print("FAILED: %d 项" % len(fails))
    for name in fails:
        print("  - " + name)
    sys.exit(1)
print("全部通过")
