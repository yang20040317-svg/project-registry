"""project-registry 测试夹具。

registry.py 是单文件脚本，模块顶部只有常量 / 正则 / Path 计算（无任何副作用，
真正的入口在 `if __name__ == "__main__": main()`），因此可以安全 import 做纯函数单测；
端到端契约（游标 / viz 字节一致 / quick 引导）走 subprocess 调脚本本体。
"""

import subprocess
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
REGISTRY = SCRIPTS / "registry.py"
PY = sys.executable

# 让 test_pure.py 能 `import registry`
sys.path.insert(0, str(SCRIPTS))


@pytest.fixture
def run():
    """小工具：run(*args, root=tmp_project) -> subprocess.CompletedProcess"""

    def _run(args, root=None):
        full = [PY, str(REGISTRY), *args]
        if root is not None:
            full += ["--root", str(root)]
        return subprocess.run(full, capture_output=True, text=True)

    return _run


@pytest.fixture
def tmp_project(tmp_path, run):
    """最小可跑项目：custom 类型 / L 档（最轻量）。"""
    r = run(["init", "--name", "demo", "--type", "custom", "--scale", "L"], root=tmp_path)
    assert r.returncode == 0, r.stderr
    return tmp_path
