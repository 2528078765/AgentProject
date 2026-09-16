"""项目统一异常类型。

重要约定：**库代码一律抛普通异常，绝不抛 SystemExit。**

SystemExit 继承自 BaseException，而 `except Exception` 抓不住它。这在 Web 服务、
后台线程这类场景下会造成「静默死亡」——线程直接退出，前端只看到连接被断开，
看不到任何错误信息。SystemExit 只应该出现在 CLI 层的参数校验里。
"""

from __future__ import annotations


class AutoVidError(RuntimeError):
    """AutoVid 的可预期错误：配置、产物缺失、环境缺失、provider 失败等。"""


class ArtifactError(AutoVidError):
    """产物（Artifact）缺失或登记失败。"""
