"""端到端契约测试（subprocess 调 registry.py 本体）。

覆盖三块只能靠"真实跑一遍"守护的契约：
  1. 游标单调 + 永不回收（两条不可退让底线之一）
  2. viz 生成的架构图 HTML 逐字节稳定（v1.9.11 拆分 render_arch_html 的验收标准）
  3. quick 一行引导：自动 init（L 档）+ 登记第一条资产
"""

import hashlib
import re
import subprocess
import sys
from pathlib import Path

REGISTRY = str(Path(__file__).resolve().parents[1] / "scripts" / "registry.py")
PY = sys.executable


def _ids_from_index(root):
    txt = (root / ".project" / "registry" / "index.md").read_text(encoding="utf-8")
    return sorted(
        set(re.findall(r"\b([A-Z]+)-(\d{2,6})\b", txt)),
        key=lambda t: (t[0], int(t[1])),
    )


def _run(args, root):
    return subprocess.run(
        [PY, REGISTRY, *args, "--root", str(root)],
        capture_output=True, text=True,
    )


# ───────────────────────── 游标：单调 + 永不回收 ─────────────────────────

def test_cursor_monotonic_and_no_recycle(tmp_project):
    for n in ("登录", "注册", "首页"):
        r = _run(["add", "--type", "UI", "--name", n, "--status", "构思"], tmp_project)
        assert r.returncode == 0, r.stderr

    ui = [i[1] for i in _ids_from_index(tmp_project) if i[0] == "UI"]
    assert ui == ["001", "002", "003"]

    # 废弃中间的 002，再 add 应得 004（ID 永不回收，废弃不删行）
    r = _run(["retire", "UI-002", "--reason", "合并进登录"], tmp_project)
    assert r.returncode == 0, r.stderr
    r = _run(["add", "--type", "UI", "--name", "设置", "--status", "构思"], tmp_project)
    assert r.returncode == 0, r.stderr

    ui = [i[1] for i in _ids_from_index(tmp_project) if i[0] == "UI"]
    assert ui == ["001", "002", "003", "004"]


# ───────────────────────── viz：HTML 逐字节稳定 ─────────────────────────

def test_viz_html_byte_stable(tmp_project):
    _run(["add", "--type", "UI", "--name", "登录", "--status", "构思"], tmp_project)
    _run(["add", "--type", "UI", "--name", "注册", "--status", "构思", "--deps", "UI-001"],
         tmp_project)

    html = tmp_project / ".project" / "registry" / "architecture.html"
    r1 = _run(["viz"], tmp_project)
    assert r1.returncode == 0 and html.exists(), r1.stderr
    b1 = html.read_bytes()

    r2 = _run(["viz"], tmp_project)
    assert r2.returncode == 0, r2.stderr
    b2 = html.read_bytes()

    assert hashlib.sha256(b1).hexdigest() == hashlib.sha256(b2).hexdigest()


# ───────────────────────── quick：一行引导 ─────────────────────────

def test_quick_one_liner_boots_and_registers(tmp_path):
    r = _run(["quick", "登录页", "--type", "UI"], tmp_path)
    assert r.returncode == 0, r.stderr

    # 自动 init（L 档）建好骨架
    assert (tmp_path / ".project" / "PROJECT.md").exists()
    # 并登记了第一条资产 UI-001
    ui = [i for i in _ids_from_index(tmp_path) if i[0] == "UI"]
    assert ui == [("UI", "001")]
