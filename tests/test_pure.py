"""纯函数契约测试（直接 import registry，不依赖磁盘项目）。

覆盖四块最脆、最该被守护的不变量：
  1. 日期归一化（days_since / _normalize_day）—— 只认 %Y-%m-%d，否则 None
  2. 收据稳定性（_delta_hash / build_delta）—— 同内容必得同收据，与文件名/时间戳无关
  3. fan-out 口径（_fanout_map）—— 去重 / 排除自环 / 含悬空引用，graph|viz|audit 共用

这些输出"看起来都正常"，只有对比历史或极端输入才暴露；没有回归测试就等于把
技能最自豪的"诚实边界"放在没人盯的角落。
"""

import registry as R


# ───────────────────────── 日期归一化 ─────────────────────────

def test_normalize_day_iso():
    assert R._normalize_day("2026-09-25") == (2026, 9, 25)


def test_normalize_day_iso_and_partial():
    # 完整日期：直接归一
    assert R._normalize_day("2026-09-25") == (2026, 9, 25)
    # 部分日期（%m-%d，注册表「更新」列的真实写法）：自动补年份，绝不 None
    # —— 这是曾被静默搞坏的坑：读侧原先只认 %Y-%m-%d，对着自己写的 09-25 恒返 None
    t = R._normalize_day("09-25")
    assert t is not None and t[1:] == (9, 25)


def test_normalize_day_unparseable_is_none():
    # 真正认不出的才 None —— 不猜、不当 0，否则超期检查静默失效
    assert R._normalize_day("") is None
    assert R._normalize_day("今天") is None
    assert R._normalize_day("2026/9/25") is None  # 斜杠分隔不匹配


def test_days_since_past_is_positive():
    d = R.days_since("2000-01-01")
    assert d is not None and d > 0


def test_days_since_unparseable_is_none():
    # 不猜、不当 0 —— "不知道"当"刚碰过"会让超期检查静默失效，那比不报更糟。
    assert R.days_since("不是日期") is None


# ───────────────────────── 收据稳定性 ─────────────────────────

def _manifest(assets):
    return {"project": "X", "assets": assets}


def test_delta_hash_stable_and_order_independent():
    o = {"a": 1, "b": [1, 2]}
    assert R._delta_hash(o) == R._delta_hash(o)
    # dict 键顺序不应影响哈希（sort_keys=True）
    assert R._delta_hash({"a": 1, "b": 2}) == R._delta_hash({"b": 2, "a": 1})


def test_build_delta_receipt_filename_independent():
    # 同内容、只换文件名 → 收据必须相同（否则"过一周再跑、收据没变=架构没动"失效）
    m = _manifest([{"id": "UI-001", "name": "a", "status": "在建"}])
    d1 = R.build_delta(m, m, "baseline-2026-09.json", "head-2026-09.json")
    d2 = R.build_delta(m, m, "renamed-base.json", "renamed-head.json")
    assert d1["receipt"]["receipt"] == d2["receipt"]["receipt"]


def test_build_delta_receipt_no_timestamp_drift():
    m = _manifest([{"id": "UI-001", "name": "a", "status": "在建"}])
    d1 = R.build_delta(m, m, "a.json", "b.json")
    d2 = R.build_delta(m, m, "a.json", "b.json")
    # 两次调用时间可能跨秒，但 receipt 不应因此变化
    assert d1["receipt"]["receipt"] == d2["receipt"]["receipt"]


# ───────────────────────── fan-out 口径 ─────────────────────────

def test_fanout_dedup_no_selfloop_includes_dangling():
    # 去重（A→B 写两遍只算一个）、排除自环（A→A 不算出度）、含悬空引用（C 不在 nodes 仍计入）
    fm = R._fanout_map(
        ["A", "B"],
        [("A", "B"), ("A", "B"), ("A", "A"), ("A", "C")],
    )
    assert fm == {"A": {"B", "C"}}
