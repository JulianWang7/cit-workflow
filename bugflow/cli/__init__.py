"""orchestrator CLI package。

不在此 import main 函数，避免 bugflow.cli.main 被函数 shadow 导致 patch 失败。
入口点 bugfix-batch = "bugflow.cli.main:main" 由 pip 直接解析。
"""
