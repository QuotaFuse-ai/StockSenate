"""Package version.

`pyproject.toml` `[project] version` 是唯一真源；这里从安装元数据读，不再手写
第二份（QUOT-401）。此前本文件与 pyproject 各写一份 1.0.1，bump 时漏改一处就会
让 `/healthz` 与 wheel 元数据对不上。
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

try:
    __version__ = _dist_version("stocksenate")
except PackageNotFoundError:  # 未安装：纯源码树直接 import
    __version__ = "0.0.0+source"

__all__ = ["__version__"]
