"""arxiv-filter：Windows 本地 arXiv 文献收件箱。

本包在方案第 3 部分定义的 src layout 中；阶段 0 只落地工程骨架
（入口、路径解析、错误码），业务模块按 T1–T7 逐个加入。
"""

from __future__ import annotations

#: 应用版本（SemVer）。这是唯一权威来源：pyproject.toml 以 dynamic 方式读取本属性。
__version__ = "0.1.0"

#: 数据库 schema 版本（单调整数），与 :data:`SCHEMA_META_VERSION` 对应 001_init.sql。
SCHEMA_VERSION = 1

#: 官方 API 祝谢文案，README 与「关于」页必须原样展示（方案 §5.1）。
ARXIV_ACKNOWLEDGEMENT = (
    "Thank you to arXiv for use of its open access interoperability."
)

__all__ = ["__version__", "SCHEMA_VERSION", "ARXIV_ACKNOWLEDGEMENT"]
