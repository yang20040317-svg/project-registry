"""端到端契约测试（subprocess 调 registry.py 本体）。

覆盖三块只能靠"真实跑一遍"守护的契约：
  1. 游标单调 + 永不回收（两条不可退让底线之一）
  2. viz 生成的架构图 HTML 逐字节稳定（v1.9.11 拆分 render_arch_html 的验收标准）
  3. quick 一行引导：自动 init（L 档）+ 登记第一条资产
     并含「裸调用（无 --root）」一档：守卫必须放行 quick、仍拦住其他命令
  4. close 的收工交接提示：默认给「开新对话」提醒 + 可粘贴开场白，且能被 --no-tip 关掉
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


def _run_cwd(args, cwd):
    """不带 --root 的裸调用 —— 专测「当前目录及父级还没有 .project/」这条路径。

    `_run` 一律补 `--root`，而 `--root` 会绕过 main() 的前置守卫。quick 的
    「自动 init」因此长期没被任何用例覆盖：文档写一行起项目，实际 exit 1。
    凡是想验证「守卫该拦 / 该放行」的用例，必须走这一个入口。
    """
    return subprocess.run(
        [PY, REGISTRY, *args], cwd=str(cwd),
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


def test_quick_without_root_boots_in_fresh_dir(tmp_path):
    """文档承诺的形态：全新目录里直接 quick，不传 --root。

    这条与上一条的区别是**没有 --root** —— 上一条走的是被守卫放行的路径，
    所以即使 main() 的守卫把 quick 拦死，它照样绿。
    """
    r = _run_cwd(["quick", "登录页", "--type", "UI"], tmp_path)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / ".project" / "PROJECT.md").exists()
    ui = [i for i in _ids_from_index(tmp_path) if i[0] == "UI"]
    assert ui == [("UI", "001")]


def test_project_less_dir_still_guarded(tmp_path):
    """守卫不能被放宽过头：需要项目上下文的命令，裸调用仍要拦住。"""
    for cmd in ("list", "context", "close"):
        args = [cmd] + (["--summary", "x"] if cmd == "close" else [])
        r = _run_cwd(args, tmp_path)
        assert r.returncode != 0, "%s 在没有 .project/ 的目录里竟放行了" % cmd
        assert ".project" in r.stderr, r.stderr
    assert not (tmp_path / ".project").exists()


# ───────────────────────── close：收工交接提示 ─────────────────────────

def test_close_prints_handoff_tip(tmp_project):
    """收工必须提醒「开新对话」，并给一段能直接粘的开场白。

    这是对「长会话恐惧」的正面回应：光喊「去开新对话」没用，人不敢开是因为不知道
    新话题第一句说什么。所以提醒必须**连交接文本一起给**，且文本里要带项目根与
    开工第一读路径 —— 否则粘过去还是接不上。
    """
    _run(["add", "--type", "UI", "--name", "登录", "--status", "在建", "--path", "src/"],
         tmp_project)
    r = _run(["close", "--summary", "登录页做完", "--next", "接支付接口",
              "--no-doctor"], tmp_project)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "开一条新对话" in out, out
    assert "直接粘" in out, out
    assert str(tmp_project) in out            # 开场白带项目根
    assert "context-packet.md" in out         # 且指向开工第一读
    assert "接支付接口" in out                 # 起步点取自刚刷新的上下文包第 6 段


def test_close_tip_is_suppressible(tmp_project):
    """提示只读、不写盘，且必须能被关掉（脚本 / CI 里不想要这 10 行）。"""
    _run(["add", "--type", "UI", "--name", "登录", "--status", "在建", "--path", "src/"],
         tmp_project)
    r = _run(["close", "--summary", "第一件", "--no-doctor", "--no-tip"], tmp_project)
    assert r.returncode == 0, r.stderr
    assert "开一条新对话" not in r.stdout
    # 会话记录照写、上下文包照刷 —— 提示不是收工的前提
    assert (tmp_project / ".project" / "memory" / "context-packet.md").exists()
    assert "第一件" in (tmp_project / ".project" / "memory" / "log.md").read_text(
        encoding="utf-8")
