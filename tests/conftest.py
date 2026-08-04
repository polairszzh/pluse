import sys
from pathlib import Path

# 让 tests/ 下所有测试能 import scripts/ 下的模块
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
