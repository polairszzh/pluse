"""pytest 全局配置：把 scripts/ 加入 sys.path，测试可直接 import 脚本模块"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
