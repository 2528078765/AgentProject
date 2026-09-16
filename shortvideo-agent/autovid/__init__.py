"""AutoVid —— 短视频口播数字人自动化流水线（P0 骨架）。

设计目标（P0）：
  * 零第三方依赖：只用 Python 标准库 + 系统 FFmpeg + Windows SAPI
  * 完全离线可跑：不联网也能端到端产出成片
  * 每一步的产物落盘为带哈希的 Artifact，支持局部重跑
  * 所有生成类能力（TTS / 出图 / 数字人 / 发布）都是可插拔 Provider
"""

__version__ = "0.1.0"

# --------------------------------------------------------------------------- #
# 依赖引导
#
# 目标机器上 pip 写不了临时文件（`Permission denied ... *.whl.metadata`），
# 所以依赖是用 scripts/vendor_deps.py 手工下载 wheel 并解包到 .pylibs/ 的。
# 这里把该目录挂到 sys.path，效果等同于装进了环境。
#
# 为什么不动全局 site-packages：一是权限，二是这样整个项目自包含，
# 拷到别的机器上照样能跑（.pylibs 已在 .gitignore 里，用脚本重建即可）。
# --------------------------------------------------------------------------- #
import sys as _sys
from pathlib import Path as _Path

_LIBS = _Path(__file__).resolve().parent.parent / ".pylibs"
if _LIBS.is_dir() and str(_LIBS) not in _sys.path:
    _sys.path.insert(0, str(_LIBS))

__all__ = ["__version__"]
