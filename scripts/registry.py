#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
project-registry · 资产注册表与记忆层的落盘工具（stdlib only）

命令：
  init     --name <项目名> --type <code-ui|writing-content|research|custom> [--scale L|M|F]
           [--dims "UI:界面元素,MOD:功能模块"] [--force] [--reset] [--root <dir>]
  add      --type <PREFIX> --name <语义名> [--alias] [--path] [--deps] [--decision]
           [--status] [--dims "PFX:维度名"] [--force] [--allow-dangling-deps] [--root]
  set      <ID> [--status] [--name] [--alias] [--path] [--deps] [--decision]
           [--allow-dangling-deps] [--root]
  retire   <ID> --reason <为什么弃用> [--root]
  find     <查询词> [--type <PREFIX>] [--root]
  list     [--status <状态>] [--type <PREFIX>] [--root]
  check    [--idea-days N] [--strict-deps] [--root]
  context  [--root]
  close    --summary <一句话> [--next <下次起步点>]
           [--opens "事项|卡在哪|关联资产"] [--no-doctor] [--root]
  loop     [--list] [--done <序号或关键词>] [--root]
  idseq    [--set <PREFIX> <N>] [--root]
  export   [--out <file>] [--root]
  reconcile [--ext] [--write] [--root]
  graph    [--root]
  viz      [--out <file>] [--root]     生成自包含架构图 HTML（注册表 → 分层依赖图，给人看）
  scan     [--ext] [--out] [--json] [--root]        导入级代码理解：import → 文件级依赖图（非符号级）
  audit    [--init] [--source auto|imports|registry] [--strict] [--out] [--json] [--root]
           架构校验：层间规则（.project/architecture.json）+ 循环 + 出度 + 未归类
  diff     <base.json> <head.json> [--out] [--json]  两份 manifest → 架构演进 + 机器收据
  quick    <语义名> --type <PREFIX> [--project 名] [--ptype custom] [--scale L] [--status 构思]
           [--alias] [--path] [--deps] [--root]   一行起项目并登记第一条资产（轻量上手）

资产全部落在 <项目根>/.project/ 下，本脚本只读写项目内文件。
"""
import argparse
import atexit
import difflib
import hashlib
import json
import os
import re
import shutil
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
TYPES_DIR = SKILL_DIR / "references" / "project-types"
TPL_DIR = SKILL_DIR / "references" / "templates"

SECTION_RE = re.compile(r"^##\s+(.+?)\s*\(([A-Z][A-Z0-9_-]{0,5})\)\s*$")
# 无中文标题的纯前缀分节（`## CH`）：自动建节时用，避免产出自指的 `## CH (CH)`
BARE_SECTION_RE = re.compile(r"^##\s+([A-Z][A-Z0-9_-]{0,5})\s*$")
ID_RE = re.compile(r"\b([A-Z][A-Z0-9_-]{0,5})-(\d{2,6})\b")
MAX_ID = 999999      # 编号上限：超限的 ID 会脱离所有 ID 逻辑（check / 依赖 / find 都认不出）
ADR_RE = re.compile(r"\bADR-(\d{2,4})\b")
COLUMNS = ["ID", "语义名", "别名", "位置", "状态", "依赖", "决策", "更新"]
OL_COLS = ["事项", "卡在哪", "关联资产", "登记日"]
VALID_STATUS = {"构思", "在建", "可用", "冻结", "废弃"}
# 「构思」= 开工前先登记。它的「位置」只是**意向位置**（还没落盘），所以路径存在性校验
# 与「登记的东西不见了」对账都不该套在它头上——否则"先登记再动手"的第一步会被自己的闸门
# 卡死（实测四处同根因：check 报 ERROR 位置不存在、context 活跃资产段全空、reconcile 报
# 失效登记、scan 报位置对不上）。五态里只有它享受这个豁免；一旦落盘改成在建/可用，
# 四处出口必须立刻照旧报警（定向驱动带负向对照）。
PLANNED_STATUS = "构思"
# 「构思」超期阈值（天）：登记了却一直没动手，超过这个天数就从 PLAN 升成 WARN。
# 只升 WARN、不进 errs（killing 正常节奏）：今天登记、几天后动手是常态，卡住它会逼人不敢登记；
# 真正要抓的是「登记完就忘了」。取 PROJECT.md 的「- 构思超期阈值：N 天」覆盖本默认，
# `check --idea-days N` 再临时覆盖（0 = 关闭升级，只保留 PLAN 行）。
IDEA_STALE_DAYS = 7
SEP_RE = re.compile(r"^\|[\s:\-|]+\|$")
FP_RE = re.compile(r"<!--\s*fp:([0-9a-f]+)\s*-->")
PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9_-]{0,5}$")

SKIP_DIRS = {".git", ".project", "node_modules", "dist", "build", ".next", ".venv", "venv",
             "__pycache__", ".idea", ".vscode", "target", "out", "coverage"}
DEFAULT_EXTS = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".vue", ".svelte", ".py",
                ".go", ".rs", ".java", ".kt", ".cs", ".rb", ".php"]
# 禁用词分两档。分档的原因：把「模块 / 组件 / 管理器 / 优化」当成子串禁用会误伤
# 「持仓模块」「性能优化」「PositionManager」——文档写的是「无修饰 / 单独使用」才禁用。
FORBIDDEN_ANY = ["新的", "新版", "最终版", "临时",           # 时间词/临时：做定语也是反模式
                 "New", "new", "Temp", "temp", "tmp", "Final", "v2", "V2"]
FORBIDDEN_BARE = ["模块", "组件", "管理器", "测试", "优化", "改进", "增强",
                  "Manager", "Helper", "Utils"]
QUALIFIER_CHARS = set("新的版最终优化改进增强临时测试")
PLACEHOLDER_PREFIX = {"XXX", "ZZZ", "YYY"}
_LATIN_RUN_RE = re.compile(r"[A-Za-z0-9]+")
_LATIN_PART_RE = re.compile(r"[A-Z]+(?![a-z])[0-9]*|[A-Z][a-z0-9]*|[a-z]+[0-9]*|[0-9]+")


def latin_tokens(s):
    """把 camelCase / snake_case 拆成词，供拉丁禁用词按「词」匹配而非子串匹配。"""
    out = set()
    for run in _LATIN_RUN_RE.findall(s or ""):
        out.update(_LATIN_PART_RE.findall(run))
    return out


def bad_name(s):
    """禁用词检测。

    - ANY 档：出现在任何位置都违规（时间词 / 临时）——「新的持仓卡片」里的「新的」同样违规。
    - BARE 档：只有「名字去掉它之后什么都不剩」才算违规。「持仓模块」「性能优化」
      「PositionManager」都有实质主体，放行；光秃秃的「模块」「Manager」才打回。
    """
    s = (s or "").strip()
    if not s:
        return []
    lat = {t.lower() for t in latin_tokens(s)}
    hit = []
    for w in FORBIDDEN_ANY:
        if re.fullmatch(r"[A-Za-z0-9]+", w):
            if w.lower() in lat:
                hit.append(w)
        elif w in s:
            hit.append(w)
    for w in FORBIDDEN_BARE:
        if re.fullmatch(r"[A-Za-z0-9]+", w):
            if w.lower() not in lat:
                continue
            rest = re.sub(r"(?i)" + re.escape(w), " ", s)
        else:
            if w not in s:
                continue
            rest = s.replace(w, " ")
        # 剥掉禁用词后还剩实质主体（中日韩文字 / 字母数字）→ 它有内容，不算违规
        if not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", rest):
            hit.append(w)
    return sorted(set(hit))


def is_placeholder(s):
    """PROJECT.md 里没填的占位符不能当真值输出（否则上下文包一本正经地引用空话）。"""
    s = (s or "").strip()
    if not s:
        return True
    if s.startswith("<") and s.endswith(">"):
        return True
    if s.startswith("（") and s.endswith("）"):
        return True
    if "待填" in s or "TODO" in s.upper():
        return True
    return False


def blank_field(s):
    """人手写的「无 / － / N/A」是空值占位，不是有效内容。
    不识别它的话，「关联资产=无」会被当成"这条开环有归属"：排序把它排到最前，
    check 的开环归属闸门也放它过去——占位词把两道闸门同时骗开。"""
    t = (s or "").strip()
    return t in ("", "-", "—", "－", "–", "/", "无", "暂无", "没有", "N/A", "n/a", "NA", "n.a.")


def guard_fields(**kw):
    """竖线/换行会把 markdown 表格撑破——静默接受就是静默损坏，必须拦。"""
    bad = [k for k, v in kw.items() if v and ("|" in v or "\n" in v or "\r" in v)]
    if bad:
        die("字段含竖线或换行，会破坏表格：" + "、".join(bad) + "。改用斜杠 / 顿号分隔。")


def dangling_deps(rid, deps, known):
    """依赖列里指向未登记 ID 的引用（去重、保序）。

    「依赖」列是 viz / graph / audit **三个出口共同的输入**：一条指向不存在 ID 的依赖会让
    三个读数一起偏，而收工时只表现为一行 WARN。所以它值得在写入那一刻就拦。
    集合口径与 check 的引用扫描一致：跳过自身与 ADR-*。
    """
    seen, out = set(), []
    for m in ID_RE.finditer(deps or ""):
        tgt = m.group(0)
        if tgt == rid or m.group(1) == "ADR" or tgt in known or tgt in seen:
            continue
        seen.add(tgt)
        out.append(tgt)
    return out


def nearest_ids(bad, known):
    """给一个写错的 ID 找候选：先同前缀按号近，再退到字符串近似。返 [] 表示无从建议。"""
    m = re.match(r"^([A-Z][A-Z0-9_-]*)-(\d+)$", bad)
    if m:
        same = [i for i in known if i.startswith(m.group(1) + "-")]
        same.sort(key=lambda i: (abs(int(i.rsplit("-", 1)[1]) - int(m.group(2))), i))
        if same:
            return same[:3]
    return difflib.get_close_matches(bad, sorted(known), n=3, cutoff=0.4)


def gate_deps(rid, deps, known, allow=False):
    """写入时依赖闸门：依赖必须指向**已登记**的 ID，未登记直接拒绝写入。

    为什么拦在写入而不是收工算总账：写错的只有当时那个人、那个动作，成本是零；等到收工时，
    引用已经铺开，改起来贵一个数量级。这里也**不假装有例外场景**——想依赖还没动手的东西，
    就先把它登记成「构思」（五态之一是专门给这个用的）。
    真需要例外（批量登记的前向引用 / 有意造悬空边的夹具）→ --allow-dangling-deps，且会回显。
    """
    bad = dangling_deps(rid, deps, known)
    if not bad:
        return
    if allow:
        print(f"  ⚠ 已放行 {len(bad)} 条悬空依赖（--allow-dangling-deps）：{'、'.join(bad)}")
        print("    依赖列是 viz / graph / audit 的共同输入；留悬空边这三个读数会一起偏。")
        return
    print(f"依赖闸门：依赖必须指向已登记的 ID —— {'、'.join(bad)} 还没登记。")
    for b in bad:
        cand = nearest_ids(b, known)
        print(f"  {b} 未登记。" + (f"是不是想写 {' / '.join(cand)}？" if cand else
              "先把它登记出来（还没开工就加 --status 构思），再写这条依赖。"))
    print("  拦在这里是因为：写错的只有正在写它的那一次，收工时引用已经铺开、改起来贵得多。")
    print("  批量登记的前向引用 / 有意造悬空边的夹具 → 加 --allow-dangling-deps 放行（会回显）。")
    sys.exit(2)


# ---------- 基础工具 ----------

def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def find_root(start=None):
    p = Path(start).resolve() if start else Path.cwd().resolve()
    if (p / ".project").is_dir():
        return p
    for d in [p] + list(p.parents):
        if (d / ".project").is_dir():
            return d
    return None


def proj(root):
    return Path(root) / ".project"


# ---------- 跨进程互斥 ----------
# Registry 是「读整个 index.md → 改 → 写回」，next_id 还要读改写 .idseq.json。
# 多个 registry.py 同时跑（两个终端 / 脚本批量 / AI 并发调用）时，各自读到的都是同一份
# 旧快照，后写的把先写的整片覆盖。实测：6 个并发 add 全部 rc=0，末态只剩 1 条，
# .idseq.json 回退到 {"UI":1}，6 个进程都发出 UI-001——丢了数据却没有非零退出码，
# 这是最坏的失败模式：使用者会以为成功。
#
# 注意：拿「文件是否存在」当锁状态是**不成立**的（第一版就这么写，实测翻车）。
# 锁文件会在持有者之外残留，而残留物无法判断：当占用 → 后面全部卡死到超时；
# 无脑回收 → 可能把正在写的进程和另一个写者并到一起。正确做法是让操作系统当裁判：
# 对 .lock 加建议锁（POSIX flock / Windows msvcrt.locking），进程一死锁自动释放，
# 既没有残留也没有「回收」这个判断。锁文件是 <项目根>/.project.lock，常驻 1 字节，别删。
_LOCK_HELD = {}          # str(path) -> 本进程重入计数；同进程再取锁不算冲突


def lock_path(root):
    """锁文件放**项目根**，不放 .project/ 里面。

    放里面会撞上 `init --force --reset`：那条路径要 `shutil.rmtree(.project)`，
    而 Windows 删不掉正被本进程打开的文件 → PermissionError，reset 崩在清空这一步
    （骨架已删、重建未做）。放项目根就完全避开这次自我删除，也不会被 copytree
    复制进 .project.bak-*/ 备份里。
    """
    return Path(root) / ".project.lock"


def _os_try_lock(fd):
    """非阻塞抢建议锁：拿到 True；被别人占着 False。"""
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _os_unlock(fd):
    if os.name == "nt":
        import msvcrt
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


class FileLock:
    """写事务的跨进程互斥。拿不到就等，等不到就报错——绝不静默降级成无锁。"""

    def __init__(self, path, timeout=20.0, poll=0.05):
        self.path = Path(path)
        self.timeout = timeout
        self.poll = poll
        self.fd = None
        self.owned = False

    def acquire(self):
        key = str(self.path)
        if key in _LOCK_HELD:        # 同进程重入：不重复加锁，也不自我阻塞
            _LOCK_HELD[key] += 1
            self.owned = True
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR)
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"0")       # 建议锁要锁一个字节，先保证文件非空
            os.fsync(fd)
        deadline = time.monotonic() + self.timeout
        while True:
            if _os_try_lock(fd):
                self.fd = fd
                _LOCK_HELD[key] = 1
                self.owned = True
                return self
            if time.monotonic() >= deadline:
                os.close(fd)
                die(f"等锁超时（{self.timeout:.0f}s）：{self.path} 正被另一个 registry.py 占用。\n"
                    f"  并发写要排队，这是设计行为；若一直卡住，先确认没有别的 registry.py 没退出。")
            time.sleep(self.poll)

    def release(self):
        key = str(self.path)
        if not self.owned:
            return
        if key in _LOCK_HELD:
            _LOCK_HELD[key] -= 1
            if _LOCK_HELD[key] > 0:
                return
            _LOCK_HELD.pop(key, None)
        if self.fd is not None:
            _os_unlock(self.fd)
            os.close(self.fd)
            self.fd = None
        self.owned = False


def hold_lock(path):
    """取锁并挂到进程退出时释放。

    CLI 是一次性进程：命令结束 = 事务结束。用 atexit 而不是 with，让「加锁」只占一行，
    不必把每个命令体缩进一层；命令中途 die()（sys.exit）同样会走到 atexit。
    """
    lk = FileLock(path)
    lk.acquire()
    atexit.register(lk.release)
    return lk


def atomic_write(path, text):
    """先写同目录临时文件，再 os.replace 覆盖。

    write_text 会先截断再写，读者可能看到半截文件；并发时尤其明显。
    os.replace 在 Windows 与 POSIX 上都是原子的。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def today():
    return datetime.now().strftime("%Y-%m-%d")


def short_date():
    return datetime.now().strftime("%m-%d")


def _normalize_day(s):
    """注册表日期串 → `(年, 月, 日)` 三元组。两种写法都吃，认不出返回 None。

    **这一处是日期的唯一读法**，写与读的两侧都调它：
    - 写侧 `short_date()` 用的是 `%m-%d`（`09-25`，**没有年份**）—— 注册表
      「更新」列里躺的就是这种值；
    - 读侧原先只认 `%Y-%m-%d`，于是 `days_since` 对着自己写出来的 `09-25`
      恒返回 None —— 构思超期判定从未生效过一次。
    两边各写一份时，生产者与消费者对不上，而错误是**静默**的（不报错、
    不崩溃、退出码正常），只有把产物打开看才会发现。

    缺年份只能是推断不是还原：补当年，若补出来比今天还晚，那它只可能是
    去年的——「更新」记的是已经发生的动作，不存在未来日期。真躺了一年多
    的条目会差大约一年，但方向与量级仍对，远好过一声不响。
    """
    s = (s or "").strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            datetime(y, mo, d)
        except ValueError:
            return None
        return (y, mo, d)
    m = re.match(r"^(\d{1,2})-(\d{1,2})$", s)
    if not m:
        return None
    mo, d = int(m.group(1)), int(m.group(2))
    now = datetime.now()
    try:
        datetime(now.year, mo, d)
    except ValueError:
        return None
    if (mo, d) > (now.month, now.day):
        return (now.year - 1, mo, d)
    return (now.year, mo, d)


def days_since(s):
    """注册表日期串 → 距今几天。认不出返回 None，**不猜、也不当成 0**——
    把"不知道"当"刚碰过"会让超期检查静默失效，那比不报更糟。"""
    t = _normalize_day(s)
    if not t:
        return None
    return (datetime.now().date() - datetime(*t).date()).days


_IDEA_DAYS_RE = re.compile(r"^-\s*构思超期阈值：\s*(\d+)", re.M)


def idea_stale_days(p):
    """构思超期阈值（天）。优先级：PROJECT.md 的「- 构思超期阈值：N 天」> 内置默认。
    0 = 关闭超期升级（只保留 PLAN 提示行）。"""
    m = _IDEA_DAYS_RE.search(_read(Path(p) / "PROJECT.md"))
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return IDEA_STALE_DAYS


def stamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def copy_backup(p, tag=""):
    """备份 .project → 返回备份目录路径。
    stamp() 只到秒，同一秒内连跑两次 --force 会撞同一个名字，第二次直接
    FileExistsError [WinError 183] 中止——备份是安全动作，不该因为撞名而失败。
    撞了就自增序号重试（用 copytree 自己不覆盖（dirs_exist_ok=False）来判定）。"""
    for i in range(1, 51):
        name = f"{tag}{stamp()}" + (f"-{i}" if i > 1 else "")
        bak = Path(str(p) + ".bak-" + name)
        try:
            shutil.copytree(p, bak)
            return bak
        except FileExistsError:
            continue
    die(f"备份目录连撞 50 次名，先清理 {p}.bak-* 再试")


def tokens(s):
    s = (s or "").lower()
    t = set(re.findall(r"[a-z0-9]+", s))
    t.update(ch for ch in s if "\u4e00" <= ch <= "\u9fff")
    return t


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def norm(s):
    return re.sub(r"[\s_\-]+", "", (s or "").lower())


def canon_cycles(cycles):
    """把一个环的多种写法归一，再判重。

    同一个环会被从不同起点枚举出来（PG-001 -> PG-002 -> PG-001 与
    PG-002 -> PG-001 -> PG-002 是同一个环），不归一就会把一个环报成两个。
    归一办法：转成「最小 ID 打头」的闭环字符串。
    """
    out = set()
    for c in cycles:
        parts = [x for x in str(c).split(" → ") if x]
        if len(parts) > 1 and parts[0] == parts[-1]:
            ring = parts[:-1]
        else:
            ring = parts
        if not ring:
            continue
        k = min(range(len(ring)), key=lambda i: ring[i])
        rot = ring[k:] + ring[:k]
        out.add(" → ".join(rot + [rot[0]]))
    return sorted(out)


def _read(p):
    return p.read_text(encoding="utf-8") if Path(p).exists() else ""


def dw(s):
    """显示宽度：CJK 全角算 2，其余算 1。按 len() 补空格会让中文列全部错位。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in str(s))


def pad(s, n):
    """按显示宽度右补空格。超长时至少补一个空格——否则状态列会吃进语义名（粘连）。"""
    s = str(s)
    return s + " " * max(1, n - dw(s))


def slugify(s, n=24):
    """文件名 slug：保留中英数字，其余转连字符，去首尾与连续连字符。"""
    s = unicodedata.normalize("NFKC", str(s or ""))
    out = []
    for ch in s:
        if ch.isalnum() or "\u4e00" <= ch <= "\u9fff":
            out.append(ch)
        else:
            out.append("-")
    t = re.sub(r"-+", "-", "".join(out)).strip("-")
    return (t[:n].strip("-")) or "session"


def project_name(p, root):
    m = re.search(r"^#\s+(.+)$", _read(p / "PROJECT.md"), re.M)
    return m.group(1).strip() if m else Path(root).name


def scale_of(p):
    m = re.search(r"规模档位：\s*([LMF])", _read(p / "PROJECT.md"))
    return m.group(1) if m else "M"


def type_of(p):
    m = re.search(r"^-\s*项目类型：\s*(.+)$", _read(p / "PROJECT.md"), re.M)
    return m.group(1).strip() if m else ""


def rows_fingerprint(reg):
    """注册表内容指纹：用来判断 dependencies.md 是不是过期了。"""
    s = "\n".join("|".join(f"{k}={d.get(k, '')}" for k in COLUMNS) for _, _, d in reg.rows)
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


# ---------- 开环读写 ----------

def read_open_loops(p):
    """读 open-loops.md，返回 (未闭环 list[dict], 已闭环 list[list])。"""
    f = Path(p) / "memory" / "open-loops.md"
    if not f.exists():
        return [], []
    txt = _read(f)
    head, *rest = re.split(r"(?m)^## ", txt)
    closed = []
    for sec in rest:
        if sec.startswith("已闭环"):
            for ln in sec.splitlines():
                if ln.startswith("|") and not SEP_RE.match(ln) and "事项" not in ln:
                    c = [x.strip() for x in ln.strip().strip("|").split("|")]
                    if c and c[0]:
                        closed.append(c)
    loops = []
    for ln in head.splitlines():
        if ln.startswith("|") and not SEP_RE.match(ln) and "事项" not in ln:
            c = [x.strip() for x in ln.strip().strip("|").split("|")]
            if c and c[0]:
                loops.append({"item": c[0], "blocked": c[1] if len(c) > 1 else "",
                              "assets": c[2] if len(c) > 2 else "",
                              "date": c[3] if len(c) > 3 else ""})
    return loops, closed


def write_open_loops(p, name, loops, closed):
    L = [f"# 开环 · {name}", "",
         "| " + " | ".join(OL_COLS) + " |", "|" + "---|" * len(OL_COLS)]
    for l in loops:
        L.append(f"| {l['item']} | {l.get('blocked','')} | {l.get('assets','')} | {l.get('date','')} |")
    L += ["", "## 已闭环", "",
          "| 事项 | 卡在哪 | 关联资产 | 登记日 | 闭环日 |", "|---|---|---|---|---|"]
    for c in closed:
        L.append("| " + " | ".join(c) + " |")
    L.append("")
    (Path(p) / "memory" / "open-loops.md").parent.mkdir(parents=True, exist_ok=True)
    atomic_write(Path(p) / "memory" / "open-loops.md", "\n".join(L))


def parse_opens(s):
    """解析 `--opens "事项|卡在哪|关联资产"`（登记日自动填），返回三元组。
    曾经把整串塞进「事项」列：用竖线补另外两列会写出 7 个竖线的坏行（4 列应为 5），
    读回时 c[3] 越界、登记日被静默丢弃；而 add 是拒绝竖线的——同一个技能两套标准。
    这里把竖线收编成字段分隔符并逐段守一遍，超段直接报错而不是写坏表。"""
    parts = [x.strip() for x in str(s or "").split("|")]
    if len(parts) > 3:
        die('--opens 最多三段，用竖线分隔：--opens "事项|卡在哪|关联资产"（登记日自动填）')
    parts += [""] * (3 - len(parts))
    guard_fields(事项=parts[0], 卡在哪=parts[1], 关联资产=parts[2])
    if not parts[0]:
        die('--opens 第一段（事项）不能为空：--opens "事项|卡在哪|关联资产"')
    return parts[0], parts[1], parts[2]


def add_loop(p, item, blocked="", assets=""):
    """登记开环。同事项不重复追加——否则 reconcile 每跑一轮就堆一批同样的条目。"""
    if not item:
        return False
    loops, closed = read_open_loops(p)
    if any(l["item"] == item for l in loops):
        return False
    loops.append({"item": item, "blocked": blocked, "assets": assets, "date": today()})
    write_open_loops(p, project_name(p, Path(p).parent), loops, closed)
    return True


def loop_rank(l):
    """阻塞程度排序：卡住具体资产的排最前，其次有卡点的，同档按登记日早的优先。"""
    return (0 if not blank_field(l.get("assets")) else 1,
            0 if not blank_field(l.get("blocked")) else 1,
            l.get("date", ""))


# ---------- 注册表读写 ----------

class Registry:
    def __init__(self, root, lock=False):
        self.root = Path(root)
        self.path = proj(self.root) / "registry" / "index.md"
        if not self.path.exists():
            die(f"找不到 {self.path}，先跑：registry.py init --name <项目名> --type <类型>")
        # 锁必须在「读」之前拿：要保护的区间是 读 index.md → next_id → 写回。
        # 只锁 write 等于没锁——两个进程都读到同一份旧快照，后写的整片覆盖先写的。
        if lock:
            hold_lock(lock_path(self.root))
        self.lines = self.path.read_text(encoding="utf-8").splitlines()
        self.rows = []          # [(lineno, prefix, cells dict)]
        self.section_of = {}    # prefix -> 分节标题
        self.header_of = {}     # prefix -> 列顺序 list
        self.last_row = {}      # prefix -> 最后一行行号
        self._parse()

    def _parse(self):
        cur = None
        header = None
        for i, ln in enumerate(self.lines):
            m = SECTION_RE.match(ln)
            if m:
                cur = m.group(2)
                self.section_of[cur] = m.group(1)
                header = None
                continue
            m2 = BARE_SECTION_RE.match(ln)
            if m2:
                cur = m2.group(1)
                self.section_of.setdefault(cur, "")
                header = None
                continue
            if cur and ln.startswith("|"):
                cells = [c.strip() for c in ln.strip().strip("|").split("|")]
                if header is None:
                    if "ID" in cells:
                        header = cells
                        self.header_of[cur] = cells
                    continue
                if SEP_RE.match(ln) or not cells or not cells[0]:
                    continue
                d = dict(zip(header, cells))
                d["_cols"] = header
                self.rows.append((i, cur, d))
                self.last_row[cur] = i

    def ids(self):
        return {r[2]["ID"] for r in self.rows if r[2].get("ID")}

    def _infer_from_text(self, prefix, live):
        """游标缺失时的一次性冷启动推断：扫 .project 全文，孤立引用不算「发过」。

        判据：同一号在 ≥2 个不同文件里出现（真发过的资产会被会话/决策多处提到），
        或号 ≤ 现存行最大号（已被覆盖，不影响结果）。只出现一次的更大号视为笔误——
        它通常就是依赖列里手滑写出来的那个不存在的号。
        """
        pdir = proj(self.root)
        where = {}
        for f in pdir.rglob("*"):
            if not f.is_file() or f.suffix.lower() in (".png", ".jpg", ".ico", ".json"):
                continue
            try:
                t = f.read_text(encoding="utf-8")
            except Exception:
                continue
            for m in ID_RE.finditer(t):
                if m.group(1) == prefix:
                    where.setdefault(int(m.group(2)), set()).add(str(f))
        n = live
        for num, files in where.items():
            if num <= live or len(files) >= 2:
                n = max(n, num)
        return n

    def next_id(self, prefix):
        """发号：权威基数 = 现存行 ∪ 单调游标。文本提及不再参与发号。

        曾经把「.project 全文扫到的最大号」当基数——依赖列手滑写一个 UI-999，
        下一条就发成 UI-1000，游标被写成 {"UI":1000} 且不可逆。讽刺的是同一次
        check 明明已经报了「引用了未登记的 UI-999」：脚本知道那是笔误，却拿它当
        「该号用过」。游标才是权威，它只在真正发号时前进，删行不会让它回退。
        """
        live = 0
        for _, _, d in self.rows:
            m = ID_RE.fullmatch((d.get("ID") or "").strip())
            if m and m.group(1) == prefix:
                live = max(live, int(m.group(2)))
        sp = proj(self.root) / "registry" / ".idseq.json"
        seq = {}
        if sp.exists():
            try:
                seq = json.loads(sp.read_text(encoding="utf-8"))
            except Exception:
                seq = {}
        if prefix in seq:
            base = max(live, int(seq[prefix]))
        else:
            base = max(live, self._infer_from_text(prefix, live))
            if base > live and base > 0:
                print(f"  ⓘ 前缀 {prefix} 首次建游标：从历史文本推断最大号 {base}"
                      f"（现存行最大 {live}）")
        if base + 1 > MAX_ID:
            die(f"{prefix} 的号已到上限 {MAX_ID}，发不出新号。接手既有项目时先用 "
                f"registry.py idseq --set {prefix} <正确号> 把游标修正回真实值。")
        seq[prefix] = base + 1
        atomic_write(sp, json.dumps(seq, ensure_ascii=False, indent=2) + "\n")
        return f"{prefix}-{base + 1:03d}"

    def save(self):
        atomic_write(self.path, "\n".join(self.lines) + "\n")
        self.path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")

    def insert(self, prefix, cells_dict, title=""):
        """插入一行，返回是否新建了分节。

        title 非空 → `## 维度名 (PFX)`；否则退化成裸分节（**不写 `## CH (CH)`**——
        标题等于前缀等于没写）。add 会把已知的维度名传进来：前缀来自 naming.md §四
        或内置维度表时标题是现成的，不必等人事后去改 index.md。
        """
        created = False
        if prefix in self.last_row:
            at = self.last_row[prefix] + 1
        elif prefix in self.section_of:
            # 分节存在但无数据行：紧贴分隔线之后插入
            # 注意不能按「表头 +3」算：init 模板在分隔线后有空行，+3 会把行插到空行之后，
            # GFM 表格在空行处断开，整张表退化成裸文本。以分隔线位置为准才稳。
            at = len(self.lines)
            for i, ln in enumerate(self.lines):
                m = SECTION_RE.match(ln)
                if m and m.group(2) == prefix:
                    j = i + 1
                    while j < len(self.lines) and not self.lines[j].startswith("## "):
                        if self.lines[j].startswith("| ID"):
                            at = j + 2  # 表头 j，分隔线 j+1 → 插在 j+2
                            break
                        j += 1
                    break
        else:
            head = f"## {title.strip()} ({prefix})" if title.strip() else f"## {prefix}"
            self.lines.append(f"\n{head}\n")
            self.lines.append("| " + " | ".join(COLUMNS) + " |")
            self.lines.append("|" + "---|" * len(COLUMNS))
            at = len(self.lines)
            created = True
        cols = self.header_of.get(prefix, COLUMNS)
        row = "| " + " | ".join(str(cells_dict.get(c, "")) for c in cols) + " |"
        self.lines.insert(at, row)
        # 必须更新 last_row：否则同一个 Registry 实例里连续插多行（--force 迁回、
        # 批量登记）会全部落在同一个锚点，后插的排到前面——迁回顺序被整体反转。
        self.last_row[prefix] = at
        return created

    def update(self, rid, changes):
        """改一行（只改单元格，不动行号）。

        返回 (前缀, 改前的值 dict)；找不到返回 (None, None)。
        旧值必须在 update 之前快照——调用方拿到的往往是 self.rows 里同一个 dict 对象，
        就地 update 之后再读「旧值」读到的其实是新值，于是 set 的反馈永远印成
        「状态：可用 → 可用」。状态机唯一的反馈失真就是这么来的。
        """
        for i, pfx, d in self.rows:
            if d.get("ID") != rid:
                continue
            cols = d.get("_cols", COLUMNS)
            old = dict(d)
            d.update(changes)
            self.lines[i] = "| " + " | ".join(str(d.get(c, "")) for c in cols) + " |"
            return pfx, old
        return None, None


# ---------- 命令 ----------

def split_types(spec):
    """混合类型：`--type "code-ui+writing-content"` → ['code-ui', 'writing-content']。

    文档承诺了「同一项目可混合类型，前缀空间共享」，退化成单一 ITEM 等于没兑现。
    """
    parts = [x.strip() for x in re.split(r"[+＋,，/|&]+", spec or "") if x.strip()]
    return parts or (["custom"] if not spec else [spec])


def parse_dims(spec):
    """解析 `--dims "UI:界面元素,MOD:功能模块"` → [("UI","界面元素"), ("MOD","功能模块")]。

    切分口径与 build_sections 原先内联的完全一致（中英文逗号/分号 + 中英文冒号）。
    抽出来是为了 init 与 add 共用一处——否则「index.md 建了节、naming.md 没写」这类
    漂移又会从两份正则开始。
    """
    out = []
    for item in re.split(r"[,，;；]+", spec or ""):
        if not item.strip():
            continue
        parts = re.split(r"[:：]", item, maxsplit=1)
        pfx = parts[0].strip().upper()
        if not PREFIX_RE.match(pfx):
            continue
        out.append((pfx, parts[1].strip() if len(parts) > 1 else ""))
    return out


def read_naming_dims(root):
    """读 `registry/naming.md` §四「自定义维度」表 → {前缀: 维度名}。

    填的是一个洞：naming.md 曾经是「只写不读」的文件。文档三处（custom.md /
    naming.md.tpl / SKILL.md）都说自定义维度的归属地是它，但 registry.py 全文只有
    cmd_init 那一处 write_text，**没有任何读者**——于是 §四 是一张死表（填了不起
    任何作用），而 `add --type ZZ` 会静默长出 `## ZZ` 裸分节。这里让它真正成为
    前缀权威源之一。
    """
    p = proj(Path(root)) / "registry" / "naming.md"
    if not p.exists():
        return {}
    out, inside = {}, False
    for ln in p.read_text(encoding="utf-8").splitlines():
        if ln.startswith("## "):
            inside = "自定义维度" in ln
            continue
        if not inside or not ln.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        pfx = cells[0].strip("`").strip().upper()
        if not PREFIX_RE.match(pfx) or pfx in PLACEHOLDER_PREFIX:
            continue
        out[pfx] = cells[1].strip("`").strip()
    return out


def sync_naming_dim(root, prefix, title):
    """把 (前缀, 维度名) 补进 naming.md §四 的表；已有该前缀则不动（只补不覆盖）。

    `init --dims` 原先只写 index.md，naming.md §四 一直是模板里那张空表——「三处定义
    漏一处」正是 §四 变成死表的另一半原因。现在两处一起写，同源。
    """
    p = proj(Path(root)) / "registry" / "naming.md"
    if not p.exists():
        return False
    lines = p.read_text(encoding="utf-8").splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith("## ") and "自定义维度" in ln:
            start = i
            break
    row = f"| {prefix} | {title} |  |  |"
    if start is None:
        lines += ["", "## 四、自定义维度", "",
                  "| 前缀 | 维度 | 什么东西算一个 | 字段提示 |",
                  "|---|---|---|---|", row, ""]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    if any(re.match(r"^\|\s*`?" + re.escape(prefix) + r"`?\s*\|", ln)
           for ln in lines[start:end]):
        return False
    last = None
    for j in range(start, end):
        if lines[j].lstrip().startswith("|"):
            last = j
    if last is None:
        lines[end:end] = ["", "| 前缀 | 维度 | 什么东西算一个 | 字段提示 |",
                          "|---|---|---|---|", row]
    elif not "".join(c.strip() for c in lines[last].strip().strip("|").split("|")):
        lines[last] = row          # 模板里那行空占位直接用掉，别再往旁边堆一行
    else:
        lines.insert(last + 1, row)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def _read_type_dims(f):
    """读一张内置类型维度表 → [(前缀, 维度名)]。三处共用一处解析，免得漂移从三份正则开始。"""
    out = []
    for ln in f.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\|\s*`([A-Z][A-Z0-9_-]{0,5})`\s*\|\s*([^|]+?)\s*\|", ln)
        if not m:
            continue
        pfx, title = m.group(1), m.group(2).split("，")[0].split("(")[0].strip()
        # 文档里的示例行（XXX / 全是省略号）不是真维度
        if pfx in PLACEHOLDER_PREFIX or set(title) <= set(".…· "):
            continue
        out.append((pfx, title))
    return out


def all_builtin_prefixes():
    """四张内置类型表的并集——只用于**提示**（告诉用户「它在别家合法」），绝不用于判定。"""
    out = {}
    if TYPES_DIR.exists():
        for f in sorted(TYPES_DIR.glob("*.md")):
            for pfx, title in _read_type_dims(f):
                out.setdefault(pfx, title)
    return out


def scoped_builtin_prefixes(root):
    """C 口径：单一类型的项目只认自己那张表；custom / 混合 / 类型未识别 → 保留全集。

    类型解析**必须**复用 split_types() 与 init 第 969 行那套 exists() 过滤，绝不在第二处
    重写。一旦两边口径分岔，就会出现「init 按 custom 建了节、add 却按别的表判它非法」
    的自相矛盾——那比不过滤还糟。

    为什么 custom 与混合类型回落到全集：
    ① custom 恰恰是「我不知道该用什么维度」的人，过滤掉等于抽掉他唯一的参考表；
    ② 混合类型的前缀空间本就设计为共享并集（见 build_sections 的注释）；
    ③ 类型识别不出来时宁松不误杀——与依赖闸门「不回头算存量」同一条原则。
    """
    parts = split_types(type_of(proj(Path(root))))
    hit = [t for t in parts if (TYPES_DIR / f"{t}.md").exists()]
    if len(hit) != 1 or "custom" in hit:
        return all_builtin_prefixes()          # 兜底档：全集
    return dict(_read_type_dims(TYPES_DIR / f"{hit[0]}.md"))


def defined_prefixes(root):
    """本项目「已定义」的前缀全集——前缀闸门的唯一权威源，三个来源缺一不可。

    ① index.md 现有分节：凡在本项目建过节的**一律合法**。这条刻意留着：它保证新闸门
       只拒绝「从没在本项目出现过」的前缀，**不审判历史**——否则老项目第二天早上就
       干不了活（与依赖闸门「不回头算存量」同一条原则）。
    ② naming.md §四：文档指定的自定义维度归属地（此前无人读 → 死表）。
    ③ 技能内置类型维度表：UI/API/MOD/… 开箱即用。**单一类型**的项目只加载自己那张表
       （见 scoped_builtin_prefixes）；custom / 混合类型 / 类型未识别保留全集。
    """
    out = {}
    idx = proj(Path(root)) / "registry" / "index.md"
    if idx.exists():
        for ln in idx.read_text(encoding="utf-8").splitlines():
            m = SECTION_RE.match(ln)
            if m:
                out.setdefault(m.group(2), m.group(1))
                continue
            m = BARE_SECTION_RE.match(ln)
            if m:
                out.setdefault(m.group(1), "")
    for pfx, title in read_naming_dims(root).items():
        out.setdefault(pfx, title)
    for pfx, title in scoped_builtin_prefixes(root).items():
        out.setdefault(pfx, title)
    return out


def nearest_prefix(pfx, pool, k=2):
    """给写错的前缀找最近邻，用于「是不是想写 UI？」。

    阈值随长度收紧：短前缀（≤3 位）只认编辑距离 1 —— 否则 `ZZ` 会"最近"到 `CH`/`DM`
    （两位全换也不过距离 2），建议立刻变成噪声。真错拼（`UII`→`UI`）基本都是距离 1。
    """
    def ed(a, b):
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            cur = [i]
            for j, cb in enumerate(b, 1):
                cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
            prev = cur
        return prev[-1]
    limit = 1 if len(pfx) <= 3 else 2
    cand = sorted(pool, key=lambda x: (ed(pfx, x), x))
    return [x for x in cand[:k] if ed(pfx, x) <= limit]


def build_sections(tpl_types, dims=""):
    """生成 index.md 的分节。custom 类型默认只给通用 ITEM，不产出 `## ... (XXX)` 垃圾分节。"""
    def sec(title, pfx):
        # 没有中文标题就只写前缀，别自指成 `## CH (CH)`
        head = f"## {title} ({pfx})" if title.strip() else f"## {pfx}"
        return (f"{head}\n\n"
                f"| " + " | ".join(COLUMNS) + " |\n"
                f"|" + "---|" * len(COLUMNS) + "\n")

    out, seen_pfx = [], set()

    def add(title, pfx):
        if pfx in seen_pfx:      # 混合类型共享前缀空间，同前缀只建一次
            return
        seen_pfx.add(pfx)
        out.append(sec(title, pfx))

    # 切分口径抽到 parse_dims 单点定义：init 与 add 共用一处，免得「建了节没写
    # naming.md」这类漂移从两份正则开始。
    for pfx, title in parse_dims(dims):
        add(title, pfx)
    # --dims 是「补」不是「替换」：文档三处（SKILL.md / modules/01 / init 的「未识别的
    # 类型」提示）都说它用来把没进表的维度补上。所以类型维度照常入表，两者取并集、
    # 同前缀去重（由上面的 add() 保证，先登记的标题胜出，即 --dims 的标题优先）。
    for tpl_type in tpl_types:
            dim = TYPES_DIR / f"{tpl_type}.md"
            if not dim.exists():
                continue
            for pfx, title in _read_type_dims(dim):
                add(title, pfx)
    if not out:
        add("资产", "ITEM")
    return out


def cmd_init(a):
    root = Path(a.root).resolve() if a.root else Path.cwd().resolve()
    # 混合类型（"code-ui+writing-content"）取两型维度的并集，不退化成单一 ITEM
    tpl_parts = split_types(a.type)
    tpl_types = [t for t in tpl_parts if (TYPES_DIR / f"{t}.md").exists()] or ["custom"]
    unknown = [t for t in tpl_parts if t not in tpl_types]
    p = proj(root)
    existed = p.exists()

    if existed and not a.force:
        die(f"{p} 已存在（加 --force 重建骨架并备份；--force --reset 才清空全部）")
    hold_lock(lock_path(root))      # 两个 init 同时跑会争同一批骨架文件

    old_rows = []
    if existed:
        if not a.reset:
            bak = copy_backup(p)
            print(f"⚠ 已备份既有 .project → {bak.name}（--force 不会动 memory/，资产行会原样迁回）")
        if (p / "registry" / "index.md").exists():
            r0 = Registry(str(root), lock=True)
            old_rows = [(pfx, {k: v for k, v in d.items() if not k.startswith("_")})
                        for _, pfx, d in r0.rows]
        if a.reset:
            bak = copy_backup(p, "reset-")
            print(f"⚠ --reset：整个 .project 已备份到 {bak.name}，即将清空（含 memory/）")
            if (p / "registry" / ".idseq.json").exists():
                print("⚠ 连 .idseq.json 一起删：发号游标归零，旧 ID 可能被复用。"
                      "这是「ID 永不回收」的唯一例外；要保号请用 --force（不带 --reset），"
                      f"或在 reset 后用 registry.py idseq --set <前缀> <原最大号> 补回。")
            shutil.rmtree(p)

    (p / "registry").mkdir(parents=True, exist_ok=True)
    (p / "memory").mkdir(parents=True, exist_ok=True)
    if a.scale in ("M", "F"):
        (p / "memory" / "sessions").mkdir(exist_ok=True)
        (p / "memory" / "decisions").mkdir(exist_ok=True)

    def tpl(name, **kw):
        t = (TPL_DIR / name).read_text(encoding="utf-8")
        for k, v in kw.items():
            t = t.replace("{" + k + "}", str(v))
        return t

    (p / "PROJECT.md").write_text(
        tpl("PROJECT.md.tpl", NAME=a.name, TYPE=a.type, SCALE=a.scale, DATE=today()),
        encoding="utf-8")

    sections = build_sections(tpl_types, a.dims or "")
    (p / "registry" / "index.md").write_text(
        tpl("index.md.tpl", NAME=a.name, TYPE=a.type, SCALE=a.scale, DATE=today(),
            SECTIONS="\n".join(sections)),
        encoding="utf-8")

    # 既有的资产行原样迁回（--force 重建骨架，不等于清空账本）
    if old_rows and not a.reset:
        r1 = Registry(str(root), lock=True)
        for pfx, cells in old_rows:
            r1.insert(pfx, cells)
        r1.save()
        print(f"  已迁回 {len(old_rows)} 条既有资产")

    # naming.md 是用户手写的约定，覆盖它就等于删掉项目记忆
    nm = p / "registry" / "naming.md"
    if not nm.exists():
        nm.write_text(tpl("naming.md.tpl", NAME=a.name), encoding="utf-8")

    # --dims 的维度同步进 naming.md §四：原先只写 index.md，两处漏一处——§四 一直是模板
    # 里那张空表，而它恰恰是文档指定的「自定义维度归属地」。现在两处同源。
    for _pfx, _title in parse_dims(a.dims or ""):
        sync_naming_dim(root, _pfx, _title)

    if a.scale == "F":
        (p / "registry" / "entities").mkdir(parents=True, exist_ok=True)

    log = p / "memory" / "log.md"
    if a.scale == "L" and not log.exists():
        log.write_text(f"# 记忆流水 · {a.name}\n\n> L 档轻量模式：会话与决策混记于此。\n\n",
                       encoding="utf-8")
    # 开环不是中高档的特权：L 档（资产<15 / 单人 / 短周期）恰恰是最容易「下次从哪接」
    # 说不清的场景。档位只简化目录结构（L 档不拆 sessions/ 与 decisions/），不砍能力。
    ol = p / "memory" / "open-loops.md"
    if not ol.exists():
        write_open_loops(p, a.name, [], [])
    cp = p / "memory" / "context-packet.md"
    if not cp.exists():
        cp.write_text(f"# 上下文包 · {a.name}\n\n> 由 `registry.py context` 生成，勿手改。\n",
                      encoding="utf-8")

    print(f"已初始化 {p}")
    print(f"  类型={a.type} 规模={a.scale}" + (f" 维度={a.dims}" if a.dims else ""))
    if len(tpl_types) > 1:
        print(f"  混合类型：{' + '.join(tpl_types)} 的维度已合并（{len(sections)} 个分节，同前缀去重）")
    if unknown:
        print(f"  ⚠ 未识别的类型 {unknown}：维度没进表，用 --dims \"PFX:名称\" 补"
              f"（可选项见 references/project-types/）")
    if a.scale == "F":
        cmd_graph(argparse.Namespace(root=str(root)))
        print("  F 档增量已就位：registry/entities/（每资产一卡）+ registry/dependencies.md（依赖图）")
    print("  下一步：填 PROJECT.md 的六字段，然后 registry.py add 登记资产。")


def cmd_add(a):
    root = a.root or find_root()
    reg = Registry(root, lock=True)
    prefix = a.type.upper()
    if not PREFIX_RE.match(prefix):
        die(f"前缀不合法：{prefix}（2–6 位大写字母，可用 init --dims 定义）")
    if a.status not in VALID_STATUS:
        die(f"状态必须是：{' / '.join(sorted(VALID_STATUS))}")
    guard_fields(语义名=a.name, 别名=a.alias, 位置=a.path, 依赖=a.deps, 决策=a.decision)

    # —— 前缀闸门：命名闸门是第一道，它的缺口最贵 ——
    # 未定义的前缀会建出 `## ZZ` 裸分节，而 check 照样认它 —— 等于命名空间没有权威。
    # 这与「手滑写 UI-999 把游标抬飞」是同一个洞（未定义的能进系统）的两种表现。
    # 例外出口 --force；就地定义用 --dims（同时写进 naming.md §四，让它不再只写不读）。
    declared = ""
    for _dpfx, _dtitle in parse_dims(getattr(a, "dims", "") or ""):
        if _dpfx != prefix:
            die(f"--dims 的 {_dpfx} 与 --type {prefix} 不一致：add 一次只登记一个前缀")
        declared = _dtitle
    known = defined_prefixes(root)
    title = declared or known.get(prefix, "")
    if prefix not in known and not declared:
        if not a.force:
            near = nearest_prefix(prefix, known)
            print(f"前缀闸门：{prefix} 在本项目未定义。已定义：{' / '.join(sorted(known))}")
            if near:
                print(f"  是不是想写 {' 或 '.join(near)}？")
            # 过滤生效时最常见的困惑：明明是内置前缀却说「未定义」。必须先解释它从哪来、
            # 为什么不算数，否则用户只会加 --force 绕过，闸门白收紧。
            if prefix in all_builtin_prefixes():
                print(f"  {prefix} 是别的项目类型的内置前缀（本项目类型："
                      f"{type_of(proj(Path(root))) or '未标注'}）——本项目没启用它。"
                      f"跨类型借用请显式声明，别让它悄悄进命名空间。")
            print(f"  未定义的前缀会建出 `## {prefix}` 裸分节，而它照样能被 check 认下——"
                  f"命名空间就没了权威。三条出路：")
            print(f"  ① 就地定义：add --type {prefix} --dims \"{prefix}:维度名\" ...")
            print(f"  ② 在 registry/naming.md §四 填一行（前缀 / 维度 / 什么东西算一个）")
            print(f"  ③ 确属例外 → --force 放行（会回显）")
            sys.exit(2)
        # 例外不能静默：放行了什么、会留下什么，必须说出来
        print(f"  ⚠ 前缀闸门放行：{prefix} 在本项目未定义（--force）——"
              f"它会建出 `## {prefix}` 裸分节。建议补 naming.md §四 或改用 --dims。")

    # —— 查重三查：同名 / 同义 / 同职责（模糊）——
    # 中文按单字切分，短名共用前缀极易假阳性（「章节甲」vs「章节乙」= 0.5）。
    # 因此要求：交集 ≥ 2 个 token 且 Jaccard ≥ 0.6——低于此线的只算"碰巧像"，拦了会误伤。
    def fuzzy(s1, s2):
        # 限定词（新 / 版 / 优化 / 临时 / 测试 …）不改变所指，比较前先剥掉，
        # 否则「新的持仓卡片」vs「持仓资产卡片」会被稀释到阈值以下，漏过闸门。
        x = tokens(s1) - QUALIFIER_CHARS
        y = tokens(s2) - QUALIFIER_CHARS
        if len(x & y) < 2:
            return 0.0
        return jaccard(x, y)

    nt = tokens(a.name)
    na = norm(a.name)
    exact, near = [], []
    for _, p, d in reg.rows:
        name = d.get("语义名", "")
        alias = d.get("别名", "")
        if norm(name) == na or (a.path and d.get("位置", "").strip() == a.path.strip()):
            exact.append(d)
            continue
        s = max(fuzzy(a.name, name), fuzzy(a.name, alias))
        if s >= 0.6:
            near.append((s, d))
    if exact and not a.force:
        print("查重命中【同名 / 同位置】，按闸门不得另起 ID：")
        for d in exact:
            print(f"  {d['ID']}  {d.get('语义名','')}  <{d.get('位置','')}>  [{d.get('状态','')}]")
        print("出路：复用它 / 与它合并 / 加 --force 并在决策卡写明差异。")
        sys.exit(2)
    if near and not a.force:
        print("查重疑似【同义 / 同职责】，先确认再发 ID：")
        for s, d in sorted(near, reverse=True)[:5]:
            print(f"  [{s:.0%}] {d['ID']}  {d.get('语义名','')}  别名={d.get('别名','')}  [{d.get('状态','')}]")
        print("确认无重复 → 加 --force 放行，并在决策卡写明与它的差异。")
        sys.exit(2)

    # 禁用词是真闸门，不是提示。只提示等于把判断推给未来的某次 reconcile，
    # 而那时资产已经落地、引用已经铺开，改名的成本比现在高一个数量级。
    hit = bad_name(a.name)
    if hit and not a.force:
        print(f"命名闸门：语义名「{a.name}」命中禁用词 {hit} —— 改写后再登记：")
        print("  版本演进用状态与决策表达，不用名字表达（要换就新发 ID，旧的标 废弃 + 写决策卡）。")
        print("  确属例外 → 加 --force 放行，并在决策卡写明为什么必须用这个词。")
        sys.exit(2)

    # 依赖闸门在发号之前：写错的依赖不该先占掉一个号再回滚。
    gate_deps("", a.deps, reg.ids(), allow=getattr(a, "allow_dangling_deps", False))

    new_id = reg.next_id(prefix)
    created_section = reg.insert(prefix, {
        "ID": new_id,
        "语义名": a.name,
        "别名": a.alias or "",
        "位置": a.path or "",
        "状态": a.status,
        "依赖": a.deps or "",
        "决策": a.decision or "",
        "更新": short_date(),
    }, title=title)
    reg.save()
    # naming.md §四 的写入放在落盘之后：一次失败的 add 不该在命名约定里留痕——
    # 「声明了 --dims、但后面被依赖闸门拦下」是真会发生的（pr33 夹具就是这么造的）。
    if declared:
        sync_naming_dim(root, prefix, declared)
    if created_section:
        if title.strip():
            print(f"  ⓘ 新前缀 {prefix} 已建节：`## {title.strip()} ({prefix})`")
        else:
            print(f"  ⓘ 新前缀 {prefix} 已建节（`## {prefix}`）——"
                  f"建议编辑 index.md 把标题改成中文，或用 --dims \"{prefix}:维度名\" 定义")
    if scale_of(proj(root)) == "F":
        write_entity_card(proj(root), new_id, prefix, {
            "NAME": a.name, "TYPE": prefix, "STATUS": a.status,
            "PATH": a.path or "", "ALIAS": a.alias or "",
            "DEPS": a.deps or "", "DECISION": a.decision or "", "DATE": today(),
        })
        print(f"  F 档：已开详卡 registry/entities/{new_id}.md")
    print(f"已登记 {new_id}  {a.name}  [{a.status}]")
    if a.path:
        print(f"  位置：{a.path}")
    print("  下一步：若这是非显然的决定，补一张决策卡 memory/decisions/ADR-NNN-*.md")


def cmd_set(a):
    """改资产字段：状态机 / 别名收敛 / 位置修正都走这里。ID 永不改。"""
    root = a.root or find_root()
    reg = Registry(root, lock=True)
    rid = a.id.upper()
    changes = {}
    for k, v in [("状态", a.status), ("语义名", a.name), ("别名", a.alias),
                 ("位置", a.path), ("依赖", a.deps), ("决策", a.decision)]:
        if v is not None:
            changes[k] = v
    if not changes:
        die("没给任何要改的字段（--status / --name / --alias / --path / --deps / --decision）")
    if a.status and a.status not in VALID_STATUS:
        die(f"状态必须是：{' / '.join(sorted(VALID_STATUS))}")
    guard_fields(**changes)
    cur = next((d for _, _, d in reg.rows if d.get("ID") == rid), None)
    if cur is None:
        die(f"没有 {rid} 这个资产（registry.py list 看全部）")
    # 只校验**本次要写入的值**，不回头校验存量：否则改个状态也会被历史遗留的悬空依赖卡住，
    # 那就成了「旧账变成今天的门禁」——存量由 check --strict-deps 单独清。
    if "依赖" in changes:
        gate_deps(rid, changes["依赖"], reg.ids(), allow=getattr(a, "allow_dangling_deps", False))
    before = dict(cur)   # 快照：reg.update 会就地改同一个 dict 对象
    changes["更新"] = short_date()
    reg.update(rid, changes)
    reg.save()
    print(f"已更新 {rid}  {before.get('语义名','')}")
    for k, v in changes.items():
        if k == "更新":
            continue
        old = before.get(k, "") or "（空）"
        print(f"  {k}：{old} → {v}" + ("  （无变化）" if old == v else ""))
    if a.status == "废弃":
        print("  已废弃：ID 不回收，历史引用仍然指向它（想彻底替掉请新开 ID + 决策卡）")


def cmd_retire(a):
    """废弃一个资产：状态置「废弃」，原因写进详卡变更记录。ID 保留。"""
    root = a.root or find_root()
    reg = Registry(root, lock=True)
    rid = a.id.upper()
    pfx, old = reg.update(rid, {"状态": "废弃", "更新": short_date()})
    if pfx is None:
        die(f"没有 {rid} 这个资产")
    print(f"  状态：{old.get('状态') or '（空）'} → 废弃")
    reg.save()
    ent = proj(root) / "registry" / "entities" / f"{rid}.md"
    if ent.exists():
        t = ent.read_text(encoding="utf-8")
        t = t.rstrip() + f"\n- {today()} 废弃：{a.reason}\n"
        ent.write_text(t, encoding="utf-8")
        print(f"  详卡变更记录已追加 → {ent.name}")
    print(f"已废弃 {rid}（ID 保留不回收）原因：{a.reason}")
    print("  补一张决策卡说明替代方案，比直接删行有用——后人会问「为什么没有它」。")


def cmd_find(a):
    reg = Registry(a.root or find_root())
    q = a.query
    nt = tokens(q)
    nq = norm(q)
    hits = []
    for _, p, d in reg.rows:
        if a.type and p != a.type.upper():
            continue
        name, alias = d.get("语义名", ""), d.get("别名", "")
        blob = " ".join(str(v) for k, v in d.items() if not k.startswith("_"))
        if nq and nq in norm(blob):
            hits.append((1.0, d))
            continue
        # 只在「语义名 + 别名」上做相似度，避免被路径等噪声字段稀释
        s = max(jaccard(nt, tokens(name)), jaccard(nt, tokens(alias)))
        if s >= 0.34:
            hits.append((s, d))
    if not hits:
        print(f"未命中「{q}」——可发新 ID，但仍需人工过一遍【同职责查】。")
        return
    print(f"命中 {len(hits)} 条（按相关度）：")
    for s, d in sorted(hits, key=lambda x: -x[0])[:20]:
        print(f"  [{s:.0%}] {pad(d['ID'], 10)}{pad(d.get('语义名',''), 20)}"
              f"别名={d.get('别名','')} [{d.get('状态','')}] {d.get('位置','')}")


def cmd_list(a):
    reg = Registry(a.root or find_root())
    n = 0
    for _, p, d in reg.rows:
        if a.type and p != a.type.upper():
            continue
        if a.status and d.get("状态", "") != a.status:
            continue
        n += 1
        print(f"  {pad(d['ID'], 10)}{pad(d.get('语义名',''), 22)}"
              f"[{d.get('状态','')}] {d.get('位置','')}")
    print(f"— 共 {n} 条")


def cmd_loop(a):
    """开环闭环：只增不减会让 open-loops 变成垃圾场，必须有闭合动作。"""
    root = a.root or find_root()
    p = proj(root)
    hold_lock(lock_path(root))      # open-loops.md 是读-改-写，闭环与登记并发会互相覆盖
    loops, closed = read_open_loops(p)
    if a.done is None and not a.list:
        a.list = True
    if a.list:
        if not loops:
            print("开环为空 ✅（已闭环 " + str(len(closed)) + " 条）")
        else:
            print(f"开环 {len(loops)} 条（按阻塞程度）：")
            for i, l in enumerate(sorted(loops, key=loop_rank), 1):
                tail = "  ".join(x for x in [f"资产={l['assets']}" if not blank_field(l["assets"]) else "",
                                             f"卡点={l['blocked']}" if not blank_field(l["blocked"]) else "",
                                             l["date"]] if x)
                print(f"  {i}. {l['item']}" + (f"  [{tail}]" if tail else ""))
        return
    key = str(a.done).strip()
    idx = None
    if key.isdigit():
        k = int(key)
        ranked = sorted(loops, key=loop_rank)
        if 1 <= k <= len(ranked):
            idx = loops.index(ranked[k - 1])
    if idx is None:
        for i, l in enumerate(loops):
            if key in l["item"]:
                idx = i
                break
    if idx is None:
        die(f"没找到开环「{key}」（registry.py loop --list 看编号）")
    done = loops.pop(idx)
    closed.append([done["item"], done.get("blocked", ""), done.get("assets", ""),
                   done.get("date", ""), today()])
    write_open_loops(p, project_name(p, root), loops, closed)
    print(f"已闭环：{done['item']}")
    print(f"  开环剩 {len(loops)} 条 / 已闭环 {len(closed)} 条（历史保留，不删）")


def cmd_check(a):
    root = a.root or find_root()
    if not root:
        die("未找到 .project，先 init")
    p = proj(root)
    reg = Registry(root)
    ptype = type_of(p)
    errs, warns = [], []
    seen = {}
    planned_pending = []        # 构思中、位置还没落盘的资产（提示，不算错）
    planned_stale = []          # 其中挂了太久的：PLAN 升 WARN（阈值 idea_stale_days）
    idea_days = getattr(a, "idea_days", None)
    if idea_days is None:
        idea_days = idea_stale_days(p)
    status_of = {d.get("ID", ""): (d.get("状态", "") or "").strip() for _, _, d in reg.rows}
    for _, pfx, d in reg.rows:
        rid = d.get("ID", "")
        if rid in seen:
            errs.append(f"ID 重复：{rid}")
        seen[rid] = True
        if d.get("状态", "") not in VALID_STATUS:
            errs.append(f"{rid} 状态非法：{d.get('状态','')}")
        st = (d.get("状态") or "").strip()
        loc = (d.get("位置") or "").strip()
        on_disk = (bool(loc) and loc != "-"
                   and not loc.startswith(("http://", "https://")))
        landed = on_disk and (Path(root) / loc).exists()
        if st == PLANNED_STATUS:
            # 构思：位置是意向，还没落盘，不算错——进末尾的「构思中未落盘」汇总。
            if not landed:
                planned_pending.append(rid)
                age = days_since(d.get("更新", ""))
                if idea_days and age is not None and age >= idea_days:
                    planned_stale.append((rid, age))
        elif on_disk and not landed:
            errs.append(f"{rid} 位置不存在：{loc}")
        for m in ID_RE.finditer(" ".join(str(v) for k, v in d.items() if not k.startswith("_"))):
            ref = m.group(0)
            if ref == rid or m.group(1) == "ADR" or ref in reg.ids():
                continue
            warns.append(f"{rid} 引用了未登记的 {ref}")
        # 状态机闸门：「允许被依赖」列的 ❌ 必须真的被卡住，不能只是文档里画个叉。
        # 依赖了废弃资产 = 依赖链断了（错）；依赖了构思资产 = 上游没落盘（警）。
        for m in ID_RE.finditer(d.get("依赖", "") or ""):
            tgt = m.group(0)
            if tgt == rid or m.group(1) == "ADR":
                continue
            st = status_of.get(tgt)
            if st == "废弃":
                errs.append(f"{rid} 依赖了已废弃的 {tgt}：依赖链断了，改指新资产或写决策卡说明")
            elif st == "构思":
                warns.append(f"{rid} 依赖了还在构思的 {tgt}：上游没落盘，这条依赖现在是空的")
        # 硬闸门：research 的结论必须挂在问题或发现上
        if "research" in ptype and pfx == "CON":
            deps = d.get("依赖", "") or ""
            if not re.search(r"\b(Q|FND)-\d{2,4}", deps):
                warns.append(f"{rid} 是 CON 但没挂 Q / FND，结论悬空")
    # 构思超期：PLAN 升 WARN。**只升 WARN、不进 errs**——「先登记、过几天动手」是正常节奏，
    # 卡住它就等于逼人不敢登记；要抓的只是「登记完就忘了」。
    for rid, age in planned_stale:
        warns.append(f"构思超期未落盘：{rid} 已挂 {age} 天（阈值 {idea_days} 天）"
                     f"—— 落盘就 `set {rid} --status 在建`，不做就写决策卡说明并 retire")
    dec_dir = p / "memory" / "decisions"
    for m in ADR_RE.finditer(" ".join(
            " ".join(str(v) for k, v in d.items() if not k.startswith("_"))
            for _, _, d in reg.rows)):
        if not list(dec_dir.glob(f"ADR-{int(m.group(1)):03d}-*.md")):
            warns.append(f"引用了 ADR-{int(m.group(1)):03d} 但文件不存在")
    for f in sorted(dec_dir.glob("ADR-*.md")):
        missing = {m.group(0) for m in ID_RE.finditer(f.read_text(encoding="utf-8"))
                   if m.group(1) != "ADR" and m.group(0) not in reg.ids()}
        for ref in sorted(missing):
            warns.append(f"{f.stem} 提到未登记的 {ref}")

    loops, closed = read_open_loops(p)
    if loops:
        ownerless = [l["item"] for l in loops if blank_field(l.get("assets"))]
        if ownerless:
            warns.append(f"开环 {len(ownerless)} 条没有归属资产：{'、'.join(ownerless[:5])}"
                         + ("…" if len(ownerless) > 5 else ""))
        if len(loops) > 15:
            warns.append(f"开环 {len(loops)} 条 > 15，做一轮「放弃还是做」的裁决"
                         f"（registry.py loop --done N）")
    cp = p / "memory" / "context-packet.md"
    if not cp.exists():
        warns.append("没有上下文包，跑 registry.py context")
    if scale_of(p) == "F":
        ent = p / "registry" / "entities"
        missing, unfilled = [], []
        for _, _, d in reg.rows:
            rid = d.get("ID", "")
            if not rid:
                continue
            f = ent / f"{rid}.md"
            if not f.exists():
                missing.append(rid)
                continue
            if entity_unfilled(f.read_text(encoding="utf-8")):
                unfilled.append(rid)
        if missing:
            warns.append(f"F 档缺详卡 {len(missing)} 个：{'、'.join(missing[:8])}"
                         + ("…" if len(missing) > 8 else ""))
        if unfilled:
            warns.append(f"F 档详卡没填职责/边界 {len(unfilled)} 个：{'、'.join(unfilled[:8])}"
                         + ("…" if len(unfilled) > 8 else "")
                         + " —— 答不出边界就说明它可能是邻居的重复")
        dep = p / "registry" / "dependencies.md"
        if not dep.exists():
            warns.append("F 档缺依赖关系图，跑 registry.py graph")
        else:
            m = FP_RE.search(dep.read_text(encoding="utf-8"))
            if not m:
                warns.append("依赖图没有指纹，跑 registry.py graph 重建")
            elif m.group(1) != rows_fingerprint(reg):
                warns.append("依赖图已过期（注册表改过），跑 registry.py graph")
    # --strict-deps：把「依赖了未登记的 ID」从 WARN 重新分级为 ERROR。
    # **只搬级别、不新增检查**，所以默认口径一字未改——存量清账是逐步的，不该一开闸就把
    # 半年前写错的旧账变成今天的门禁（那样只会逼人加 --force 绕过，闸门就废了）。
    if getattr(a, "strict_deps", False):
        moved = []
        for _, _, d in reg.rows:
            for ref in dangling_deps(d.get("ID", ""), d.get("依赖", "") or "", reg.ids()):
                msg = f"{d.get('ID','')} 引用了未登记的 {ref}"
                if msg in warns:
                    warns.remove(msg)
                    moved.append(msg + "：依赖链断了（--strict-deps）")
        errs.extend(moved)

    for e in errs:
        print("ERROR  " + e)
    for w in warns:
        print("WARN   " + w)
    if planned_pending:
        print(f"PLAN   {len(planned_pending)} 个构思中未落盘（位置是意向，不做路径校验）："
              + "、".join(planned_pending[:8]) + ("…" if len(planned_pending) > 8 else ""))
    print(f"— check：{len(errs)} 错 / {len(warns)} 警 / {len(reg.rows)} 资产"
          + (f" / 构思未落盘 {len(planned_pending)}" if planned_pending else ""))
    sys.exit(1 if errs else 0)


def entity_unfilled(t):
    """F 档详卡是不是还停留在模板占位（职责 / 边界都没写）。"""
    for sec in ("职责", "与邻居的边界"):
        m = re.search(r"##\s*" + sec + r"\s*\n([\s\S]*?)(?=\n##|\Z)", t)
        body = (m.group(1).strip() if m else "")
        if not body or body.startswith("（") or "TODO" in body.upper():
            return True
    return False


def recent_sessions(p, n=3):
    """最近的会话记录：M/F 从 sessions/ 读文件，L 档从单一 log.md 切块。
    不覆盖 L 档会让「开工第一读」在最需要它的小项目阶段空转。"""
    out = []
    d = p / "memory" / "sessions"
    if d.exists():
        out = [(f.stem, _read(f)) for f in sorted(d.glob("*.md"), key=lambda f: f.name)[-n:]]
    if not out:
        log = p / "memory" / "log.md"
        if log.exists():
            blocks = [b for b in re.split(r"(?m)^(?=# )", _read(log))
                      if b.strip().startswith("# ")
                      and ("## 本次目标" in b or "## 下次起步点" in b)]
            for b in blocks[-n:]:
                out.append((b.splitlines()[0].lstrip("# ").strip(), b))
    return out


def adr_title(body, fallback):
    """决策卡标题取 H1（`# ADR-001 · 真实标题`），不取文件名 slug。
    context 与 export 必须共用这一个函数——两处各写一份时，同一张卡会在
    上下文包出 `ADR-001-sort-server`、在 manifest 出「持仓卡片改为服务端排序」。"""
    m = re.search(r"^#\s+(.+)$", body, re.M)
    t = m.group(1).strip() if m else ""
    t = re.sub(r"^ADR-\d+\s*[·\-–—:：]\s*", "", t).strip()
    if re.fullmatch(r"ADR-\d+", t or ""):
        t = ""      # H1 只写了编号 = 没有真标题，回退文件名，别输出「ADR-002 · ADR-002」
    return t or fallback


def _recency_key(d):
    """「最近」的判定键：**日期优先，同一天按 ID 序号倒序**。

    「更新」列只精确到天。同一天登记的一批，这个值完全相同 —— 而 Python 的
    sorted 是稳定排序，键相等时保持原表顺序，`reverse=True` 在这一批里等于没
    执行。于是「最近 10 条」拿到的其实是当天最先登记的那批（实测：100 资产项
    目里最后登记的 UI-066/067/068 一条都进不了表，表首站着开天辟地的 MOD-001）。

    次级依据用 ID 序号：NNN 全局单调递增、永不回收，号大者必然后登记 —— 这条
    是技能自己的硬承诺，不依赖任何时间戳，比放宽「更新」列的粒度便宜得多。
    """
    # 走 _normalize_day 拿 (年,月,日) 而不是直接比字符串：字符串比较是逐字符的，
    # 跨年会翻车（`12-30` > `01-05`，去年的压过今年的）。拿到年份才等于「哪个更晚」。
    m = re.search(r"(\d+)\s*$", d.get("ID") or "")
    return (_normalize_day(d.get("更新")) or (0, 0, 0), int(m.group(1)) if m else -1)


def _adr_num(f):
    """决策卡编号的数值键。文件名是 `ADR-NNN-*.md`（三位补零），字典序在 999 张
    以内恰好等于数值序 —— 那是巧合，不是设计；超过 999 就翻车。直接按数值排，
    不依赖补零位数。"""
    m = re.match(r"ADR-(\d+)", f.name)
    return int(m.group(1)) if m else -1


def cmd_context(a):
    root = Path(a.root) if getattr(a, "root", None) else find_root()
    if not root:
        die("未找到 .project，先 init")
    p = proj(Path(root))
    reg = Registry(root)
    name = project_name(p, root)
    pm = _read(p / "PROJECT.md")

    active = sorted([d for _, _, d in reg.rows if d.get("状态") in ("在建", "可用")],
                    key=_recency_key, reverse=True)
    planned = sorted([d for _, _, d in reg.rows
                      if (d.get("状态") or "").strip() == PLANNED_STATUS],
                     key=_recency_key, reverse=True)
    decs = sorted((p / "memory" / "decisions").glob("ADR-*.md"),
                  key=_adr_num)[-5:] if (p / "memory" / "decisions").exists() else []
    sess = recent_sessions(p, 3)
    loops, closed = read_open_loops(p)

    L = [f"# 上下文包 · {name} · {today()}", "",
         "> 由 `registry.py context` 生成，勿手改。开工第一读。", ""]
    pos = re.search(r"^-\s*一句话定位：\s*(.+)$", pm, re.M)
    pos_txt = pos.group(1).strip() if pos else ""
    L += ["## 1 项目是什么", ""]
    if pos and not is_placeholder(pos_txt):
        L.append(f"**{name}** — {pos_txt}")
    else:
        L.append(f"**{name}** — （PROJECT.md 的一句话定位还没填）")
    L.append("")
    b = re.search(r"##\s*边界（本项目不做）\s*\n([\s\S]*?)(?=\n##|\Z)", pm)
    if b and b.group(1).strip():
        items = [x.strip() for x in b.group(1).strip().splitlines()
                 if x.strip() and not re.match(r"^\d+\.\s*$", x.strip())]
        items = [x for x in items if not is_placeholder(x)]
        if items:
            L += ["边界（不做）：" + "；".join(items), ""]
    L.append("")
    L += ["## 2 现在到哪", ""]
    if sess:
        for label, body in sess:
            first = next((ln for ln in body.splitlines()
                          if ln.strip() and not ln.startswith("#")), "").strip()
            L.append(f"- {label}：{first}")
    else:
        L.append("（还没有会话记录）")
    L.append("")
    L += ["## 3 活跃资产", ""]
    if active:
        L.append("| ID | 语义名 | 状态 | 位置 |")
        L.append("|---|---|---|---|")
        for d in active[:10]:
            L.append(f"| {d['ID']} | {d.get('语义名','')} | {d.get('状态','')} | {d.get('位置','')} |")
        if len(active) > 10:
            L.append(f"\n（共 {len(active)} 条活跃，按更新时间列最近 10 条）")
    else:
        L.append("（无在建 / 可用资产）")
    if planned:
        # 「构思」不是活跃资产的子集，但开工第一读必须看得见它——否则「先登记再动手」
        # 的第一步，读到的包恰好是空的（实测：规划完几个资产、还没落盘时开工会显示
        # "无活跃资产"，上下文包等于没给）。
        L.append("")
        L.append(f"**构思中（待落盘）{len(planned)} 个**")
        L.append("")
        L.append("| ID | 语义名 | 位置（意向） |")
        L.append("|---|---|---|")
        for d in planned[:10]:
            L.append(f"| {d['ID']} | {d.get('语义名','')} | {d.get('位置','') or '（位置未定）'} |")
        if len(planned) > 10:
            L.append(f"\n（共 {len(planned)} 个构思，按更新时间列最近 10 个）")
    L.append("")
    L += ["## 4 近期决策", ""]
    if decs:
        for f in decs:
            body = _read(f)
            st = re.search(r"^-\s*状态：(.+)$", body, re.M)
            de = re.search(r"^##\s*决定\s*\n+(.+)$", body, re.M)
            m = re.match(r"(ADR-\d+)", f.name)
            did = m.group(1) if m else f.stem
            L.append(f"- **{did} · {adr_title(body, f.stem)}** "
                     f"[{st.group(1).strip() if st else '生效'}] — "
                     + (de.group(1).strip() if de else ""))
    else:
        L.append("（还没有决策卡）")
    L.append("")
    L += ["## 5 开环与阻塞", ""]
    if loops:
        L.append("| 事项 | 卡在哪 | 关联资产 | 登记日 |")
        L.append("|---|---|---|---|")
        for l in sorted(loops, key=loop_rank):
            bl = "" if blank_field(l["blocked"]) else l["blocked"]
            ast = "" if blank_field(l["assets"]) else l["assets"]
            L.append(f"| {l['item']} | {bl} | {ast} | {l['date']} |")
        if closed:
            L.append(f"\n（已闭环 {len(closed)} 条，见 open-loops.md）")
    else:
        L.append("（无开环）" + (f"（已闭环 {len(closed)} 条）" if closed else ""))
    L.append("")
    L += ["## 6 建议起步点", ""]
    nexts = [m.group(1).strip() for m in
             (re.search(r"^##\s*下次起步点\s*\n+(.+)$", body, re.M) for _, body in sess) if m]
    nexts = [x for x in nexts if not is_placeholder(x)]
    # 同 active：按「最近」排，别按登记顺序 —— 否则这里永远推三个开天辟地的号
    building = sorted([d for _, _, d in reg.rows if d.get("状态") == "在建"],
                      key=_recency_key, reverse=True)
    n_step = 0
    if nexts:
        n_step += 1
        L.append(f"{n_step}. 接上次：{nexts[-1]}")
    if loops:
        n_step += 1
        L.append(f"{n_step}. 先解最堵的开环：{sorted(loops, key=loop_rank)[0]['item']}")
    if building:
        n_step += 1
        L.append(f"{n_step}. 推进在建："
                 + "、".join(f"{d['ID']} {d.get('语义名','')}" for d in building[:5]))
    elif planned:
        n_step += 1
        L.append(f"{n_step}. 构思中的先落盘再动手："
                 + "、".join(f"{d['ID']} {d.get('语义名','')}" for d in planned[:5])
                 + "（落盘后 `registry.py set <ID> --path \"...\" --status 在建`）")
    if n_step == 0:
        L.append("1. 先登记本次要做的东西（registry.py add），再动手。")
    L.append("")
    (p / "memory" / "context-packet.md").write_text("\n".join(L), encoding="utf-8")
    print(f"已刷新 {p / 'memory' / 'context-packet.md'}")


def doctor_scan(root, reg, idea_days):
    """收工体检：把「登记了却没交代」的几类一次摆出来。**只报，不裁决、不阻断。**

    与 check 的分工：check 只看注册表内部一致性（快、可反复跑）；体检要多看两眼磁盘——
    构思是不是其实已经落盘了、有没有绕过登记的新文件。那两眼都贵，所以只在收工时跑一次。
    """
    exts = list(DEFAULT_EXTS)
    root = Path(root)
    ideas, stale, landed, gone = [], [], [], []
    for _, _, d in reg.rows:
        rid = (d.get("ID") or "").strip()
        if not rid:
            continue
        nm = d.get("语义名", "")
        loc = _rel(d.get("位置", ""))
        if (d.get("状态") or "").strip() == PLANNED_STATUS:
            if loc and has_source_files(root / loc, exts):
                landed.append((rid, nm, loc))       # 登记说没做，磁盘说做了 → 状态没跟上
            else:
                age = days_since(d.get("更新", ""))
                ideas.append((rid, nm, loc, age))
                if idea_days and age is not None and age >= idea_days:
                    stale.append((rid, age))
        elif loc and not (root / loc).exists():
            gone.append((rid, nm, loc))
    locs = [(d, _rel(d.get("位置", ""))) for _, _, d in reg.rows]
    unreg = [f for f in walk_source_files(root, exts) if not asset_of_file(locs, f)]
    return {"ideas": ideas, "stale": stale, "landed": landed, "gone": gone,
            "unregistered": unreg}


def doctor_count(doc):
    return (len(doc["ideas"]) + len(doc["landed"]) + len(doc["gone"])
            + len(doc["unregistered"]))


def render_doctor(doc, idea_days):
    """体检结果的文本行。终端与会话记录共用同一份，避免两处措辞漂移。"""
    L = []
    if doc["stale"]:
        L.append(f"构思超期（阈值 {idea_days} 天）："
                 + "、".join(f"{r}（已挂 {a} 天）" for r, a in doc["stale"]))
    if doc["ideas"]:
        L.append("构思未落盘：" + "、".join(f"{r} {n}" for r, n, _, _ in doc["ideas"]))
    if doc["landed"]:
        L.append("构思但其实已落盘："
                 + "、".join(f"{r} {n} → `{lc}`" for r, n, lc in doc["landed"])
                 + "—— 落盘了就 `set --status 在建`")
    if doc["gone"]:
        L.append("位置不存在：" + "、".join(f"{r} {n} → `{lc}`" for r, n, lc in doc["gone"])
                 + "—— `check` 要报 ERROR，收工前清掉")
    if doc["unregistered"]:
        u = doc["unregistered"]
        L.append(f"未登记文件 {len(u)} 个：" + "、".join(f"`{f}`" for f in u[:5])
                 + ("…" if len(u) > 5 else "")
                 + "—— 跑 `reconcile` 出完整提议；该登记的登记，不登记的写决策卡说明")
    return L


def handoff_tip(root, p, name):
    """收工后的「开新对话」交接提示：一句提醒 + 一段可直接粘贴的开场白。

    为什么要有它：人不敢开新对话，本质是两件事 —— 怕丢上下文、以及不知道新话题
    第一句该说什么。于是单条会话越拖越长，直到压缩，丢细节 → AI 误解 → 返工 +
    把背景重讲一遍。把「交接」做成可复制文本，开新对话的成本就降到接近零，
    长会话也就没有存在的理由了。

    数据全部取自刚被 cmd_context 刷新的 context-packet.md，**不另建文件**：
    交接卡与上下文包信息高度重合，多一个文件就是多一份会各自漂移的真相。
    """
    pkt = _read(p / "memory" / "context-packet.md")
    m = re.search(r"##\s*6\s*建议起步点\s*\n([\s\S]*?)(?=\n##|\Z)", pkt)
    first = ""
    if m:
        for ln in m.group(1).splitlines():
            ln = ln.strip()
            if re.match(r"^\d+\.\s*\S", ln):
                first = re.sub(r"^\d+\.\s*", "", ln).strip()
                # 上下文包那行本身以「接上次：」开头，这里再套一层前缀就成了
                # 「上次收工的起步点：接上次：…」——剥掉，开场白读着才像人话
                first = re.sub(r"^接上次：\s*", "", first)
                break
    loops, _closed = read_open_loops(p)
    W = 66
    L = ["", "─" * W,
         "✅ 这件事收工了 —— 建议开一条新对话再做下一件。",
         "   原因：本会话的上下文已被这轮工作占住，继续聊只会越来越慢；",
         "   压缩一旦发生，丢的是细节，代价是返工 + 你得把背景重讲一遍。",
         "",
         "   新对话第一句直接粘这一段：",
         "",
         f"   接着「{name}」继续做。项目根：{root}",
         "   先读 .project/memory/context-packet.md（开工第一读）再动手。"]
    if first:
        L.append(f"   上次收工的起步点：{first}")
    if loops:
        L.append("   待解开环 %d 项，最堵的一条：%s"
                 % (len(loops), sorted(loops, key=loop_rank)[0]["item"]))
    L.append("─" * W)
    return L


def cmd_close(a):
    root = a.root or find_root()
    if not root:
        die("未找到 .project，先 init")
    p = proj(root)
    hold_lock(lock_path(root))      # 会话文件名唯一性判定 + 开环读改写，都在锁内
    scale = scale_of(p)

    # 先解析 --opens：坏输入必须在写会话记录之前就报错，不能留下半截流水，
    # 也不能写出 7 个竖线的坏表行。
    o_item = o_blocked = o_assets = ""
    opens_txt = "（无）"
    if a.opens:
        o_item, o_blocked, o_assets = parse_opens(a.opens)
        opens_txt = o_item + "".join(
            f"（{lbl}：{v}）" for lbl, v in (("卡点", o_blocked), ("资产", o_assets)) if v)

    # 收工体检：只读、不阻断。跑在写会话记录之前，结论才能一起进流水。
    doc, doc_lines = None, []
    if not getattr(a, "no_doctor", False):
        days = idea_stale_days(p)
        doc = doctor_scan(root, Registry(root), days)
        doc_lines = render_doctor(doc, days)
    doc_sec = ""
    if doc_lines:
        doc_sec = "## 收工体检\n\n" + "\n".join("- " + x for x in doc_lines) + "\n\n"

    body = (f"# {today()} · {a.title or a.summary[:24]}\n\n"
            f"## 本次目标\n\n{a.summary}\n\n"
            f"## 做了什么\n\n- （补：动过的资产 ID + 一句话）\n\n"
            f"## 结论\n\n- \n\n"
            f"## 开环\n\n- {opens_txt}\n\n"
            + doc_sec
            + f"## 下次起步点\n\n{a.next or '（补一句可执行的话）'}\n")
    if scale == "L":
        with open(p / "memory" / "log.md", "a", encoding="utf-8") as f:
            f.write("\n" + body)
        print(f"已追加 {p / 'memory' / 'log.md'}")
    else:
        d = p / "memory" / "sessions"
        d.mkdir(exist_ok=True)
        slug = slugify(a.title or a.summary)
        f = d / f"{today()}-{slug}.md"
        i = 2
        while f.exists():
            f = d / f"{today()}-{slug}-{i}.md"
            i += 1
        f.write_text(body, encoding="utf-8")
        print(f"已写入 {f}")

    # 开环全档入账。L 档不建 open-loops.md 的话，--opens 只以纯文本躺在 log.md 里，
    # loop --list / 上下文包第 5 段 / check 的开环闸门会同时失明。
    if a.opens:
        if add_loop(p, o_item, o_blocked, o_assets):
            print(f"已登记开环 → {p / 'memory' / 'open-loops.md'}")
        else:
            print("开环已存在，未重复登记")

    cmd_context(argparse.Namespace(root=str(root)))
    if doc is not None:
        n = doctor_count(doc)
        if n:
            print(f"收工体检：{n} 项待交代（只提示、不阻断——逐条裁决：落盘 / 登记 / 写决策卡）")
            for x in doc_lines:
                print("  - " + x)
        else:
            print("收工体检：0 项待交代 ✅")
    print("收工三步：① 会话记录 ✅ ② 记得更新动过的资产状态 ③ 上下文包已刷新 ✅")
    if scale == "L":
        print("  （L 档：流水在 memory/log.md，上下文包照样生成，开工第一读照用）")
    if not getattr(a, "no_tip", False):
        for ln in handoff_tip(root, p, project_name(p, root)):
            print(ln)


def cmd_export(a):
    """产出机器可读 manifest.json——Serena 对账与 Archify IR 的入口。"""
    root = Path(a.root) if getattr(a, "root", None) else find_root()
    if not root:
        die("未找到 .project，先 init")
    p = proj(root)
    reg = Registry(root)
    pm = _read(p / "PROJECT.md")
    name = project_name(p, root)
    pos = re.search(r"^-\s*一句话定位：\s*(.+)$", pm, re.M)
    typ = re.search(r"^-\s*项目类型：\s*(.+)$", pm, re.M)
    scale = re.search(r"^-\s*规模档位：\s*([LMF])", pm, re.M)

    def split_ids(s):
        return [x.strip() for x in re.split(r"[,，、/]+", s or "") if x.strip()]

    assets = []
    for _, pfx, d in reg.rows:
        assets.append({
            "id": d.get("ID", ""),
            "type": pfx,
            "name": d.get("语义名", ""),
            "alias": split_ids(d.get("别名", "")),
            "path": (d.get("位置") or "").strip(),
            "status": d.get("状态", ""),
            "deps": split_ids(d.get("依赖", "")),
            "decisions": split_ids(d.get("决策", "")),
            "updated": d.get("更新", ""),
        })
    decs = []
    for f in sorted((p / "memory" / "decisions").glob("ADR-*.md")):
        t = f.read_text(encoding="utf-8")
        st = re.search(r"^-\s*状态：(.+)$", t, re.M)
        de = re.search(r"^##\s*决定\s*\n+(.+)$", t, re.M)
        im = re.search(r"^-\s*影响资产：(.+)$", t, re.M)
        # title 取 H1（「# ADR-001 · 真实标题」），不取文件名 slug；与上下文包同一函数
        title = adr_title(t, f.stem)
        decs.append({
            "id": re.match(r"(ADR-\d+)", f.name).group(1),
            "title": title,
            "status": st.group(1).strip() if st else "生效",
            "decision": de.group(1).strip() if de else "",
            "assets": split_ids(im.group(1)) if im else [],
            "file": str(f.relative_to(root)).replace("\\", "/"),
        })
    loops = []
    for l in read_open_loops(p)[0]:
        loops.append({"item": l["item"], "blocked": l["blocked"],
                      "assets": split_ids(l["assets"]), "since": l["date"]})
    manifest = {
        "schema": "project-registry/manifest@1",
        "project": name,
        "positioning": "" if is_placeholder(pos.group(1)) else (pos.group(1).strip() if pos else ""),
        "type": typ.group(1).strip() if typ else "",
        "scale": scale.group(1) if scale else "M",
        "generated": datetime.now().isoformat(timespec="seconds"),
        "assets": assets,
        "decisions": decs,
        "openLoops": loops,
    }
    out = Path(a.out) if a.out else p / "registry" / "manifest.json"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
    except OSError as e:
        die(f"写不了 {out}：{e}\n  --out 指向的目录会自动创建；若是权限/占用问题请换路径。")
    print(f"已导出 {out}")
    print(f"  {len(assets)} 资产 / {len(decs)} 决策 / {len(loops)} 开环")
    print("  这是机器可读出口：Serena 对账与 Archify IR 都吃这一份，不要再解析 index.md。")


def walk_source_files(root, exts):
    """项目里的源码文件（项目内相对路径、正斜杠）。跳过 SKIP_DIRS 与点开头目录。
    reconcile 与收工体检共用这一份——两处各写一份必然漂移。"""
    out = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in fns:
            if fn.endswith(tuple(exts)):
                out.append(os.path.relpath(os.path.join(dp, fn), root).replace("\\", "/"))
    return out


def asset_of_file(locs, rel):
    """这个文件归哪个资产：`位置` 覆盖它、且**最长的位置胜**。locs = [(行 dict, 归一化位置)]。
    与 scan 的 owners 同口径——早先 reconcile 是"先命中先返回"，同一个文件在两个出口
    可能归给两个不同的资产。"""
    best = None
    for d, loc in locs:
        if not loc or loc == "-":
            continue
        if rel == loc or rel.startswith(loc.rstrip("/") + "/"):
            if best is None or len(loc) > len(best[1]):
                best = (d, loc)
    return best


def has_source_files(base, exts, cap=2000):
    """这个位置底下有没有扫描范围内的源码文件。只判存在性，不解析、不建图（体检要快）。"""
    base = Path(base)
    extset = {e.lower() for e in exts}
    try:
        if base.is_file():
            return base.suffix.lower() in extset
        if not base.is_dir():
            return False
    except OSError:
        return False
    seen = 0
    for dp, dns, fns in os.walk(base):
        dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in fns:
            if Path(fn).suffix.lower() in extset:
                return True
            seen += 1
            if seen > cap:
                return False
    return False


def cmd_reconcile(a):
    """对账：代码里实际有什么（as-is） vs 注册表说该有什么（as-registered）。
    只产出【提议】，绝不直写注册表——裁决权在收工时的人。"""
    root = Path(a.root) if getattr(a, "root", None) else find_root()
    if not root:
        die("未找到 .project，先 init")
    p = proj(root)
    reg = Registry(root)
    exts = [e if e.startswith(".") else "." + e for e in a.ext.split(",")] if a.ext else DEFAULT_EXTS

    files = walk_source_files(root, exts)
    locs = [(d, (d.get("位置") or "").strip().replace("\\", "/")) for _, _, d in reg.rows]
    unregistered = [f for f in files if not asset_of_file(locs, f)]
    # 「构思」的位置是意向，没落盘不算失效——这是它和「路径写错 / 文件被删」的区别。
    planned_rows = [d for _, _, d in reg.rows
                    if (d.get("状态") or "").strip() == PLANNED_STATUS]
    stale = [(d.get("ID", ""), d.get("语义名", ""), loc)
             for d, loc in locs
             if loc and loc not in ("-", "") and not (Path(root) / loc).exists()
             and (d.get("状态") or "").strip() != PLANNED_STATUS]
    naming = [(d.get("ID", ""), d.get("语义名", ""), bad_name(d.get("语义名", "")))
              for _, _, d in reg.rows if bad_name(d.get("语义名", ""))]
    dupes = []
    rows = [(d.get("ID", ""), d.get("语义名", ""), d.get("别名", "")) for _, _, d in reg.rows]
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            s = jaccard(tokens(rows[i][1] + " " + rows[i][2]), tokens(rows[j][1] + " " + rows[j][2]))
            if s >= 0.5:
                dupes.append((s, rows[i][0] + " " + rows[i][1], rows[j][0] + " " + rows[j][1]))

    L = [f"# 对账提议 · {today()}", "",
         "> 由 `registry.py reconcile` 生成。**只提议，不裁决**：逐条确认后，",
         "> 由你（或收工时的 AI）决定是否写进注册表。", "",
         f"扫描 {len(files)} 个文件（{','.join(exts[:5])}…）／注册表 {len(reg.rows)} 项", ""]
    L += ["## 1 未登记（代码里有，注册表没有）", ""]
    L += [f"- `{f}`" for f in unregistered[:50]] or ["- （无）"]
    if len(unregistered) > 50:
        L.append(f"\n（另有 {len(unregistered) - 50} 条，先处理前 50）")
    L += ["", "## 2 失效登记（注册表有，代码里没了）", ""]
    if planned_rows:
        L += [f"> 构思中 {len(planned_rows)} 个"
              f"（{'、'.join(d.get('ID', '') for d in planned_rows[:8])}）的位置是**意向**，"
              f"还没落盘，不算失效，不在这张表里。", ""]
    L += [f"- {i} {n} → `{loc}`" for i, n, loc in stale] or ["- （无）"]
    L += ["", "## 3 命名疑似违规（命中禁用词）", ""]
    L += [f"- {i} {n} → 命中 {w}" for i, n, w in naming] or ["- （无）"]
    L += ["", "## 4 疑重（注册项之间）", ""]
    L += [f"- [{s:.0%}] {x} ⟷ {y}" for s, x, y in sorted(dupes, reverse=True)] or ["- （无）"]
    L.append("")

    (p / "registry").mkdir(parents=True, exist_ok=True)
    (p / "registry" / "proposals.md").write_text("\n".join(L), encoding="utf-8")
    print(f"对账完成：未登记 {len(unregistered)} / 失效 {len(stale)} / 命名 {len(naming)} / 疑重 {len(dupes)}"
          + (f" / 构思跳过 {len(planned_rows)}" if planned_rows else ""))
    print(f"  提议已写入 {p / 'registry' / 'proposals.md'}")
    if a.write and unregistered:
        hold_lock(lock_path(root))   # add_loop 是读-改-写，与 close / 另一次 reconcile 会打架
        n = 0
        for f in unregistered[:10]:
            if add_loop(p, f"未登记：`{f}`", blocked="待裁决：登记还是删"):
                n += 1
        print(f"  {n} 条未登记已提议进 open-loops（同事项不重复堆，仍需人工裁决）")


def write_entity_card(p, eid, prefix, kw):
    """F 档：每个资产一张详卡（职责句 + 与邻居的边界 + 变更记录）。"""
    d = Path(p) / "registry" / "entities"
    d.mkdir(parents=True, exist_ok=True)
    t = (TPL_DIR / "entity.md.tpl").read_text(encoding="utf-8")
    t = t.replace("{ID}", eid)
    for k, v in kw.items():
        t = t.replace("{" + k + "}", str(v))
    (d / f"{eid}.md").write_text(t, encoding="utf-8")


def cmd_graph(a):
    """依赖关系图 + 结构体检（F 档的核心增量，M 档也能跑）。"""
    root = Path(a.root) if getattr(a, "root", None) else find_root()
    if not root:
        die("未找到 .project，先 init")
    p = proj(root)
    reg = Registry(root, lock=True)
    name = project_name(p, root)

    nodes, edges = {}, []
    for _, _, d in reg.rows:
        nodes[d.get("ID", "")] = d.get("语义名", "")
    for _, _, d in reg.rows:
        src = d.get("ID", "")
        for dep in re.split(r"[,，、/]+", d.get("依赖", "") or ""):
            dep = dep.strip()
            if dep:
                edges.append((src, dep))

    adj = {i: [y for x, y in edges if x == i] for i in nodes}
    cycles, state = [], {}

    def dfs(u, stack):
        state[u] = 1
        for v in adj.get(u, []):
            if v not in nodes:
                continue
            if state.get(v) == 1:
                cycles.append(" → ".join(stack[stack.index(v):] + [v]))
            elif state.get(v, 0) == 0:
                dfs(v, stack + [v])
        state[u] = 2

    for i in nodes:
        if state.get(i, 0) == 0:
            dfs(i, [i])

    god = god_assets(nodes, edges)
    dangling = sorted({y for _, y in edges if y not in nodes})

    L = [f"# 依赖关系图 · {name} · {today()}", "",
         "> 由 `registry.py graph` 生成，勿手改。", "",
         f"<!-- fp:{rows_fingerprint(reg)} -->", "",
         "## 图（mermaid）", "", "```mermaid", "flowchart LR"]
    if nodes:
        for i, nm in nodes.items():
            L.append(f'  {i}["{i} {nm}"]')
        for x, y in edges:
            L.append(f"  {x} --> {y}")
    else:
        L.append("  EMPTY[（还没有登记资产）]")
    L += ["```", "", "## 结构体检", ""]
    L.append(f"- 资产 {len(nodes)} / 依赖边 {len(edges)}")
    L.append("- 循环依赖：" + ("；".join(canon_cycles(cycles)) if cycles else "无"))
    L.append(f"- 上帝资产（依赖 > {GOD_FANOUT}，需拆）：" + ("、".join(god) if god else "无"))
    L.append("- 悬空引用（依赖了未登记 ID）：" + ("、".join(dangling) if dangling else "无"))
    L.append("")
    out = p / "registry" / "dependencies.md"
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"已生成 {out}")
    print(f"  循环依赖 {len(canon_cycles(cycles))} / 上帝资产 {len(god)} / 悬空引用 {len(dangling)}")


def cmd_idseq(a):
    """查看 / 修正发号游标。

    游标是发号权威，只在真正发号时前进、删行不回退。但它被污染后原本不可逆
    （一条笔误 UI-999 就能永久抬到 1000），这里给一个显式出口：
    确认某号真实发过、或已废弃需永久保号，才上调；不要为了「对齐」而改。
    """
    root = Path(a.root) if getattr(a, "root", None) else find_root()
    if not root:
        die("未找到 .project，先 init")
    p = proj(root)
    if a.set is not None:
        hold_lock(lock_path(root))   # 游标读-改-写：锁必须在读 seq 之前拿
    sp = p / "registry" / ".idseq.json"
    seq = {}
    if sp.exists():
        try:
            seq = json.loads(sp.read_text(encoding="utf-8"))
        except Exception:
            seq = {}
    reg = Registry(root, lock=True)
    live = {}
    for _, _, d in reg.rows:
        m = ID_RE.fullmatch((d.get("ID") or "").strip())
        if m:
            live[m.group(1)] = max(live.get(m.group(1), 0), int(m.group(2)))
    if a.set is None:
        keys = sorted(set(seq) | set(live))
        if not keys:
            print("（游标为空，下次 add 时会从现存行与历史文本推断）")
            return
        print("  " + pad("前缀", 6) + pad("游标", 8) + pad("现存最大", 10) + "下一个")
        for k in keys:
            cur, lv = int(seq.get(k, 0)), live.get(k, 0)
            print(f"  {pad(k, 6)}{pad(cur, 8)}{pad(lv, 10)}{k}-{max(cur, lv) + 1:03d}")
        print("\n  游标只在真正发号时前进；删行不会让它回退。")
        return
    raw = " ".join(a.set) if isinstance(a.set, list) else (a.set or "")
    parts = re.split(r"[:=\s]+", raw.strip(), maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        die("--set 格式：registry.py idseq --set UI 3（前缀 空格 数字）")
    pfx, num = parts[0].upper(), int(parts[1])
    if not PREFIX_RE.match(pfx):
        die(f"前缀不合法：{parts[0]}（2–6 位大写字母）")
    if num > MAX_ID:
        die(f"号不能超过 {MAX_ID}：超限后写出的 ID 会脱离所有 ID 逻辑"
            f"（check / 依赖解析 / find 都认不出它，等于静默失效）")
    lv = live.get(pfx, 0)
    if num < lv:
        die(f"不能下调到 {num}：{pfx} 现存最大号是 {lv}，下调会让号被复用")
    cur = int(seq.get(pfx, 0))
    seq[pfx] = num
    atomic_write(sp, json.dumps(seq, ensure_ascii=False, indent=2) + "\n")
    print(f"游标已设 {pfx} = {num} → 下一个 {pfx}-{num + 1:03d}")
    if num < cur:
        # 下调是修正被污染游标的唯一路径（一条笔误就能把游标永久抬高），
        # 但它同时会释放 cur 以下的号——只能在这些号确认从未发出过时才做。
        print(f"  ⚠ 这是下调（{cur} → {num}）：{pfx}-{num + 1:03d}…{pfx}-{cur:03d} "
              f"可能被复用。仅在确认这些号从未发出过时才下调。")
    else:
        print("  只在确认某号真实发过、或已废弃需永久保号时才上调。")


# ---------- CLI ----------

def force_utf8_io():
    """中文 Windows 的 cmd / PowerShell 默认 cp936，print 里的 ⚠ ✅ ⓘ 会直接
    抛 UnicodeEncodeError 并以 rc=1 退出——命令看起来"失败"，实际是被编码卡死。
    最坏的例子是 init --force：崩在 copytree 之后、骨架重建之前，重建根本没发生。

    但「不崩」和「看得懂」是两件事，所以分两种出口处理：

    - **交互式控制台**（isatty）：保留它自己的编码（cp936 是能正常显示中文的），
      只把编不了的字符（⚠ ✅ ⓘ ❌ ⟷）降级成替代符。若在这里也强切 UTF-8，中文会
      以 UTF-8 字节写进 cp936 控制台 → 整行花码：「不崩」了但「看不懂」，
      等于把崩溃换成了乱码（黑盒审计实测：`list` 输出按 GBK 解是 `鎸佷粨璧勪骇`）。
    - **重定向 / 管道 / 文件**：统一 UTF-8。下游是程序或编辑器，UTF-8 才是对的默认，
      且这里的字节不会被当成控制台编码去解。
    """
    for s in (sys.stdout, sys.stderr):
        if s is None:
            continue
        enc = "utf-8"
        try:
            if s.isatty() and getattr(s, "encoding", None):
                enc = s.encoding      # 控制台原生编码，中文才不会花
        except (AttributeError, ValueError, OSError):
            pass
        try:
            s.reconfigure(encoding=enc, errors="replace")
        except (AttributeError, ValueError, OSError):
            pass  # 3.6 以前没有 reconfigure，或被重定向成非 TextIOWrapper


# ---------- 可视化：注册表 → 自包含架构图 HTML ----------
#
# 为什么内置而不是外挂：manifest.json 是给机器读的，人要看的是「这张图长什么样」。
# 外部画图技能（Archify / wrench-ai 之类）能把图渲染得更花，但都要多一层依赖和一次
# 手工导数据。这个命令只做一件事：把已经落在注册表里的事实画出来，零依赖、单文件、
# 双击就开。要更花的渲染，再拿 manifest.json 去接外部技能（见 modules/04）。
#
# 版面口径（刻意不套用「卡片从上往下堆」那套）：
#   · 依赖链越长越靠左，被依赖的沉在右——所以箭头（依赖者 → 被依赖者）一律向右，
#     不需要反着读图。
#   · 分区靠留白与字号跳档（40 / 44 / 11 / 12.5），不靠卡片容器。
#   · 数字一律 tabular-nums 成列。

STATUS_STYLE = {
    # 状态 -> (填充, 描边, 文字)
    "可用": ("#EAF3DE", "#3B6D11", "#27500A"),
    "在建": ("#E6F1FB", "#185FA5", "#0C447C"),
    "冻结": ("#F1EFE8", "#5F5E5A", "#2C2C2A"),
    "废弃": ("#FCEBEB", "#A32D2D", "#791F1F"),
    "构思": ("#EEEDFE", "#534AB7", "#3C3489"),
}

GOX_LEVELS = ["可用", "在建", "冻结", "废弃", "构思"]


def _deps_of(raw):
    return [x.strip() for x in re.split(r"[,，、/]+", raw or "") if x.strip()]


def _clip(s, maxw):
    """按显示宽度截断（CJK 算 2），超长加省略号。"""
    s = str(s or "")
    if dw(s) <= maxw:
        return s
    out, w = "", 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if w + cw > maxw - 2:
            return out + "…"
        out, w = out + ch, w + cw
    return out


def map_levels(nodes, dep):
    """依赖分层：level(n) = 到末端被依赖资产的最长链长（0 = 不依赖任何已登记资产）。
    用松弛迭代而不是递归 DFS——长链会顶到递归上限，松弛的圈数上界就是节点数。
    环上的节点会在迭代里一直涨，涨到上界收住，再由 find_cycles 单独标出。"""
    level = {i: 0 for i in nodes}
    for _ in range(len(nodes) + 1):
        moved = False
        for i in nodes:
            v = 0
            for d in dep.get(i, []):
                if d in level:
                    v = max(v, level[d] + 1)
            if v != level[i]:
                level[i], moved = v, True
        if not moved:
            break
    return level


def find_cycles(nodes, dep):
    """找环（迭代 DFS，不递归）。返回去重后的环路径列表。"""
    color, cycles = {i: 0 for i in nodes}, []

    def walk(start):
        stack = [(start, iter(dep.get(start, [])))]
        path = [start]
        color[start] = 1
        while stack:
            u, it = stack[-1]
            advanced = False
            for v in it:
                if v not in color:
                    continue
                if color[v] == 1:
                    cycles.append(" → ".join(path[path.index(v):] + [v]))
                    continue
                if color[v] == 0:
                    color[v] = 1
                    path.append(v)
                    stack.append((v, iter(dep.get(v, []))))
                    advanced = True
                    break
            if not advanced:
                color[u] = 2
                stack.pop()
                if path:
                    path.pop()

    for i in nodes:
        if color[i] == 0:
            walk(i)
    return canon_cycles(cycles)


# 「上帝资产」阈值：出度（依赖了几个别的资产）超过它就该考虑拆。
# 只在这里定义一次——曾经 graph 与 viz 各写一遍 `> 5`，而两处算 dep 的口径不同
# （graph 含悬空目标、不去重、含自环；viz 排悬空、去重、排自环），于是同一个项目
# 的同一个体检项会报出两个数（黑盒审计里 UI-001 一处报 1、另一处报 0）。
GOD_FANOUT = 5


def _fanout_map(nodes, edges):
    """{资产: 它依赖的目标集合}。口径统一定义在这里，graph / viz / audit 共用：

    - 目标**含悬空引用**：声明了 6 个依赖就是 6 个。其中 1 个 ID 写错不代表只依赖
      5 个；而且「有悬空引用」正是 graph 自己要报警的状态，此时两个出口最该一致。
    - 目标**去重**：同一个 ID 写两遍不算两个依赖。
    - **排除自环**：自己依赖自己不是「拆不拆」的问题，归 find_cycles 管。
      """
    ns = set(nodes)
    out = {}
    for s, t in edges:
        if s in ns and t != s:
            out.setdefault(s, set()).add(t)
    return out


def god_assets(nodes, edges, threshold=None):
    """出度超阈值的资产（上帝资产），按出度降序、ID 升序返回。"""
    th = GOD_FANOUT if threshold is None else threshold
    out = _fanout_map(nodes, edges)
    return sorted((i for i in out if len(out[i]) > th),
                  key=lambda x: (-len(out[x]), x))


def dag_dep(nodes, dep):
    """拆环：迭代 DFS 标出回边，返回**去掉回边后的依赖表**，只给布局用。

    不拆的话松弛迭代会让环上的节点每轮 +1，level 一路涨到节点数——图会被撑成
    几十列、横向一屏放不下。环本身照画不误（读者要看的就是它违规）。
    """
    color, back = {i: 0 for i in nodes}, set()
    for s in nodes:
        if color[s]:
            continue
        color[s] = 1
        stack = [(s, iter(dep.get(s, [])))]
        while stack:
            u, it = stack[-1]
            advanced = False
            for v in it:
                if v not in color:
                    continue
                if color[v] == 1:
                    back.add((u, v))          # 回边：布局忽略，画图保留
                    continue
                if color[v] == 0:
                    color[v] = 1
                    stack.append((v, iter(dep.get(v, []))))
                    advanced = True
                    break
            if not advanced:
                color[u] = 2
                stack.pop()
    return {i: [d for d in dep.get(i, []) if (i, d) not in back] for i in nodes}


def order_levels(by_lv, dagdep, edges, L, sweeps=8):
    """层内排序：重心法（barycenter）压边交叉。

    不排的话同一列按 ID 字典序堆着，二十几个资产就能把边交叉六十多次、糊成一团线。
    做法是标准 Sugiyama 的后两步：右往左按「被依赖的邻居」重心排，左往右按
    「依赖它的邻居」重心排，来回几轮；没有邻居的节点保持原位，不打乱已定的稳定序。
    """
    idx = {i: k for lst in by_lv.values() for k, i in enumerate(lst)}
    rev = {}
    for s, d in edges:
        if s in idx and d in idx:
            rev.setdefault(d, []).append(s)

    def reorder(lv, neighbor):
        lst = by_lv.get(lv)
        if not lst or len(lst) < 2:
            return
        key = {}
        for n in lst:
            ns = [idx[x] for x in neighbor(n) if x in idx]
            key[n] = (sum(ns) / len(ns), idx[n]) if ns else (float(idx[n]), idx[n])
        lst.sort(key=lambda n: key[n])
        for k, n in enumerate(lst):
            idx[n] = k

    for it in range(sweeps):
        if it % 2 == 0:
            for lv in range(L, -1, -1):
                reorder(lv, lambda n: dagdep.get(n, []))
        else:
            for lv in range(0, L + 1):
                reorder(lv, lambda n: rev.get(n, []))
    return by_lv


def _arch_geometry(assets, edges, dangling, cycles, viol=None):
    """构图计算：资产/边/违规 -> 画图要的几何量。纯计算，不产出任何字符串。"""
    nodes = {a[0]: a[1] for a in assets}
    status_of = {a[0]: (a[2] or "在建") for a in assets}
    vd = (viol or {}).get("declared") if viol else None
    vi = (viol or {}).get("imports") if viol else None
    merged = {}
    for tag, lst in (("声明", vd), ("导入", vi)):
        for v in (lst or []):
            k = (v.get("from", ""), v.get("to", ""))
            m = merged.get(k)
            if m is None:
                merged[k] = dict(v, src=[tag])
            else:
                m["src"].append(tag)
    allv = [merged[k] for k in sorted(merged)]
    vio_pairs = {(v["from"], v["to"]) for v in allv}
    declared_edges = {(s, d) for s, d in edges}
    extra = [p for p in sorted(vio_pairs)
             if p not in declared_edges and p[0] in nodes and p[1] in nodes]
    if extra:
        edges = list(edges) + extra
    dep = {i: [] for i in nodes}
    for s, d in edges:
        if s in dep and d in nodes:
            dep[s].append(d)
    fanin = {i: sum(1 for s, d in edges if d == i) for i in nodes}
    cyc_nodes = {n for c in cycles for n in str(c).split(" → ") if n}
    dagdep = dag_dep(nodes, dep)
    level = map_levels(nodes, dagdep)
    L = max(level.values()) if level else 0
    by_lv = {}
    for i in sorted(nodes, key=lambda x: (level[x], x)):
        by_lv.setdefault(level[i], []).append(i)
    by_lv = order_levels(by_lv, dagdep, edges, L)
    NW, NH, COLW, ROWH, M = 172, 46, 216, 64, 48
    EMPTY_W = NW + 60
    ghosts = list(dangling)
    cols = L + 1 + (1 if ghosts else 0)
    rows = max([len(v) for v in by_lv.values()] + [len(ghosts), 1])
    W_ = M * 2 + (cols - 1) * COLW + NW
    if not nodes:
        W_ = max(W_, M * 2 + EMPTY_W)
    H_ = 78 + rows * ROWH + 40

    def pos(i):
        k = level[i]
        y = 78 + by_lv[k].index(i) * ROWH
        return M + (L - k) * COLW, y

    return {
        "nodes": nodes, "status_of": status_of, "vd": vd, "vi": vi,
        "allv": allv, "vio_pairs": vio_pairs, "extra": extra,
        "declared_edges": declared_edges, "edges": edges, "dep": dep,
        "fanin": fanin, "cyc_nodes": cyc_nodes, "L": L, "by_lv": by_lv,
        "level": level, "W_": W_, "H_": H_, "pos": pos, "NW": NW, "NH": NH,
        "COLW": COLW, "ROWH": ROWH, "M": M, "EMPTY_W": EMPTY_W, "ghosts": ghosts,
    }


def _arch_nodes_svg(g):
    """SVG 输出（节点 / 幽灵 / 空框）。"""
    import html as _h
    nd = []
    for i in sorted(g["nodes"], key=lambda x: (g["level"][x], x)):
        x, y = g["pos"](i)
        NW, NH = g["NW"], g["NH"]
        so = g["status_of"][i]
        fill, stroke, ink = STATUS_STYLE.get(so, ("#F1EFE8", "#5F5E5A", "#2C2C2A"))
        dash = ' stroke-dasharray="3 3"' if so == "废弃" else ""
        ring = ('<rect x="%.1f" y="%.1f" width="%d" height="%d" rx="3" fill="none" '
                'stroke="#A32D2D" stroke-width="1.5"/>' % (x - 3, y - 3, NW + 6, NH + 6)) if i in g["cyc_nodes"] else ""
        nd.append(
            f'<g class="nd" data-id="{_h.escape(i)}" data-st="{_h.escape(so)}" tabindex="0">'
            f'{ring}'
            f'<rect x="{x}" y="{y}" width="{NW}" height="{NH}" rx="3" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="0.75"{dash}/>'
            f'<text class="nid" x="{x+12}" y="{y+17}">{_h.escape(i)}</text>'
            f'<text class="nnm" x="{x+12}" y="{y+34}">{_h.escape(_clip(g["nodes"][i], 24))}</text>'
            f'</g>')
    for gg in g["ghosts"]:
        x, y = g["M"] + (g["L"] + 1) * g["COLW"], 78 + g["ghosts"].index(gg) * g["ROWH"]
        nd.append(f'<g class="nd gh" data-id="{_h.escape(gg)}" data-st="__ghost">'
                  f'<rect x="{x}" y="{y}" width="{g["NW"]}" height="{g["NH"]}" rx="3" fill="none" '
                  f'stroke="#A32D2D" stroke-width="0.75" stroke-dasharray="4 3"/>'
                  f'<text class="nid gh" x="{x+12}" y="{y+17}">{_h.escape(gg)}</text>'
                  f'<text class="nnm gh" x="{x+12}" y="{y+34}">未登记（幽灵引用）</text></g>')
    if not g["nodes"]:
        M, NH, EMPTY_W = g["M"], g["NH"], g["EMPTY_W"]
        nd.append(
            '<g class="nd"><rect x="%d" y="%d" width="%d" height="%d" rx="3" fill="none" '
            'stroke="#B4B2A9" stroke-width="0.75" stroke-dasharray="4 3"/>'
            '<text class="nid" x="%d" y="%d" fill="#8C8A83">还没有登记资产</text>'
            '<text class="nnm" x="%d" y="%d" fill="#8C8A83">先 registry.py add 登记一条</text></g>'
            % (M, 78, EMPTY_W, NH + 8, M + 14, 78 + 21, M + 14, 78 + 38))
    return nd


def _arch_edges_svg(g):
    """边计算 + SVG 输出（依赖边，含跨列改道 / 违规标红 / 幽灵尾）。"""
    import html as _h
    ed = []
    for s, d in g["edges"]:
        if s not in g["nodes"]:
            continue
        x1, y1 = g["pos"](s)
        if d in g["nodes"]:
            x2, y2 = g["pos"](d)
        elif d in g["ghosts"]:
            x2, y2 = g["M"] + (g["L"] + 1) * g["COLW"], 78 + g["ghosts"].index(d) * g["ROWH"]
        else:
            continue
        NW, NH, COLW, ROWH, M = g["NW"], g["NH"], g["COLW"], g["ROWH"], g["M"]
        ax, ay = x1 + NW, y1 + NH / 2
        bx, by = x2, y2 + NH / 2
        mx = (ax + bx) / 2
        bad = " bad" if (d not in g["nodes"] or s in g["cyc_nodes"]) else ""
        if (s, d) in g["vio_pairs"]:
            bad += " vio"
        span = g["level"].get(s, 0) - g["level"].get(d, g["level"].get(s, 0))
        if span >= 2:
            row_of_s = g["by_lv"][g["level"][s]].index(s)
            lane = 78 + row_of_s * ROWH + NH + (ROWH - NH) / 2
            path = (f'M{ax:.1f} {ay:.1f} '
                    f'C{ax + 18:.1f} {ay:.1f} {ax + 18:.1f} {lane:.1f} {ax + 34:.1f} {lane:.1f} '
                    f'L{bx - 34:.1f} {lane:.1f} '
                    f'C{bx - 18:.1f} {lane:.1f} {bx - 18:.1f} {by:.1f} {bx:.1f} {by:.1f}')
        else:
            path = f'M{ax:.1f} {ay:.1f} C{mx:.1f} {ay:.1f} {mx:.1f} {by:.1f} {bx:.1f} {by:.1f}'
        ed.append(f'<path class="ed{bad}" data-src="{_h.escape(s)}" data-dst="{_h.escape(d)}" '
                  f'd="{path}" marker-end="url(#ar)"/>')
    return ed


def _arch_stats_html(g, assets, cycles, god, dangling, viol):
    """信息叙事：徽章 / 图例 / 过滤 / 明细 / 资产表。"""
    import html as _h
    nodes = g["nodes"]; vd = g["vd"]; vi = g["vi"]; allv = g["allv"]; extra = g["extra"]
    declared_edges = g["declared_edges"]
    n_tot, e_tot = len(nodes), len(declared_edges)
    dist = {k: sum(1 for a in assets if (a[2] or "在建") == k) for k in GOX_LEVELS}
    top = sorted(((g["fanin"][i], i) for i in nodes), reverse=True)[:5]
    bar = "".join(
        f'<span class="seg" style="flex:{v or 0.001};background:{STATUS_STYLE[k][1]}" '
        f'title="{k} {v}"></span>' for k, v in dist.items() if v)
    legend = "".join(
        f'<span class="lg"><i style="background:{STATUS_STYLE[k][0]};border-color:{STATUS_STYLE[k][1]}"></i>'
        f'{k} <b>{dist[k]}</b></span>' for k in GOX_LEVELS if dist[k])
    filt = "".join(
        f'<button class="fb" data-f="{k}">{k} {dist[k]}</button>' for k in GOX_LEVELS if dist[k])
    vio_cell = "—"
    if viol is not None:
        nd_, ni_ = len(vd or []), len(vi or [])
        vio_cell = nd_ if (vi is None or nd_ == ni_) else f"{nd_} 声明 / {ni_} 导入"
    health = [
        ("资产", n_tot), ("依赖边", e_tot),
        ("循环依赖", len(cycles)), ("上帝资产", len(god)), ("悬空引用", len(dangling)),
        ("层间违规", vio_cell),
    ]
    hs = "".join(f'<div class="st"><span class="k">{k}</span>'
                 f'<span class="v{" warn" if (k not in ("资产", "依赖边") and str(v) not in ("0", "—")) else ""}">'
                 f'{v}</span></div>'
                 for k, v in health)
    detail = ""
    if allv:
        _nd, _ni = len(vd or []), len(vi or [])
        _src = (f"声明源 {_nd} / 导入源 {_ni}" if vi is not None
                else f"声明源 {_nd}（本次未取导入源）")
        detail += ('<div class="al al-bad"><span class="at">层间违规（资产级边 · ' + _src
                   + ' · 规则来自 .project/architecture.json）</span>'
                   + "".join('<code title="'
                             + _h.escape("来源：" + "/".join(v.get("src", [])) + "；"
                                         + "；".join((v.get("evidence") or [])[:3]))
                             + '">' + _h.escape(v.get("from", "")) + " → "
                             + _h.escape(v.get("to", ""))
                             + ("" if v.get("count", 1) < 2 else f' ×{v["count"]}')
                             + "</code>" for v in allv[:14]) + "</div>")
    if extra:
        detail += ('<div class="al al-warn"><span class="at">上列里有 ' + str(len(extra))
                   + ' 条只存在于代码里（注册表没声明这条依赖，图上按红实线画出）</span>'
                   + "".join('<code>' + _h.escape(a) + " → " + _h.escape(b) + "</code>"
                             for a, b in extra[:14]) + "</div>")
    if cycles:
        detail += ('<div class="al al-bad"><span class="at">循环依赖</span>'
                   + "".join(f'<code>{_h.escape(c)}</code>' for c in cycles) + "</div>")
    if dangling:
        detail += ('<div class="al al-bad"><span class="at">悬空引用（依赖了未登记的 ID）</span>'
                   + "".join(f'<code>{_h.escape(d)}</code>' for d in dangling) + "</div>")
    if god:
        detail += ('<div class="al al-warn"><span class="at">上帝资产（依赖了 &gt; '
                   + str(GOD_FANOUT) + ' 个其他资产，考虑拆）</span>'
                   + "".join(f'<code>{_h.escape(gg)}</code>' for gg in god) + "</div>")
    rows_html = "".join(
        f'<tr data-st="{_h.escape(a[2] or "在建")}"><td class="mid">{_h.escape(a[0])}</td>'
        f'<td>{_h.escape(a[1] or "")}</td><td>{_h.escape(a[2] or "")}</td>'
        f'<td class="pth">{_h.escape(a[3] or "—")}</td>'
        f'<td class="mid">{_h.escape(", ".join(g["dep"].get(a[0], [])) or "—")}</td></tr>'
        for a in sorted(assets, key=lambda x: x[0]))
    top_html = "".join(
        f'<div class="tr2"><span class="mid">{_h.escape(i)}</span>'
        f'<span class="tn">{_h.escape(_clip(nodes[i], 22))}</span>'
        f'<span class="tv">{c}</span></div>' for c, i in top if c)
    return bar, legend, filt, hs, detail, rows_html, top_html


def _arch_html(name, type_name, scale, fp, W_, H_, ed, nd, bar, legend, filt, hs,
               detail, top_html, rows_html):
    """包装：把上面四块拼成自包含 HTML（逐字节对齐拆分前输出）。"""
    import html as _h
    T = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_h.escape(name)} · 架构图</title>
<style>
:root{{
  color-scheme:light;
  --ink:#1B1B19; --ink2:#5F5E5A; --ink3:#8C8A83; --rule:#E2DFD6; --paper:#FBFAF7;
  --mono:ui-monospace,"SF Mono",Consolas,"Cascadia Mono",monospace;
  --sans:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",system-ui,sans-serif;
}}
*{{box-sizing:border-box}}
html,body{{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);font-size:13px;line-height:1.6}}
.wrap{{max-width:1360px;margin:0 auto;padding:56px 40px 96px}}
.num,.v,.tv,.mid{{font-variant-numeric:tabular-nums}}
header{{display:flex;align-items:flex-end;justify-content:space-between;gap:32px;border-bottom:1px solid var(--rule);padding-bottom:20px}}
h1{{margin:0;font-size:40px;font-weight:300;letter-spacing:-.01em;line-height:1.1}}
.meta{{text-align:right;color:var(--ink3);font-size:11px;letter-spacing:.06em;white-space:nowrap}}
.meta b{{display:block;color:var(--ink2);font-weight:500;font-size:12.5px;letter-spacing:0}}
.stats{{display:flex;gap:0;margin:32px 0 8px;border-bottom:1px solid var(--rule)}}
.st{{flex:1;padding:0 24px 20px 0}}
.st:first-child{{padding-left:0}}
.k{{display:block;font-size:11px;letter-spacing:.1em;color:var(--ink3);margin-bottom:6px}}
.v{{font-size:44px;font-weight:300;line-height:1;letter-spacing:-.02em}}
.v.warn{{color:#A32D2D}}
.bar{{display:flex;height:3px;margin:0 0 28px}}
.seg{{display:block}}
.lg{{margin-right:18px;color:var(--ink2);font-size:12px}}
.lg b{{color:var(--ink);margin-left:4px}}
.lg i{{display:inline-block;width:9px;height:9px;border:1px solid;border-radius:2px;margin-right:6px;vertical-align:-1px}}
h2{{font-size:11px;font-weight:500;letter-spacing:.12em;color:var(--ink3);margin:56px 0 16px;text-transform:uppercase}}
.map{{border:1px solid var(--rule);background:#fff;overflow:auto}}
svg{{display:block;min-width:100%}}
.nid{{font-family:var(--mono);font-size:11px;font-weight:500;fill:#1B1B19}}
.nnm{{font-size:12.5px;fill:#3A3A36}}
.nid.gh,.nnm.gh{{fill:#A32D2D}}
.nd{{transition:opacity .12s}}
.ed{{fill:none;stroke:#B4B2A9;stroke-width:1;transition:opacity .12s,stroke .12s}}
.ed.bad{{stroke:#D9A0A0;stroke-dasharray:4 3}}
.ed.vio{{stroke:#A32D2D;stroke-width:1.8}}
.nd.dim,.ed.dim{{opacity:.13}}
.nd.hl{{opacity:1}}
.ed.hl{{stroke:#185FA5;stroke-width:2}}
svg.focus .nd:not(.hl):not(.pin),svg.focus .ed:not(.hl):not(.pin){{opacity:.13}}
.fb{{font-family:var(--sans);font-size:12px;color:var(--ink2);background:#fff;border:1px solid var(--rule);
     padding:5px 11px;margin:0 6px 10px 0;cursor:pointer;border-radius:2px;font-variant-numeric:tabular-nums}}
.fb:hover{{border-color:var(--ink3);color:var(--ink)}}
.fb.off{{opacity:.38;text-decoration:line-through}}
.legend{{margin:12px 0 0}}
.al{{border-left:2px solid var(--rule);padding:8px 0 8px 14px;margin:14px 0;font-size:12.5px}}
.al-bad{{border-color:#A32D2D}} .al-warn{{border-color:#BA7517}}
.at{{display:block;font-size:11px;letter-spacing:.1em;color:var(--ink3);margin-bottom:6px}}
code{{font-family:var(--mono);font-size:12px;background:#F2F0EA;padding:2px 6px;margin:0 8px 4px 0;display:inline-block}}
table{{border-collapse:collapse;width:100%;font-size:12.5px}}
th{{text-align:left;font-weight:500;font-size:11px;letter-spacing:.08em;color:var(--ink3);
    border-bottom:1px solid var(--rule);padding:9px 12px 9px 0}}
td{{border-bottom:1px solid #F0EEE7;padding:9px 12px 9px 0;vertical-align:top}}
.pth{{font-family:var(--mono);font-size:11.5px;color:var(--ink2)}}
.two{{display:grid;grid-template-columns:1fr 1fr;gap:56px}}
.tr2{{display:flex;gap:12px;align-items:baseline;padding:7px 0;border-bottom:1px solid #F0EEE7}}
.tn{{flex:1;color:var(--ink2)}}
.tv{{font-family:var(--mono);font-size:13px}}
footer{{margin-top:72px;padding-top:16px;border-top:1px solid var(--rule);color:var(--ink3);font-size:11px;letter-spacing:.04em}}
@media print{{.fb{{display:none}}.map{{overflow:visible}}.wrap{{padding:0}}}}
</style>
</head>
<body>
<div class="wrap">
<header>
  <div>
    <h1>{_h.escape(name)}</h1>
    <div class="legend">{legend}</div>
  </div>
  <div class="meta">
    <b>{_h.escape(type_name or "—")} · {_h.escape(scale)} 档</b>
    生成 {today()} · 注册表指纹 {_h.escape(fp)}
  </div>
</header>

<div class="stats">{hs}</div>
<div class="bar">{bar}</div>

<h2>架构图 · 箭头指向被依赖方（依赖者 → 被依赖者）</h2>
<div>{filt}</div>
<div class="map">
<svg id="map" viewBox="0 0 {W_} {H_}" width="{W_}" role="img">
<defs>
  <marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto">
    <path d="M1 1L7 5L1 9" fill="none" stroke="#B4B2A9" stroke-width="1.2" stroke-linecap="round"/>
  </marker>
</defs>
{"".join(ed)}
{"".join(nd)}
</svg>
</div>
<div class="legend" style="color:var(--ink3)">点节点可钉住高亮（再点取消）· 悬停临时高亮 · 左侧按钮按状态过滤</div>
{detail}

<h2>被依赖最多</h2>
<div class="two">
  <div>{top_html or '<div class="tn">—</div>'}</div>
  <div style="color:var(--ink2)">被依赖数高 = 改动成本最高：它的接口一变，依赖它的那一串都要跟着走。超过 5 个，值得在注册表里拆成两个 ID。</div>
</div>

<h2>全部资产</h2>
<table>
<thead><tr><th>ID</th><th>语义名</th><th>状态</th><th>位置</th><th>依赖</th></tr></thead>
<tbody>{rows_html}</tbody>
</table>

<footer>
  由 <code style="background:none;padding:0">registry.py viz</code> 从 <code style="background:none;padding:0">.project/registry/index.md</code> 生成 · 事实源是注册表，不要手改这份 HTML。
  要给机器读的出口是 <code style="background:none;padding:0">registry.py export</code> 产出的 manifest.json。
</footer>
</div>
<script>
(function () {{
  var svg = document.getElementById("map");
  if (!svg) return;
  var nds = [].slice.call(svg.querySelectorAll(".nd"));
  var eds = [].slice.call(svg.querySelectorAll(".ed"));
  var pinned = null;

  function neig(id) {{
    var s = {{}}, i;
    for (i = 0; i < eds.length; i++) {{
      if (eds[i].dataset.src === id) {{ s[eds[i].dataset.dst] = 1; }}
      if (eds[i].dataset.dst === id) {{ s[eds[i].dataset.src] = 1; }}
    }}
    return s;
  }}
  function clear() {{
    nds.concat(eds).forEach(function (e) {{ e.classList.remove("hl", "dim"); }});
    svg.classList.remove("focus");
    if (pinned) {{
      var link = neig(pinned.dataset.id);
      nds.forEach(function (n) {{
        if (n.dataset.id === pinned.dataset.id || link[n.dataset.id]) n.classList.add("hl");
      }});
      eds.forEach(function (e) {{
        if (e.dataset.src === pinned.dataset.id || e.dataset.dst === pinned.dataset.id) e.classList.add("hl");
      }});
      svg.classList.add("focus");
    }}
  }}
  function focus(id) {{
    clear();
    if (pinned && pinned.dataset.id === id) return;
    var link = neig(id);
    nds.forEach(function (n) {{
      if (n.dataset.id === id || link[n.dataset.id]) n.classList.add("hl");
    }});
    eds.forEach(function (e) {{
      if (e.dataset.src === id || e.dataset.dst === id) e.classList.add("hl");
    }});
    svg.classList.add("focus");
  }}
  nds.forEach(function (n) {{
    n.addEventListener("mouseenter", function () {{ focus(n.dataset.id); }});
    n.addEventListener("mouseleave", function () {{ clear(); }});
    n.addEventListener("click", function () {{
      if (pinned === n) {{ pinned = null; }} else {{ pinned = n; }}
      clear();
    }});
  }});

  var off = {{}};
  [].slice.call(document.querySelectorAll(".fb")).forEach(function (b) {{
    b.addEventListener("click", function () {{
      off[b.dataset.f] = !off[b.dataset.f];
      b.classList.toggle("off");
      nds.forEach(function (n) {{ n.style.display = off[n.dataset.st] ? "none" : ""; }});
      document.querySelectorAll("tbody tr").forEach(function (r) {{
        r.style.display = (r.dataset.st && off[r.dataset.st]) ? "none" : "";
      }});
    }});
  }});
}})();
</script>
</body>
</html>
"""
    return T


def render_arch_html(name, type_name, scale, assets, edges, dangling, cycles, god, fp,
                     viol=None):
    g = _arch_geometry(assets, edges, dangling, cycles, viol)
    nd = _arch_nodes_svg(g)
    ed = _arch_edges_svg(g)
    bar, legend, filt, hs, detail, rows_html, top_html = _arch_stats_html(
        g, assets, cycles, god, dangling, viol)
    return _arch_html(name, type_name, scale, fp, g["W_"], g["H_"], ed, nd,
                      bar, legend, filt, hs, detail, top_html, rows_html)

def cmd_viz(a):
    """注册表 → 一张自包含的架构图 HTML（零依赖，双击就开）。"""
    root = Path(a.root) if getattr(a, "root", None) else find_root()
    if not root:
        die("未找到 .project，先 init")
    p = proj(root)
    reg = Registry(root, lock=True)
    name = project_name(p, root)

    assets = [(d.get("ID", ""), d.get("语义名", ""), d.get("状态", ""), d.get("位置", ""))
              for _, _, d in reg.rows if d.get("ID")]
    nodes = {a[0] for a in assets}
    edges, dangling = [], []
    for _, _, d in reg.rows:
        src = d.get("ID", "")
        if not src:
            continue
        for dep in _deps_of(d.get("依赖", "")):
            if dep == src:
                continue
            edges.append((src, dep))
            if dep not in nodes and dep not in dangling:
                dangling.append(dep)

    dep = {i: [] for i in nodes}
    for s, d in edges:
        if s in dep and d in nodes:
            dep[s].append(d)
    cycles = find_cycles(nodes, dep)
    # 与 graph 同源同口径（曾经两边各算一遍、各写一个阈值，会报出两个数）
    god = god_assets(nodes, edges)

    vio = arch_violations(root)
    out = Path(a.out) if getattr(a, "out", None) else p / "registry" / "architecture.html"
    html = render_arch_html(name, type_of(p), scale_of(p), assets, edges,
                            dangling, cycles, god, rows_fingerprint(reg), vio)
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(out, html)
    print(f"已生成 {out}")
    print(f"  资产 {len(assets)} / 依赖边 {len(edges)} / 循环依赖 {len(cycles)}"
          f" / 上帝资产 {len(god)} / 悬空引用 {len(dangling)}")
    if vio is None:
        print("  层间违规 —（没找到 .project/architecture.json；跑 registry.py audit --init 生成规则骨架）")
    else:
        _nd, _ni = len(vio.get("declared") or []), len(vio.get("imports") or [])
        _ni_s = "—" if vio.get("imports") is None else str(_ni)
        print(f"  层间违规（资产级边）声明源 {_nd} / 导入源 {_ni_s}"
              + ("（两源一致）" if (vio.get("imports") is not None and _nd == _ni)
                 else "（两源不同是正常的：一个吃注册表声明、一个吃代码导入）"))
        if vio.get("other"):
            print(f"  其他体检（循环/出度/未归类/未解析）{vio['other']} 条，见 architecture-audit.md")
        if vio.get("unmapped"):
            print(f"  其中 {vio['unmapped']} 条导入级违规发生在未登记文件之间，图上无对应边（看 architecture-audit.md）")
    print("  双击用浏览器打开即可看；这是给人看的呈现层，机器读的出口仍是 export 的 manifest.json。")


# ============================================================================
# 导入级代码理解（scan）· 架构校验（audit）· 快照差异（diff）
#
# 分工：
#   scan   读 src，正则解析 import，产出「谁依赖谁」的**文件级**依赖图
#   audit  拿依赖图（或注册表依赖）对照 .project/architecture.json 的层间规则，判违规
#   diff   两份 manifest 对比 → added / removed / changed / moved + 机器收据
#
# 诚实边界（同时写进产物与文档）：导入级解析靠正则，**动态导入（eval / importlib /
# require 变量）、重导出、路径别名、条件导入会漏**。符号级（函数 / 类 / 引用 /
# 类型层级）本工具不做——那是语言服务器的活，应外接 Serena（见 modules/04）。
# ============================================================================

CFG_NAME = "architecture.json"
ARCH_SCHEMA = "project-registry/architecture@1"
IMPORTS_SCHEMA = "project-registry/imports@1"
AUDIT_SCHEMA = "project-registry/audit@1"
DELTA_SCHEMA = "project-registry/delta@1"
ASSET_REF_RE = re.compile(r"^[A-Z][A-Z0-9_-]{0,5}-\d{1,6}$")

_JS_EXTS = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".svelte"}
_RESOLVE_SUF = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte", ".py",
                ".go", ".rs", ".java", ".kt", ".cs"]

_PAT_JS = [
    re.compile(r"""\bimport\s+(?:type\s+)?(?:[^'"\n]*?\sfrom\s*)?['"]([^'"]+)['"]"""),
    re.compile(r"""\bexport\s+(?:type\s+)?[^'"\n]*?\sfrom\s*['"]([^'"]+)['"]"""),
    re.compile(r"""\brequire\(\s*['"]([^'"]+)['"]\s*\)"""),
    re.compile(r"""\bimport\(\s*['"]([^'"]+)['"]\s*\)"""),
]
_PAT_PY_IMPORT = re.compile(r"^\s*import\s+(.+)$", re.M)
_PAT_PY_FROM = re.compile(r"^\s*from\s+([\.\w]*)\s+import\b", re.M)
_PAT_GO_BLOCK = re.compile(r"\bimport\s*\(([^)]*)\)", re.S)
_PAT_GO_ONE = re.compile(r'\bimport\s+(?:[A-Za-z_.]\w*\s+)?"([^"]+)"')
_PAT_GO_PATH = re.compile(r"""["]([^"\n]+)["]""")
_PAT_JAVA = re.compile(r"^\s*import\s+(?:static\s+)?([\w\.\*]+)\s*;", re.M)
_PAT_RS = re.compile(r"^\s*use\s+([\w:]+)", re.M)
_PAT_CS = re.compile(r"^\s*using\s+([\w\.]+)\s*;", re.M)
_PAT_RB = re.compile(r"""\brequire(_relative)?\s*\(?\s*['"]([^'"]+)['"]""")
_PAT_PHP = re.compile(r"^\s*use\s+([\w\\]+)\s*;", re.M)

_GLOB_CACHE = {}


def _rel(s):
    """归一为项目内相对路径：正斜杠、去前导 ./ 与两端 /。"""
    s = (s or "").replace("\\", "/").strip()
    while s.startswith("./"):
        s = s[2:]
    return s.strip("/")


def glob_to_re(pat):
    """glob → 正则。

    `*` / `?` 不跨目录（fnmatch 的 `*` 会跨，是错的）；`**` 跨目录。
    尾缀 `/**` 额外匹配「它本身」——资产的「位置」是目录（`src/ui`），
    而层规则通常写成 `src/ui/**`，若严格要斜杠开头就对不上，层会全判未归类。
    """
    r = _GLOB_CACHE.get(pat)
    if r is not None:
        return r
    p, tail_any = _rel(pat), False
    if p.endswith("/**"):
        p, tail_any = p[:-3], True
    i, out = 0, []
    while i < len(p):
        c = p[i]
        if c == "*":
            if p[i:i + 2] == "**":
                i += 2
                if p[i:i + 1] == "/":
                    i += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    r = re.compile("^" + "".join(out) + ("(?:/.*)?" if tail_any else "") + "$")
    _GLOB_CACHE[pat] = r
    return r


def glob_match(pat, rel):
    return bool(glob_to_re(pat).match(_rel(rel)))


def count_loc(text, ext):
    """有效行数（去空行 / 行注释 / 块注释）。多行字符串当注释处理，属近似值。"""
    e = ext.lower()
    t = text
    if e == ".py":
        t = re.sub(r'"""[\s\S]*?"""', "", t)
        t = re.sub(r"'''[\s\S]*?'''", "", t)
        lc = "#"
    elif e == ".rb":
        lc = "#"
    else:
        t = re.sub(r"/\*[\s\S]*?\*/", "", t)
        lc = "//"
    n = 0
    for ln in t.splitlines():
        s = ln.strip()
        if s and not s.startswith(lc):
            n += 1
    return n


def extract_imports(ext, text):
    """按扩展名抽 import 目标（原始书写形态，去重保序）。"""
    e, out = ext.lower(), []
    if e in _JS_EXTS:
        for pat in _PAT_JS:
            out.extend(m.group(1) for m in pat.finditer(text))
    elif e == ".py":
        for m in _PAT_PY_IMPORT.finditer(text):
            for piece in m.group(1).split(","):
                spec = piece.strip().split(" as ")[0].strip()
                if spec:
                    out.append(spec)
        for m in _PAT_PY_FROM.finditer(text):
            if m.group(1):
                out.append(m.group(1))
    elif e == ".go":
        for m in _PAT_GO_BLOCK.finditer(text):
            out.extend(_PAT_GO_PATH.findall(m.group(1)))
        out.extend(m.group(1) for m in _PAT_GO_ONE.finditer(text))
    elif e in (".java", ".kt"):
        out.extend(m.group(1) for m in _PAT_JAVA.finditer(text))
    elif e == ".rs":
        out.extend(m.group(1) for m in _PAT_RS.finditer(text))
    elif e == ".cs":
        out.extend(m.group(1) for m in _PAT_CS.finditer(text))
    elif e == ".rb":
        out.extend(m.group(2) for m in _PAT_RB.finditer(text))
    elif e == ".php":
        out.extend(m.group(1) for m in _PAT_PHP.finditer(text))
    seen, res = set(), []
    for s in out:
        s = s.strip()
        if s and s not in seen:
            seen.add(s)
            res.append(s)
    return res


def _join(base, tail):
    parts = []
    for seg in (base + "/" + tail).split("/"):
        if not seg or seg == ".":
            continue
        if seg == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(seg)
    return "/".join(parts)


def _resolve(src_rel, spec, ext, files, dirs):
    """尽力解析一个 import 目标。

    返回 (kind, value)：
      int   项目内命中（value = 文件相对路径，或目录路径）
      ext   外部依赖（value = 包名）
      unres 像本地却没找到（value = 原样 spec）——值得报，通常是路径写错或漏扫描
    """
    raw = (spec or "").strip()
    if not raw:
        return ("skip", "")
    if raw.startswith(("http:", "https:", "data:")):
        return ("ext", raw)
    if raw.startswith("node:"):
        return ("ext", raw)
    raw = raw.split("?")[0].split("#")[0]
    # `e` 必须是扩展名。曾把相对路径赋给 `e`，于是所有语言分支永不命中、
    # 全员退化成最后那条通用兜底：相对导入被误标成外部依赖、「未解析」恒为 0。
    rel = _rel(src_rel)
    d = rel.rsplit("/", 1)[0] if "/" in rel else ""
    e = (ext or "").lower()
    if "." not in Path(raw).name and not raw.startswith("."):
        pass

    def try_cands(base, order):
        if not base:
            return None
        if base in files:
            return base
        for suf in order:
            if base + suf in files:
                return base + suf
        for suf in order:
            if base + "/index" + suf in files:
                return base + "/index" + suf
            if base + "/__init__" + suf in files:
                return base + "/__init__" + suf
        if base in dirs:
            return base
        return None

    if e in _JS_EXTS:
        if raw.startswith("."):
            c = try_cands(_join(d, raw), _RESOLVE_SUF)
            return ("int", c) if c else ("unres", raw)
        if raw.startswith(("@/", "~/")):
            c = try_cands(_join("", raw[2:]), _RESOLVE_SUF)
            return ("int", c) if c else ("unres", raw)
        if raw.startswith("@"):
            return ("ext", "/".join(raw.split("/")[:2]))
        return ("ext", raw.split("/")[0])

    if e == ".py":
        if raw.startswith("."):
            dots = len(raw) - len(raw.lstrip("."))
            rest = raw[dots:].replace(".", "/")
            base = d
            for _ in range(max(0, dots - 1)):
                base = base.rsplit("/", 1)[0] if "/" in base else ""
            c = try_cands(_join(base, rest) if rest else base, [".py"])
            return ("int", c) if c else ("unres", raw)
        tail = raw.replace(".", "/")
        c = try_cands(tail, [".py"])
        if c:
            return ("int", c)
        for pref in ("src", "app", "lib"):
            c = try_cands(_join(pref, tail), [".py"])
            if c:
                return ("int", c)
        return ("ext", raw.split(".")[0])

    if e == ".go":
        c = try_cands(raw, [".go"])
        if c:
            return ("int", c)
        for pref in ("", "src"):
            c = try_cands(_join(pref, raw), [".go"])
            if c:
                return ("int", c)
        return ("ext", raw)

    if e in (".java", ".kt", ".cs"):
        c = try_cands(raw.replace(".", "/").rstrip("/*"), [])
        if c:
            return ("int", c)
        return ("ext", raw.split(".")[0] if raw.split(".")[0] else raw)

    if e == ".rs":
        if raw.startswith(("crate::", "self::", "super::")):
            c = try_cands(raw.replace("::", "/"), [".rs"])
            return ("int", c) if c else ("unres", raw)
        return ("ext", raw.split("::")[0])

    if e in (".rb", ".php"):
        if raw.startswith((".", "/")) or e == ".php":
            c = try_cands(_join(d, raw), [".rb", ".php"])
            if c:
                return ("int", c)
        return ("ext", raw.split("/")[0])

    c = try_cands(_join(d, raw), _RESOLVE_SUF)
    return ("int", c) if c else ("ext", raw)


def scan_project(root, exts=None, cfg=None):
    """扫 src 建导入图。返回 dict（同时是 imports.json 的内容）。

    增量：按 (mtime, size) 指纹复用上次解析结果（缓存落 .project/registry/.scan-cache.json）。
    """
    root = Path(root)
    p = proj(root)
    exts = exts or list(DEFAULT_EXTS)
    exts = [e if e.startswith(".") else "." + e for e in exts]
    extset = {e.lower() for e in exts}

    walked = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [x for x in dns if x not in SKIP_DIRS and not x.startswith(".")]
        for fn in fns:
            if Path(fn).suffix.lower() in extset:
                walked.append(str(Path(dp, fn).relative_to(root)).replace("\\", "/"))
    walked.sort()

    files = set(walked)
    dirs = set()
    for f in walked:
        parts = f.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:i]))

    cache_p = p / "registry" / ".scan-cache.json"
    cache = {}
    if cache_p.exists():
        try:
            cache = json.loads(cache_p.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
    new_cache, hits = {}, 0

    entries, unassigned_specs = [], []
    for rel in walked:
        fp = root / rel
        try:
            st = fp.stat()
        except OSError:
            continue
        ext = Path(rel).suffix.lower()
        key = f"{st.st_mtime_ns}:{st.st_size}"
        old = cache.get(rel)
        if isinstance(old, dict) and old.get("k") == key and old.get("v", {}).get("ext") == ext:
            val = old["v"]
            hits += 1
        else:
            try:
                text = fp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            specs = extract_imports(ext, text)
            val = {"ext": ext, "loc": count_loc(text, ext), "specs": specs}
        new_cache[rel] = {"k": key, "v": val}
        imports = []
        for spec in val["specs"]:
            kind, target = _resolve(rel, spec, ext, files, dirs)
            if kind == "skip":
                continue
            imports.append({"spec": spec, "kind": kind, "target": target})
            if kind == "unres":
                unassigned_specs.append({"path": rel, "spec": spec})
        entries.append({"path": rel, "ext": ext, "loc": val["loc"], "imports": imports})

    try:
        atomic_write(cache_p, json.dumps(new_cache, ensure_ascii=False) + "\n")
    except OSError:
        pass

    ext_count, int_edges = {}, 0
    for e in entries:
        for im in e["imports"]:
            if im["kind"] == "ext":
                ext_count[im["target"]] = ext_count.get(im["target"], 0) + 1
            elif im["kind"] == "int":
                int_edges += 1

    data = {
        "schema": IMPORTS_SCHEMA,
        "project": project_name(p, root),
        "generated": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "root": ".",
        "exts": exts,
        "stats": {
            "files": len(entries),
            "loc": sum(e["loc"] for e in entries),
            "internal_edges": int_edges,
            "external_modules": len(ext_count),
            "unresolved": len(unassigned_specs),
            "cached": hits,
        },
        "files": entries,
        "external_top": sorted(({"name": k, "files": v} for k, v in ext_count.items()),
                               key=lambda x: (-x["files"], x["name"]))[:30],
        "unresolved": unassigned_specs[:200],
    }
    return data, (root, p)


def map_assets_to_files(reg, data):
    """把注册表的「位置」映射到扫描到的文件，并做「声明依赖 vs 实际导入」对账。"""
    files = [e["path"] for e in data["files"]]
    by_path = {e["path"]: e for e in data["files"]}
    owners = {}
    assets = []
    for _, _, d in reg.rows:
        aid = (d.get("ID") or "").strip()
        loc = _rel(d.get("位置", ""))
        own = [f for f in files if loc and (f == loc or f.startswith(loc + "/"))]
        for f in own:
            prev = owners.get(f)
            if prev is None or len(loc) > len(prev[1]):
                owners[f] = (aid, loc)
        assets.append({"id": aid, "name": d.get("语义名", ""), "path": loc,
                       "files": own, "targets": [],
                       "status": (d.get("状态") or "").strip()})

    # 「位置」写了但一个文件都没扫到 —— 必须**分成三类**，混成一类就是纯噪声：
    #   ⓪ 构思中：状态是「构思」= 开工前先登记，位置只是意向，还没落盘。**先把它摘走**，
    #      否则"先登记再动手"会被自己的对账报告骂成「登记的东西不见了」。
    #   ① 非源码资产：路径在磁盘上真实存在，只是后缀不在扫描范围（.md / .tpl / .json…）。
    #      文档型项目（技能包、写作、研究）里这是**常态**——把 17 个资产里的 16 个
    #      都报成「位置对不上」，使用者第一眼就会把整段当废纸（狗粮自审时本技能自己撞上）。
    #   ② 真幽灵：路径在磁盘上根本不存在 —— 路径写错 / 文件被删。这才是要报的。
    extset = {str(e).lower() for e in (data.get("exts") or [])}
    ghosts, noncode, planned, planned_landed = [], [], [], []
    for a in assets:
        if a.get("status") == PLANNED_STATUS:
            # 构思里「位置已有源码文件」的单独记账：它其实落盘了，只是状态没跟上。
            (planned_landed if a["files"] else planned).append(a)
            continue
        if not a["path"] or a["files"]:
            continue
        tgt = reg.root / a["path"]
        if not tgt.exists():
            ghosts.append(a)
        elif tgt.is_file() and tgt.suffix.lower() in extset:
            ghosts.append(a)        # 后缀在扫描范围内却没扫到 → 另有原因，仍报
        else:
            noncode.append(a)

    observed = {}
    for a in assets:
        tg = set()
        for f in a["files"]:
            for im in by_path.get(f, {}).get("imports", []):
                if im["kind"] != "int" or not im["target"]:
                    continue
                t = im["target"]
                cand = [t] if t in by_path else [x for x in files if x.startswith(t + "/")]
                for c in cand[:200]:
                    o = owners.get(c)
                    if o and o[0] != a["id"]:
                        tg.add(o[0])
                        observed.setdefault((a["id"], o[0]), f"{f} → {c}")
        a["targets"] = sorted(tg)

    declared = {}
    for _, _, d in reg.rows:
        aid = (d.get("ID") or "").strip()
        declared[aid] = set(x.strip() for x in re.split(r"[,，、/]+", d.get("依赖", "") or "")
                            if x.strip())

    # 「多声明」只在**两端都是源码资产**时才有意义：文档资产没有 import，
    # 拿它去比「代码里怎么没导」必然 100% 命中，28 条声明会被全报成多声明（纯噪声）。
    src_ids = {a["id"] for a in assets if a["files"]}
    missing, extra = [], []
    for a in assets:
        for t in a["targets"]:
            if t not in declared.get(a["id"], set()):
                missing.append({"from": a["id"], "to": t, "evidence": observed.get((a["id"], t), "")})
        if a["id"] not in src_ids:
            continue
        for t in sorted(declared.get(a["id"], set())):
            if t not in a["targets"] and t in src_ids:
                extra.append({"from": a["id"], "to": t})
    data["assets"] = assets
    data["ghosts"] = [{"id": a["id"], "path": a["path"]} for a in ghosts]
    data["noncode"] = [{"id": a["id"], "path": a["path"], "name": a["name"]} for a in noncode]
    # 构思两类不进 ghosts：一个还没落盘（正常），一个已落盘但状态没跟上（提示改状态）。
    data["planned"] = [{"id": a["id"], "path": a["path"], "name": a["name"]} for a in planned]
    data["planned_landed"] = [a["id"] for a in planned_landed]
    data["declared_vs_imported"] = {"missing": missing, "extra": extra}
    return data


def render_imports_md(data):
    s = data["stats"]
    L = [f"# 导入图 · {data['project']} · {today()}", "",
         "> 由 `registry.py scan` 生成，勿手改。**导入级**事实，不是符号级。", "",
         "## 体格", "",
         f"- 扫描文件 {s['files']} / 有效行 {s['loc']}（近似）",
         f"- 项目内依赖边 {s['internal_edges']} / 外部模块 {s['external_modules']}",
         f"- 相对导入未解析 {s['unresolved']} / 命中增量缓存 {s['cached']}",
         f"- 后缀：{'、'.join(data['exts'])}", ""]
    dv = data.get("declared_vs_imported") or {}
    n_src = len([a for a in data.get("assets", []) if a.get("files")])
    n_all = len(data.get("assets", []))
    L += ["## 声明依赖 vs 实际导入", "",
          f"- 对账范围：**只比有源码文件的资产**（{n_src} / {n_all}）。"
          "文档 / 模板型资产没有 import 可比，它们的依赖不进这两张表。", ""]
    L.append(f"- 实际导入了但**没声明**（{len(dv.get('missing', []))} 条，优先看这批）：")
    for x in dv.get("missing", [])[:40]:
        L.append(f"  - {x['from']} → {x['to']}　`{x.get('evidence', '')}`")
    if not dv.get("missing"):
        L.append("  - 无")
    L.append(f"- 声明了但代码里**没看到导入**（{len(dv.get('extra', []))} 条，可能合规的软依赖）：")
    for x in dv.get("extra", [])[:40]:
        L.append(f"  - {x['from']} → {x['to']}")
    if not dv.get("extra"):
        L.append("  - 无")
    L += ["", "## 外部模块 Top", ""]
    for x in data.get("external_top", [])[:20]:
        L.append(f"- {x['name']}（{x['files']} 处）")
    if not data.get("external_top"):
        L.append("- 无")
    nc = data.get("noncode") or []
    if nc:
        L += ["", f"## 非源码资产（{len(nc)} 个，不参与导入对账）", "",
              "位置在磁盘上**存在**，只是后缀不在本次扫描范围——文档 / 模板 / 配置型资产落这里，"
              "**这不是问题**。", ""]
        for x in nc[:40]:
            L.append(f"- {x['id']}　`{x['path']}`")
    pl = data.get("planned") or []
    if pl:
        L += ["", f"## 构思中未落盘（{len(pl)} 个：位置是意向，别当幽灵）", "",
              "「构思」= 开工前先登记，位置只是打算放哪儿，**还没写**。这里既不是问题，"
              "也不参与上面的导入对账。", ""]
        for x in pl[:40]:
            L.append(f"- {x['id']}　`{x['path'] or '（位置未定）'}`")
    pll = data.get("planned_landed") or []
    if pll:
        L += ["", f"## 构思但已有源码文件（{len(pll)} 个：状态该更新了）", "",
              "这些资产的位置底下已经扫到文件了——说明落了盘。"
              "`registry.py set <ID> --status 在建`。", ""]
        L.append("- " + "、".join(pll[:40]))
    if data.get("ghosts"):
        L += ["", f"## 位置对不上（{len(data['ghosts'])} 个：磁盘上找不到登记的路径）", "",
              "路径写错 / 文件被删。这比上面那类严重：上面是「不归我扫」，这个是**登记的东西不见了**。", ""]
        for x in data["ghosts"][:30]:
            L.append(f"- {x['id']}　`{x['path']}`")
    if data.get("unresolved"):
        L += ["", "## 相对导入未解析（前 50）", ""]
        for x in data["unresolved"][:50]:
            L.append(f"- `{x['path']}` → `{x['spec']}`")
    L += ["", "## 已知漏检（读这份报告时必须知道）", "",
          "- 动态导入（`eval` / `importlib.import_module` / `require(变量)`）抓不到。",
          "- 重导出（`export * from`）与路径别名（tsconfig paths）若不写成 `@/` 或 `~/` 会当外部模块。",
          "- 注释与字符串里的 import 会被误当真实导入（正则法的固有代价）。",
          "- 符号级信息（函数 / 类 / 引用 / 类型层级）本工具不产出，需 Serena（见 modules/04）。", ""]
    return "\n".join(L)


def cmd_scan(a):
    """导入级代码理解：解析 import → 文件级依赖图 + 行数统计 + 对账。"""
    root = Path(a.root) if getattr(a, "root", None) else find_root()
    if not root:
        die("未找到 .project，先 init")
    exts = [e.strip() for e in a.ext.split(",") if e.strip()] if getattr(a, "ext", "") else None
    data, (root, p) = scan_project(root, exts)
    reg = Registry(root, lock=True)
    data = map_assets_to_files(reg, data)
    out_json = Path(a.json_out) if getattr(a, "json_out", None) else p / "registry" / "imports.json"
    out_md = Path(a.out) if getattr(a, "out", None) else p / "registry" / "imports.md"
    atomic_write(out_json, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    atomic_write(out_md, render_imports_md(data))
    s = data["stats"]
    dv = data["declared_vs_imported"]
    print(f"已生成 {out_json}")
    print(f"已生成 {out_md}")
    print(f"  文件 {s['files']} / 有效行 {s['loc']} / 项目内边 {s['internal_edges']}"
          f" / 外部模块 {s['external_modules']} / 未解析 {s['unresolved']}")
    print(f"  漏声明 {len(dv['missing'])} / 多声明 {len(dv['extra'])}"
          f" / 位置对不上 {len(data.get('ghosts', []))}"
          f" / 非源码资产 {len(data.get('noncode', []))}"
          + (f" / 构思未落盘 {len(data.get('planned', []))}" if data.get("planned") else "")
          + (f" / 构思但已落盘 {len(data.get('planned_landed', []))}"
             if data.get("planned_landed") else ""))
    if s["unresolved"]:
        print("  提示：相对导入未解析通常是路径写错或后缀不在扫描范围内（--ext 可覆盖）。")


def load_arch_cfg(p):
    fp = p / CFG_NAME
    if not fp.exists():
        return None
    try:
        cfg = json.loads(fp.read_text(encoding="utf-8"))
    except Exception as e:
        die(f"{fp} 不是合法 JSON：{e}")
    if not isinstance(cfg, dict):
        die(f"{fp} 顶层必须是对象")
    return cfg


def _layer_paths(v, where):
    """一个层的 `paths` → glob 列表。字符串算一条，数组算多条，其余不认。"""
    if isinstance(v, str):
        return [v] if v.strip() else []
    if isinstance(v, list):
        return [str(x) for x in v if isinstance(x, str) and x.strip()]
    die(f"{where} 的 `paths` 是 {type(v).__name__}，必须是字符串或字符串数组。"
        f"正确写法见 `registry.py audit --init` 生成的骨架。")


def layer_defs(cfg):
    """`layers` 的**唯一读法** → [(层名, [glob, ...])]。两种写法都认：

    ① 数组 + 对象（文档推荐）：`[{"name": "api", "paths": ["src/api/**"]}]`
    ② 对象简写（层名 → 路径）：`{"api": "src/api/**"}`

    认不出就 die，绝不静默跳过——跳过的后果是「0 层命中」，
    于是每条规则都走 default（默认 allow），报告一片绿，
    而层间校验其实**一次都没生效**。那种"没报问题"比报错糟得多。
    """
    raw = (cfg or {}).get("layers")
    if raw is None:
        return []
    out = []
    if isinstance(raw, dict):
        for name, paths in raw.items():
            out.append((str(name).strip(), _layer_paths(paths, f"层 `{name}`")))
    elif isinstance(raw, list):
        for i, lay in enumerate(raw):
            if not isinstance(lay, dict):
                die(f"`layers` 第 {i + 1} 条是 {type(lay).__name__}，必须是对象："
                    f'{{"name": "层名", "paths": ["glob"]}}。'
                    f"想写「层名 → 路径」的简写，就把整个 `layers` 写成对象，不要写成数组。"
                    f"正确写法见 `registry.py audit --init` 生成的骨架。")
            name = str(lay.get("name") or "").strip()
            if not name:
                die(f"`layers` 第 {i + 1} 条缺 `name`。")
            out.append((name, _layer_paths(lay.get("paths", []), f"层 `{name}`")))
    else:
        die(f"`layers` 是 {type(raw).__name__}，必须是数组或对象。"
            f"正确写法见 `registry.py audit --init` 生成的骨架。")
    for name, paths in out:
        if not paths:
            die(f"层 `{name}` 没有 `paths`：没有路径就永远匹配不到任何文件，"
                f"这条层规则等于不生效，而报告上照样看不出来。补 paths，或删掉这一层。")
    return out


def layer_of(cfg, rel):
    """路径 → 层名。多个 glob 命中时取**最长匹配**（最具体的那条），同长取配置靠前的。"""
    best, best_len = "", -1
    for name, paths in layer_defs(cfg):
        for pat in paths:
            if glob_match(pat, rel):
                pl = len(_rel(pat))
                if pl > best_len:
                    best, best_len = name, pl
    return best


def _rule_match(cfg, token, layer, ref):
    if token == "*":
        return True
    if isinstance(token, str) and ASSET_REF_RE.match(token.strip()):
        return glob_match(token.strip(), ref) or ref == token.strip()
    if token == layer or (layer and token == layer):
        return True
    return glob_match(token, ref) if token else False


def eval_rules(cfg, src_layer, dst_layer, src_ref, dst_ref, src_path, dst_path):
    """按顺序匹配规则，**第一条命中生效**（写法：先例外、后总则）。返回 (allowed, why)。"""
    for i, r in enumerate(cfg.get("rules", []) or []):
        frm, to = r.get("from", "*"), r.get("to", "*")
        toks = to if isinstance(to, list) else [to]
        if not _rule_match(cfg, frm, src_layer, src_ref):
            continue
        if any(_rule_match(cfg, t, dst_layer, dst_ref) for t in toks):
            allow = bool(r.get("allow", True))
            tag = f"rules[{i}] {frm} → {to} = {'允许' if allow else '禁止'}"
            return allow, tag
    dflt = str(cfg.get("default", "allow")).lower()
    return dflt != "deny", f"default = {dflt}"


def audit_engine(root, cfg, data=None, reg=None):
    """产出 findings。data 为 None 或 source=registry 时改用注册表声明的依赖。"""
    p = proj(root)
    # 配置在碰第一个文件之前先校验：坏配置必须当场炸。
    # 不能等到扫到文件才炸——空项目 / 纯文档项目扫不到文件，坏配置就被藏起来了。
    if cfg:
        layer_defs(cfg)
    src_mode = "imports" if data else "registry"
    edges, layers, ref_files = [], {}, {}
    unassigned, unresolved, ghosts = [], [], []
    nm, loc = {}, {}

    if data:
        for e in data["files"]:
            if cfg:
                ly = layer_of(cfg, e["path"])
                if ly:
                    layers[e["path"]] = ly
                else:
                    unassigned.append(e["path"])
            for im in e["imports"]:
                if im["kind"] == "int" and im["target"]:
                    edges.append((e["path"], im["target"], f"{e['path']} 里 `{im['spec']}`"))
                elif im["kind"] == "unres":
                    unresolved.append((e["path"], im["spec"]))
        ghosts = list(data.get("ghosts", []))
    else:
        rows = (reg or Registry(root, lock=True)).rows
        for _, _, d in rows:
            aid = (d.get("ID") or "").strip()
            nm[aid] = d.get("语义名", "")
            loc[aid] = _rel(d.get("位置", ""))
        ref_files = loc
        for _, _, d in rows:
            aid = (d.get("ID") or "").strip()
            for dep in re.split(r"[,，、/]+", d.get("依赖", "") or ""):
                dep = dep.strip()
                if not dep:
                    continue
                if dep not in nm:
                    edges.append((aid, dep, f"{aid} 声明依赖 {dep}（未登记）"))
                    continue
                edges.append((aid, dep, f"{aid} 声明依赖 {dep}"))
        if cfg:
            for aid, lc in loc.items():
                if lc:
                    layers[aid] = layer_of(cfg, lc)

    # 节点集合（两种事实源各自的"资产"）：环检测与出度都基于它。
    # 出度走 _fanout_map —— 与 graph / viz 同一个口径（曾经这里不去重、算自环，
    # 于是 audit 的「出度超限」和 graph 的「上帝资产」对同一个资产给出不同读数）。
    cnodes = [e["path"] for e in data["files"]] if data else sorted(nm)
    fan = _fanout_map(cnodes, [(s, t) for s, t, _ in edges])

    findings = []
    if cfg:
        for s, t, ev in edges:
            sl = layers.get(s, "") if data else layers.get(s, "")
            tl = layers.get(t, "") if data else layers.get(t, "")
            if data and (not sl or not tl):
                continue
            ok, why = eval_rules(cfg, sl, tl, s, t,
                                 ref_files.get(s, ""), ref_files.get(t, ""))
            if not ok:
                findings.append({"kind": "layer", "from": s, "to": t, "src_layer": sl,
                                 "dst_layer": tl, "why": why, "evidence": ev})

    maxf = int(cfg.get("maxFanout", 0) or 0) if cfg else 0
    if maxf:
        for s, ts in sorted(fan.items()):
            if len(ts) > maxf:
                findings.append({"kind": "fanout", "from": s, "to": "",
                                 "src_layer": layers.get(s, ""), "dst_layer": "",
                                 "why": f"依赖 {len(ts)} 个目标 > maxFanout {maxf}",
                                 "evidence": "、".join(sorted(ts)[:12])})

    # 找环两种事实源共用：只在其中一种里做，同一个命令会给出两种行为。
    cset = set(cnodes)
    cdep = {}
    for s, t, _ in edges:
        if t in cset:
            cdep.setdefault(s, []).append(t)
    for c in find_cycles(cnodes, cdep):
        findings.append({"kind": "cycle", "from": c, "to": "", "src_layer": "",
                         "dst_layer": "",
                         "why": "代码级循环依赖（文件级）" if data else "注册表声明的循环依赖",
                         "evidence": c})

    if data:
        for f in unassigned:
            findings.append({"kind": "unassigned", "from": f, "to": "", "src_layer": "",
                             "dst_layer": "", "why": "不属于任何已配置的层", "evidence": ""})
        for f, sp in unresolved:
            findings.append({"kind": "unresolved", "from": f, "to": "", "src_layer": "",
                             "dst_layer": "", "why": f"相对导入解析不到：`{sp}`", "evidence": ""})

    # ghost 曾经只活在 stats 里（`ghosts: 1`），findings 里永远没有它——而文档把它列为
    # 六类判定之一，用户按类去找永远找不到，报告里实为五类。补成正经 finding。
    # 不影响 --strict 的退出码：闸门只看 layer / cycle / fanout。
    for g in ghosts:
        if isinstance(g, dict):
            gid, gpath = g.get("id", ""), g.get("path", "")
        else:
            gid, gpath = "", str(g)
        findings.append({"kind": "ghost", "from": gid, "to": "", "src_layer": "",
                         "dst_layer": "", "why": f"登记了位置但扫不到文件：`{gpath}`",
                         "evidence": gpath})

    return {"source": src_mode, "findings": findings,
            "stats": {"files": len(data["files"]) if data else len(ref_files),
                      "edges": len(edges), "layers": len(set(layers.values())),
                      "unassigned": len(unassigned), "unresolved": len(unresolved),
                      "ghosts": len(ghosts)}}


def audit_no_facts(res):
    """架构闸门「无事实可判」的判定 —— **静默的绿比没有闸门更危险**。

    为什么单列一条：没有任何依赖边时，layer / cycle / fanout 三类判定都无从产生，
    `audit --strict` 必然 0 违规、rc=0 —— 它看起来是「通过了」。知识型项目（技能包 /
    文档 / 写作）这是常态：`scan` 扫不到 import，于是「文件 0 / 边 0」。文档已经诚实写了
    「0 违规只等于没东西可查」，但**这个绿本身没有信号**，要人自己去比对「文件 0」那个
    数字。配了规则却无事实，比「没配规则」更危险——后者有明确警告，前者看起来是绿的。

    不改退出码：知识型项目没有代码不是错误。返回 (bool, 说明)，说明带上两个数字，
    便于判断该走哪条路。
    """
    s = res["stats"]
    if res["source"] == "imports":
        if s["files"] < 2 or s["edges"] == 0:
            return True, f"文件 {s['files']} / 项目内边 {s['edges']}"
        return False, ""
    if s["edges"] == 0:
        return True, f"资产 {s['files']} / 声明依赖 {s['edges']}"
    return False, ""


def render_audit_md(project, res, cfg, cfg_path, vb=None):
    F = res["findings"]
    def of(k):
        return [x for x in F if x["kind"] == k]
    viol, cyc, fan = of("layer"), of("cycle"), of("fanout")
    una, unr = of("unassigned"), of("unresolved")
    L = [f"# 架构校验 · {project} · {today()}", "",
         "> 由 `registry.py audit` 生成，勿手改。", ""]
    if not cfg:
        L += [f"## ⚠ 未找到 `{cfg_path}`，只做了通用体检", "",
              "层间规则需要一个显式配置。生成骨架：", "",
              "```bash", "registry.py audit --init", "```", "",
              "生成的骨架里 `layers` 按源码目录自动列出，`rules` 只有一条「全部允许」，",
              "**它默认不会报任何层间违规**——规则要你自己收紧（先例外、后总则）。", ""]
    else:
        L += [f"## 规则来源 `{cfg_path}`", "",
              f"- 层 {len(cfg.get('layers', []))} 个 / 规则 {len(cfg.get('rules', []))} 条"
              f" / 未命中时默认 `{cfg.get('default', 'allow')}`"
              f" / maxFanout `{cfg.get('maxFanout', 0)}`", "",
              "匹配语义：**按顺序，第一条命中生效**——所以「先写例外、后写总则」。", ""]
    L += [f"## 事实来源：{'导入级扫描（scan）' if res['source'] == 'imports' else '注册表声明依赖'}", ""]
    src_note = ("- 事实是**导入级**的：动态导入 / 别名 / 重导出会漏，可能漏报违规。"
                if res["source"] == "imports" else
                "- 事实来自**注册表声明的依赖**（人写的）：代码里真实存在、注册表没登记的边"
                "不在本报告里——那类要靠 `scan` 的对账段查。")
    s = res["stats"]
    L += [f"- 文件 {s['files']} / 依赖边 {s['edges']} / 命中层 {s['layers']} 个", ""]
    _nf, _nf_why = audit_no_facts(res)
    if _nf:
        L += [f"## ⚠ 本次无事实可判（`{_nf_why}`）", "",
              "没有依赖边，`layer` / `cycle` / `fanout` **三类判定都无从产生**，"
              "所以下面必然全是 0。",
              "**这份绿是「没东西可查」，不是「查过了没问题」。**", "",
              "知识型 / 文档型项目（技能包、文档、写作）请改用注册表声明依赖作为事实源：", "",
              "```bash", "registry.py audit --source registry", "```", ""]

    L += [f"## 1 层间违规（{len(viol)}）", ""]
    if viol:
        for v in viol[:60]:
            L.append(f"- `{v['from']}` → `{v['to']}`　〔{v['src_layer'] or '?'} → "
                     f"{v['dst_layer'] or '?'}〕　{v['why']}")
            if v.get("evidence"):
                L.append(f"    - 证据：{v['evidence']}")
    else:
        L.append("- 无" if cfg else "- 未配置规则，跳过")
    if vb is not None:
        _nd, _ni = len(vb.get("declared") or []), vb.get("imports")
        _al = [f"- 声明源（注册表声明的依赖）：**{_nd}** 条资产级边"]
        if _ni is None:
            # 没取导入源时别说「两个数不相等是正常的」——那会让人以为有两个数。
            _al.append("- 导入源：本次没 scan（`--source registry`），未取")
            _al.append("- 想同时看到两个数，跑不带 `--source` 的 `audit`（默认走导入级）。")
        else:
            _al.append(f"- 导入源（代码里真实解析出的导入）：**{len(_ni)}** 条资产级边")
            _al.append("- 两个数**不相等是正常的**：一个吃「人写的声明」，一个吃「代码的实际导入」；"
                       "相等才值得留意（说明两边已同步）。")
        _al.append("- 页面上红边取两源的并集；徽章分别报两个数，与本节一致。")
        L += ["", "## 1b 折算到资产级（与 `viz` 页面对齐用）", ""] + _al + [""]
    L += ["", f"## 2 代码级循环依赖（{len(cyc)}）", ""]
    # 注意：这里只能有一处「空则写 - 无」——曾经 `[...] or ["- 无"]` 与随后的
    # `if not cyc:` 叠加，空结果会打印两条「- 无」（狗粮自审时肉眼看出）。
    L += [f"- {x['evidence']}" for x in cyc[:20]] or ["- 无"]
    L += ["", f"## 3 出度超限（{len(fan)}）", ""]
    if fan:
        for x in fan[:30]:
            L.append(f"- `{x['from']}`：{x['why']}")
            L.append(f"    - {x['evidence']}")
    else:
        L.append("- 无" if cfg else "- 未配置 maxFanout，跳过")
    L += ["", f"## 4 不属于任何层（{len(una)}）", ""]
    L += [f"- `{x['from']}`" for x in una[:30]]
    if not una:
        L.append("- 无" if cfg else "- 未配置 layers，跳过")
    L += ["", f"## 5 相对导入未解析（{len(unr)}）", ""]
    L += [f"- `{x['from']}`：{x['why']}" for x in unr[:30]]
    if not unr:
        L.append("- 无")
    gho = of("ghost")
    if gho:
        L += ["", f"## 6 位置对不上（{len(gho)}）", "",
              "登记了「位置」但一个文件也没扫到 —— 路径写错、后缀不在扫描范围、或已被删。", ""]
        L += [f"- `{x['from']}`　`{x['evidence']}`" for x in gho[:30]]
    L += ["", "## 判据说明（别误读）", "",
          "- 层间违规是**结构性**判断，不是 bug 清单：有些是刻意的（如 DI 容器、事件总线）。",
          "- `from` 是依赖者、`to` 是被依赖者；违规 = 规则拒绝这条边。",
          src_note,
          "- 「不属于任何层」「相对导入未解析」「位置对不上」三类**只在导入级事实源下产出**"
          "（`--source registry` 没有文件级信息，报不出来）——所以换事实源时报告会少三段，"
          "那是预期的，不是坏了。",
          "- 想改判：改 `.project/architecture.json` 的 `rules`（先例外后总则），再重跑。", ""]
    return "\n".join(L)


def arch_template(root):
    """生成一份可用的骨架：层按源码目录自动列出，规则只有一条全允许。"""
    root = Path(root)
    extset = {e.lower() for e in DEFAULT_EXTS}
    top = []
    for base in ("src", "app", "source", "lib"):
        b = root / base
        if not b.is_dir():
            continue
        subs = []
        for d in sorted(b.iterdir()):
            if not d.is_dir() or d.name in SKIP_DIRS or d.name.startswith("."):
                continue
            if any(f.suffix.lower() in extset for f in d.rglob("*") if f.is_file()):
                subs.append(f"{base}/{d.name}")
        if subs:
            top = subs
            break
    if not top:
        # 兜底：布局不用 src/app/source/lib 这些约定名时（技能包、文档项目、工具仓库常见），
        # 取「顶层目录里直接含源码文件」的那些，用目录名当层名。否则 --init 会给出 0 层，
        # 使用者只能自己手填——本技能自己就撞上过这一点（目录是 modules/ scripts/ references/）。
        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name in SKIP_DIRS or d.name.startswith("."):
                continue
            if any(f.suffix.lower() in extset for f in d.rglob("*") if f.is_file()):
                top.append(d.name)
    layers = [{"name": s.split("/")[-1], "paths": [s + "/**"]} for s in top]
    return {
        "schema": ARCH_SCHEMA,
        "default": "allow",
        "layers": layers,
        "rules": [{"from": "*", "to": "*", "allow": True}],
        "maxFanout": 8,
        "_note": ("audit --init 生成的骨架。layers 按源码目录列出；rules 只有一条「全部允许」，"
                  "所以现在跑 audit 不会报层间违规——规则要你自己收紧。写法：按顺序匹配、"
                  "第一条命中生效，所以**先写例外、后写总则**。例："
                  "[{\"from\":\"ui\",\"to\":\"ui\",\"allow\":true},"
                  "{\"from\":\"ui\",\"to\":\"api\",\"allow\":true},"
                  "{\"from\":\"ui\",\"to\":\"*\",\"allow\":false}]。"
                  "把 default 设成 deny 可以反过来：默认禁、只放行写明的。"),
    }


def cmd_audit(a):
    """架构校验：层间依赖规则 + 循环 + 出度 + 未归类，出报告与机器可读 JSON。"""
    root = Path(a.root) if getattr(a, "root", None) else find_root()
    if not root:
        die("未找到 .project，先 init")
    p = proj(root)
    if getattr(a, "init", False):
        fp = p / CFG_NAME
        if fp.exists() and not getattr(a, "force", False):
            die(f"{fp} 已存在（要覆盖加 --force）")
        cfg = arch_template(root)
        atomic_write(fp, json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
        print(f"已生成 {fp}")
        print(f"  层 {len(cfg['layers'])} 个：{'、'.join(l['name'] for l in cfg['layers']) or '（没扫到源码目录，自己填）'}")
        print("  规则只有一条「全部允许」——不会报违规，收紧 rules 后重跑 audit。")
        return
    cfg = load_arch_cfg(p)
    cfgs = str(p / CFG_NAME)
    if getattr(a, "config", None):
        try:
            cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
            cfgs = a.config
        except Exception as e:
            die(f"--config 读取失败：{e}")
    src = getattr(a, "source", "auto") or "auto"
    exts = [e.strip() for e in a.ext.split(",") if e.strip()] if getattr(a, "ext", "") else None
    data, reg = None, None
    if src in ("auto", "imports"):
        data, _ = scan_project(root, exts)
        reg = Registry(root, lock=True)
        data = map_assets_to_files(reg, data)
        if src == "imports" and not data["files"]:
            print("  提示：没扫到任何代码文件，事实来源会是空的。")
    if reg is None:
        reg = Registry(root, lock=True)
    res = audit_engine(root, cfg, data)
    nf, nf_why = audit_no_facts(res)
    # 同一份报告里给出**另一种事实源**的读数，并折算到资产级 —— 这样 audit 与 viz
    # 报的是同一对数字，用户不必再去猜「为什么两个出口不一样」。
    vb = violations_both_sources(root, cfg, reg, data)
    name = project_name(p, root)
    out_md = Path(a.out) if getattr(a, "out", None) else p / "registry" / "architecture-audit.md"
    atomic_write(out_md, render_audit_md(name, res, cfg, cfgs, vb))
    payload = {"schema": AUDIT_SCHEMA, "project": name,
               "generated": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
               "config": cfgs if cfg else "", "source": res["source"],
               "stats": res["stats"], "findings": res["findings"],
               "no_facts": nf, "no_facts_reason": nf_why}
    out_json = Path(a.json_out) if getattr(a, "json_out", None) else p / "registry" / "architecture-audit.json"
    atomic_write(out_json, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(f"已生成 {out_md}")
    print(f"已生成 {out_json}")
    n = {}
    for f in res["findings"]:
        n[f["kind"]] = n.get(f["kind"], 0) + 1
    print(f"  层间违规 {n.get('layer', 0)} / 循环 {n.get('cycle', 0)} / 出度超限 {n.get('fanout', 0)}"
          f" / 未归类 {n.get('unassigned', 0)} / 未解析 {n.get('unresolved', 0)}")
    if nf:
        # 显式说出来：没有这条，知识型项目看到的是一行 rc=0 的绿。
        print(f"  ⚠ 架构闸门本次无事实可判（{nf_why}）——0 违规 ≠ 架构干净。")
        print("    知识型 / 文档型项目改用 `registry.py audit --source registry`："
              "按注册表声明的依赖判层间规则。")
    if not cfg:
        print(f"  未找到 {cfgs} → 只做了通用体检。跑 `registry.py audit --init` 生成规则骨架。")
    bad = n.get("layer", 0) + n.get("cycle", 0) + n.get("fanout", 0)
    if getattr(a, "strict", False) and bad:
        sys.exit(3)


def _delta_hash(obj):
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()[:16]


def _load_manifest(fp):
    try:
        m = json.loads(Path(fp).read_text(encoding="utf-8"))
    except Exception as e:
        die(f"{fp} 读取失败：{e}")
    if not isinstance(m, dict) or "assets" not in m:
        die(f"{fp} 不像 manifest.json（缺 assets）")
    return m


def _asset_map(m):
    out = {}
    for a in m.get("assets", []):
        if isinstance(a, dict) and a.get("id"):
            out[a["id"]] = a
    return out


def build_delta(base, head, base_name, head_name):
    """两份 manifest 的结构差异 + 机器收据（同样输入必得同样收据）。"""
    A, B = _asset_map(base), _asset_map(head)
    added = [B[i] for i in sorted(B) if i not in A]
    removed = [A[i] for i in sorted(A) if i not in B]
    moved, changed = [], []
    fields = [("name", "语义名"), ("status", "状态"), ("deps", "依赖"),
              ("alias", "别名"), ("path", "位置"), ("decisions", "决策")]
    for i in sorted(set(A) & set(B)):
        a, b = A[i], B[i]
        diffs = []
        for k, label in fields:
            va, vb = a.get(k), b.get(k)
            if isinstance(va, list) or isinstance(vb, list):
                va, vb = list(va or []), list(vb or [])
            if va != vb:
                diffs.append({"field": k, "label": label, "from": va, "to": vb})
        if a.get("path") != b.get("path"):
            moved.append({"id": i, "name": b.get("name", ""),
                          "from": a.get("path", ""), "to": b.get("path", "")})
        if diffs:
            changed.append({"id": i, "name": b.get("name", ""), "diffs": diffs})

    def dec_map(m):
        return {d["id"]: d for d in m.get("decisions", []) if isinstance(d, dict) and d.get("id")}
    DA, DB = dec_map(base), dec_map(head)
    dec_added = [DB[i] for i in sorted(DB) if i not in DA]
    dec_removed = [DA[i] for i in sorted(DA) if i not in DB]
    dec_changed = [{"id": i, "from": DA[i].get("status"), "to": DB[i].get("status")}
                   for i in sorted(set(DA) & set(DB))
                   if DA[i].get("status") != DB[i].get("status")]

    counts = {"added": len(added), "removed": len(removed), "changed": len(changed),
              "moved": len(moved), "decisions_added": len(dec_added),
              "decisions_removed": len(dec_removed), "decisions_changed": len(dec_changed),
              "assets_base": len(A), "assets_head": len(B)}
    core = {"base": {"file": base_name, "assets": len(A), "sha256": _delta_hash(A)},
            "head": {"file": head_name, "assets": len(B), "sha256": _delta_hash(B)},
            "counts": counts}
    # 收据只吃「与文件名无关的不变量」：两端的资产指纹 + 计数。
    # 曾经连 manifest 的文件名一起哈希（receipt=_delta_hash(core)，core 里带 file），
    # 而文档自己推荐的基线名就是 `baseline-<日期>.json` —— 下周导出换个名字，
    # 架构一个字没动，收据也一定变，「过一周再跑、收据没变=架构没动」这条用法失效。
    # 黑盒审计的判别实验：内容逐字节相同、只改文件名 →
    #   收据 ad427223ef9434f5 vs 22ce4874acfd2e1a（不同）。修掉。
    inv = {"base": {"assets": core["base"]["assets"], "sha256": core["base"]["sha256"]},
           "head": {"assets": core["head"]["assets"], "sha256": core["head"]["sha256"]},
           "counts": counts}
    return {"schema": DELTA_SCHEMA, "project": head.get("project") or base.get("project") or "",
            "generated": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "added": added, "removed": removed, "changed": changed, "moved": moved,
            "decisions": {"added": dec_added, "removed": dec_removed, "changed": dec_changed},
            "receipt": dict(core, receipt=_delta_hash(inv))}


def render_delta_md(d):
    c = d["receipt"]["counts"]
    L = [f"# 架构演进 · {d['project']} · {today()}", "",
         "> 由 `registry.py diff` 生成，勿手改。两处 `export` 之间「架构变了什么」。", "",
         "## 收据", "",
         f"- base `{d['receipt']['base']['file']}`　资产 {c['assets_base']}　指纹 `{d['receipt']['base']['sha256']}`",
         f"- head `{d['receipt']['head']['file']}`　资产 {c['assets_head']}　指纹 `{d['receipt']['head']['sha256']}`",
         f"- delta 收据 `{d['receipt']['receipt']}`（同输入必得同收据，可当交付凭证；"
         f"只由两端资产指纹与计数决定，**与 manifest 文件名无关**）", "",
         f"新增 {c['added']} / 移除 {c['removed']} / 变更 {c['changed']} / 移动 {c['moved']}", ""]
    L += ["## 新增资产", ""]
    L += [f"- {a['id']}　{a.get('name', '')}　〔{a.get('status', '')}〕`{a.get('path', '')}`"
          for a in d["added"]] or ["- 无"]
    L += ["", "## 移除资产", ""]
    L += [f"- {a['id']}　{a.get('name', '')}" for a in d["removed"]] or ["- 无"]
    L += ["", "## 变更资产", ""]
    if d["changed"]:
        for x in d["changed"]:
            L.append(f"- {x['id']}　{x.get('name', '')}")
            for f in x["diffs"]:
                L.append(f"    - {f['label']}：`{f['from']}` → `{f['to']}`")
    else:
        L.append("- 无")
    L += ["", "## 位置移动", ""]
    L += [f"- {x['id']}　`{x['from']}` → `{x['to']}`" for x in d["moved"]] or ["- 无"]
    L += ["", "## 决策变化", ""]
    dd = d["decisions"]
    L += [f"- 新增 {x['id']}　{x.get('title', '')}" for x in dd["added"]] or ["- 新增：无"]
    L += [f"- 移除 {x['id']}　{x.get('title', '')}" for x in dd["removed"]] or ["- 移除：无"]
    L += [f"- 状态变化 {x['id']}：{x['from']} → {x['to']}" for x in dd["changed"]] or ["- 状态变化：无"]
    L += ["", "## 怎么用", "",
          "- 配同期 ADR 一起交付，就是一份**架构演进说明**（这是注册表最容易变现的输出）。",
          "- 收据哈希不含时间戳，只由资产集合与计数决定 → 可写进 PR / 评审记录做「这一刻的架构」。", ""]
    return "\n".join(L)


def cmd_diff(a):
    """两份 manifest 对比 → added / removed / changed / moved + 机器收据。"""
    base = _load_manifest(a.base)
    head = _load_manifest(a.head)
    d = build_delta(base, head, Path(a.base).name, Path(a.head).name)
    root = Path(a.root) if getattr(a, "root", None) else find_root()
    p = proj(root) if root else None
    if getattr(a, "out", None):
        out_md = Path(a.out)
    elif p:
        out_md = p / "registry" / "architecture-delta.md"
    else:
        out_md = Path("architecture-delta.md")
    out_json = Path(a.json_out) if getattr(a, "json_out", None) else None
    if out_json is None and p:
        out_json = p / "registry" / "architecture-delta.json"
    atomic_write(out_md, render_delta_md(d))
    print(f"已生成 {out_md}")
    if out_json:
        atomic_write(out_json, json.dumps(d, ensure_ascii=False, indent=2) + "\n")
        print(f"已生成 {out_json}")
    c = d["receipt"]["counts"]
    print(f"  新增 {c['added']} / 移除 {c['removed']} / 变更 {c['changed']} / 移动 {c['moved']}"
          f"　收据 {d['receipt']['receipt']}")


def cmd_quick(a):
    """一行起项目并登记第一条资产——给「我就想快速记一下」的小需求。

    自动：若 --root 还没有 .project/，先用 L 档（轻量）建项目骨架；再直接登记
    一条资产。等价于 `init --scale L` + `add` 的合体，但只打一行命令。
    项目类型默认 custom（任何资产前缀都合法），用 --ptype 覆盖。
    """
    root = Path(a.root).resolve() if a.root else Path.cwd().resolve()
    fresh = not proj(root).exists()
    if fresh:
        cmd_init(argparse.Namespace(name=a.project or a.name, type=a.ptype,
                                    scale=a.scale, dims="", root=str(root),
                                    force=False, reset=False))
    cmd_add(argparse.Namespace(type=a.type, name=a.name, alias=a.alias,
                               path=a.path, deps=a.deps, decision="",
                               dims="", status=a.status,
                               allow_dangling_deps=False, root=str(root),
                               force=False))
    if fresh:
        print("💡 项目已用 L 档自动初始化（轻量）。要升档：改 PROJECT.md 的「规模」并 init --force。")


def pack_violations(nodes, owner, decided_findings, imported_findings):
    """把两种事实源的 `layer` findings 折算成统一的「资产级边」，按 (from,to) 去重合并。

    这是 audit 报告与 viz 页面**共用的唯一折算入口**：两个出口若各折各的，就会
    再一次各报一个数（这正是被审计出来的那条：viz 报 2、`audit --source registry` 报 3）。

    - `decided` 的 from/to 已经是资产 ID（注册表声明的依赖）
    - `imported` 的 from/to 是文件路径，按 owner 映射到资产
    - 折算不上的（未登记文件）单独计数，不静默丢
    - `imported_findings is None` 表示本次没取导入源（没 scan），此时 `imports` 为 None，
      与「取了但是 0 条」区分开——「没取」和「没有」是两件事。
    """
    def _pack(triples):
        seen = {}
        for a1, a2, why, ev in triples:
            if not a1 or not a2 or a1 not in nodes or a2 not in nodes:
                continue
            x = seen.setdefault((a1, a2), {"from": a1, "to": a2, "why": why, "evidence": []})
            if ev and ev not in x["evidence"]:
                x["evidence"].append(ev)
        return [dict(seen[k], count=len(seen[k]["evidence"])) for k in sorted(seen)]

    dec = [(v["from"], v["to"], v["why"], v.get("evidence", ""))
           for v in (decided_findings or []) if v.get("kind") == "layer"]
    out = {"declared": _pack(dec), "imports": None, "unmapped": 0, "files": len(dec)}
    if imported_findings is not None:
        raw = [(owner.get(v["from"]), owner.get(v["to"]), v["why"], v.get("evidence", ""))
               for v in imported_findings if v.get("kind") == "layer"]
        out["imports"] = _pack(raw)
        out["unmapped"] = sum(1 for a1, a2, _, _ in raw if not a1 or not a2)
        out["files"] = len(raw)
    return out


def violations_both_sources(root, cfg, reg, data):
    """两种事实源的层间违规，折算到资产级。`audit` 与 `viz` 都调它。

    `data` 为 None 时只有声明源（没 scan 就没有文件级事实）。
    `cfg` 为 None（项目没配 .project/architecture.json）时返回 None。
    """
    if not cfg:
        return None
    nodes = {d.get("ID", "") for _, _, d in reg.rows if d.get("ID")}
    owner = {}
    for a in (data or {}).get("assets", []):
        for f in a["files"]:
            owner[f] = a["id"]
    res_d = audit_engine(root, cfg, None, reg)
    res_i = audit_engine(root, cfg, data) if data else None
    out = pack_violations(nodes, owner, res_d["findings"],
                          res_i["findings"] if res_i else None)
    out["other"] = 0
    if res_i:
        out["other"] = len(res_i["findings"]) - sum(1 for f in res_i["findings"]
                                                   if f.get("kind") == "layer")
    return out


def arch_violations(root):
    """（viz 用）两种事实源的层间违规，折算到资产级边。None = 项目没配规则文件。

    为什么必须两个源都报：viz 画的是**注册表资产**，而 UI 上那根红边到底代表
    「声明里违规」还是「代码里违规」，是两件事。只报一个数时，用户拿它去比
    `audit --source registry` 必然对不上，还会以为是「同一事实算了两遍打架」。
    """
    p = proj(root)
    cfg = load_arch_cfg(p)
    if not cfg:
        return None
    reg = Registry(root, lock=True)
    data, _ = scan_project(root)
    data = map_assets_to_files(reg, data)
    return violations_both_sources(root, cfg, reg, data)

def main():
    force_utf8_io()
    ap = argparse.ArgumentParser(prog="registry.py", description="project-registry 落盘工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init")
    p.add_argument("--name", required=True)
    p.add_argument("--type", default="code-ui")
    p.add_argument("--scale", default="M", choices=["L", "M", "F"])
    p.add_argument("--dims", default="", help="自定义维度，如 UI:界面元素,MOD:功能模块")
    p.add_argument("--root")
    p.add_argument("--force", action="store_true", help="重建骨架（先备份，不动 memory/，资产行迁回）")
    p.add_argument("--reset", action="store_true", help="连同 memory/ 一起清空（必须配 --force）")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("add")
    p.add_argument("--type", required=True, help="资产前缀，如 UI / MOD / CH")
    p.add_argument("--name", required=True, help="语义名")
    p.add_argument("--alias", default="", help="别名，斜杠分隔")
    p.add_argument("--path", default="")
    p.add_argument("--deps", default="",
                   help="依赖资产 ID，逗号分隔。必须已登记：未登记的会被写入时闸门拒绝")
    p.add_argument("--decision", default="", help="关联决策 ADR-00N")
    p.add_argument("--dims", default="",
                   help="就地定义新前缀，如 \"ZZ:自定义件\"。同时写进 naming.md §四")
    p.add_argument("--status", default="在建",
                   help="五态之一。开工前先登记用 构思（位置可不写，落盘后再 set）")
    p.add_argument("--allow-dangling-deps", dest="allow_dangling_deps", action="store_true",
                   help="放行指向未登记 ID 的依赖（批量登记的前向引用 / 造悬空边的夹具）")
    p.add_argument("--root")
    p.add_argument("--force", action="store_true",
                   help="例外出口：放行未定义前缀 / 查重命中 / 禁用词（会回显放行了什么）")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("set", help="改资产字段（状态机 / 别名 / 位置 / 依赖）")
    p.add_argument("id")
    p.add_argument("--status", help="构思 / 在建 / 可用 / 冻结 / 废弃")
    p.add_argument("--name")
    p.add_argument("--alias")
    p.add_argument("--path")
    p.add_argument("--deps", help="依赖资产 ID，逗号分隔。必须已登记（见 add --deps）")
    p.add_argument("--decision")
    p.add_argument("--allow-dangling-deps", dest="allow_dangling_deps", action="store_true",
                   help="放行指向未登记 ID 的依赖（造悬空边的夹具 / 前向引用）")
    p.add_argument("--root")
    p.set_defaults(fn=cmd_set)

    p = sub.add_parser("retire", help="废弃资产（ID 保留不回收，原因写进详卡）")
    p.add_argument("id")
    p.add_argument("--reason", required=True)
    p.add_argument("--root")
    p.set_defaults(fn=cmd_retire)

    p = sub.add_parser("find")
    p.add_argument("query")
    p.add_argument("--type")
    p.add_argument("--root")
    p.set_defaults(fn=cmd_find)

    p = sub.add_parser("list")
    p.add_argument("--status")
    p.add_argument("--type")
    p.add_argument("--root")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("check", help="一致性闸门：ID / 状态 / 位置 / 决策引用 / 开环 / 依赖状态机")
    p.add_argument("--idea-days", dest="idea_days", type=int, default=None,
                   help="构思超期阈值（天），覆盖 PROJECT.md 与内置默认；0 = 关闭超期升级")
    p.add_argument("--strict-deps", dest="strict_deps", action="store_true",
                   help="悬空依赖从 WARN 升 ERROR（rc=1）——清存量 / 挂 CI 时用，默认关")
    p.add_argument("--root")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("context")
    p.add_argument("--root")
    p.set_defaults(fn=cmd_context)

    p = sub.add_parser("loop", help="开环管理：--list 看，--done <序号/关键词> 闭环")
    p.add_argument("--list", action="store_true")
    p.add_argument("--done", default=None)
    p.add_argument("--root")
    p.set_defaults(fn=cmd_loop)

    p = sub.add_parser("export", help="导出机器可读 manifest.json（Serena/Archify 入口）")
    p.add_argument("--out")
    p.add_argument("--root")
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("reconcile", help="对账：代码 as-is vs 注册表（只提议不裁决）")
    p.add_argument("--ext", default="", help="扫描后缀，逗号分隔，默认主流代码后缀")
    p.add_argument("--write", action="store_true", help="把未登记项提议进 open-loops")
    p.add_argument("--root")
    p.set_defaults(fn=cmd_reconcile)

    p = sub.add_parser("graph", help="生成依赖关系图 + 结构体检（F 档核心增量）")
    p.add_argument("--root")
    p.set_defaults(fn=cmd_graph)

    p = sub.add_parser("viz", help="生成自包含的架构图 HTML（注册表 → 分层依赖图，给人看）")
    p.add_argument("--out")
    p.add_argument("--root")
    p.set_defaults(fn=cmd_viz)

    p = sub.add_parser("idseq", help="查看 / 修正发号游标（--set <前缀> <号>）")
    # nargs="+"：文档与 -h 都写作 `--set UI 3`（两个 token），只吃一个会「复制即失败」
    p.add_argument("--set", nargs="+", default=None,
                   help="如 --set UI 3：把 UI 的游标设为 3（也接受 UI:3 / UI=3）")
    p.add_argument("--root")
    p.set_defaults(fn=cmd_idseq)

    p = sub.add_parser("close")
    p.add_argument("--summary", required=True)
    p.add_argument("--next", dest="next", default="")
    p.add_argument("--opens", default="")
    p.add_argument("--title", default="")
    p.add_argument("--no-doctor", dest="no_doctor", action="store_true",
                   help="跳过收工体检（默认跑：构思未落盘 / 构思但其实已落盘 / 位置不存在 / 未登记文件）")
    p.add_argument("--no-tip", dest="no_tip", action="store_true",
                   help="跳过收工后的「开新对话」交接提示（默认打印：提醒 + 可粘贴开场白）")
    p.add_argument("--root")
    p.set_defaults(fn=cmd_close)

    p = sub.add_parser("scan", help="导入级代码理解：解析 import → 文件级依赖图 + 行数统计")
    p.add_argument("--ext", default="", help="扫描后缀，逗号分隔，默认主流代码后缀")
    p.add_argument("--out")
    p.add_argument("--json", dest="json_out", default=None, help="同时输出机器可读 imports.json")
    p.add_argument("--root")
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("audit", help="架构校验：层间依赖规则 + 循环 + 出度 + 未归类")
    p.add_argument("--init", action="store_true", help="生成 .project/architecture.json 规则骨架")
    p.add_argument("--force", action="store_true", help="配合 --init 覆盖已有规则文件")
    p.add_argument("--config", default=None, help="用外部规则文件，不读 .project/architecture.json")
    p.add_argument("--source", default="auto", choices=["auto", "imports", "registry"],
                   help="事实来源：imports=扫描导入图（默认），registry=注册表声明的依赖")
    p.add_argument("--ext", default="")
    p.add_argument("--strict", action="store_true", help="有违规时退出码 3（可接 CI）")
    p.add_argument("--out")
    p.add_argument("--json", dest="json_out", default=None)
    p.add_argument("--root")
    p.set_defaults(fn=cmd_audit)

    p = sub.add_parser("diff", help="两份 manifest 对比 → 架构演进（新增/移除/变更/移动）+ 收据")
    p.add_argument("base")
    p.add_argument("head")
    p.add_argument("--out")
    p.add_argument("--json", dest="json_out", default=None)
    p.add_argument("--root")
    p.set_defaults(fn=cmd_diff)

    p = sub.add_parser("quick", help="一行起项目并登记第一条资产（轻量上手，自动 init）")
    p.add_argument("name", help="第一条资产的语义名")
    p.add_argument("--type", required=True, help="资产前缀，如 UI / MOD / CH")
    p.add_argument("--project", default="", help="项目名（缺省用资产名）")
    p.add_argument("--ptype", default="custom",
                   help="项目类型，默认 custom（任何资产前缀都合法）")
    p.add_argument("--scale", default="L", choices=["L", "M", "F"],
                   help="自动初始化时的规模档，默认 L 轻量")
    p.add_argument("--status", default="构思",
                   help="资产状态，默认 构思（随手记一笔，落盘后再 set）")
    p.add_argument("--alias", default="")
    p.add_argument("--path", default="")
    p.add_argument("--deps", default="")
    p.add_argument("--root")
    p.set_defaults(fn=cmd_quick)

    a = ap.parse_args()
    # quick 也必须放行：它的存在意义就是「当前目录还没有 .project/」时一行起项目，
    # cmd_quick 内部会先跑 cmd_init。曾漏在这一串里 → 文档承诺的一行入口在没有
    # .project/ 的目录里直接 exit 1，只有补 --root 才能走通（而 --root 恰恰是它
    # 想省掉的东西）。e2e 用例因为 `_run` 一律补 --root，正好绕过这道守卫 —
    # 测试全绿却测不到。裸调用路径由 test_quick_without_root_* 守着。
    if a.cmd not in ("init", "quick", "diff") and not a.root and not find_root():
        die("当前目录及父级没有 .project/，先跑 init 或用 --root 指定项目根。")
    a.fn(a)


if __name__ == "__main__":
    main()
