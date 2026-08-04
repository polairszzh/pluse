"""知乎 API 连通性测试"""
import sys
import time
from pathlib import Path

import requests

# 加载 .env
env_path = Path(__file__).parent.parent / ".env"
if not env_path.exists():
    print("❌ 未找到 .env 文件，请从 .env.example 复制并填入 Access Secret")
    sys.exit(1)

env_vars = {}
with open(env_path) as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env_vars[k.strip()] = v.strip()

ACCESS_SECRET = env_vars.get("ZHIHU_ACCESS_SECRET", "")
if not ACCESS_SECRET or ACCESS_SECRET == "your_access_secret_here":
    print("❌ 请在 .env 中填入真实的 ZHIHU_ACCESS_SECRET")
    sys.exit(1)

BASE = "https://developer.zhihu.com/api/v1"
HEADERS = {
    "Authorization": f"Bearer {ACCESS_SECRET}",
    "X-Request-Timestamp": str(int(time.time())),
    "Content-Type": "application/json",
}


def test_search():
    """测试知乎搜索 API"""
    print("=" * 50)
    print("1. 知乎搜索 API")
    resp = requests.get(
        f"{BASE}/content/zhihu_search",
        headers=HEADERS,
        params={"Query": "AI搜索优化", "Count": 3},
    )
    data = resp.json()
    code = data.get("Code")
    print(f"   状态码: {resp.status_code}  API Code: {code}")
    if code == 0:
        items = data.get("Data", {}).get("Items", [])
        print(f"   返回 {len(items)} 条结果:")
        for i, item in enumerate(items):
            print(f"   [{i+1}] {item.get('Title','?')} | 赞同:{item.get('VoteUpCount',0)} | {item.get('ContentType','?')}")
        print("   ✅ 搜索 API 通过")
    elif code == 20001:
        print("   ❌ 鉴权失败，检查 Access Secret")
    elif code == 30001:
        print("   ⚠️ 频率限制，稍后重试")
    else:
        print(f"   ❌ 错误: {data.get('Message','')}")


def test_user_contents():
    """测试用户内容 API"""
    print()
    print("2. 用户内容 API")
    resp = requests.get(
        f"{BASE}/user/contents",
        headers=HEADERS,
        params={"ContentType": "all", "Limit": 5},
    )
    data = resp.json()
    code = data.get("Code")
    print(f"   状态码: {resp.status_code}  API Code: {code}")
    if code == 0:
        items = data.get("Data", {}).get("Items", [])
        totals = data.get("Data", {}).get("Paging", {}).get("Totals", 0)
        print(f"   总计 {totals} 条内容，返回前 {len(items)} 条:")
        for i, item in enumerate(items):
            print(f"   [{i+1}] {item.get('Title','?')[:50]} | 赞:{item.get('LikeCount',0)} 藏:{item.get('FavoriteCount',0)}")
        print("   ✅ 用户内容 API 通过")
    elif code == 20001:
        print("   ❌ 鉴权失败")
    else:
        print(f"   ❌ 错误: {data.get('Message','')}")


def test_user_followees():
    """测试用户关注 API"""
    print()
    print("3. 用户关注 API")
    resp = requests.get(
        f"{BASE}/user/followees",
        headers=HEADERS,
        params={"Limit": 5},
    )
    data = resp.json()
    code = data.get("Code")
    print(f"   状态码: {resp.status_code}  API Code: {code}")
    if code == 0:
        items = data.get("Data", {}).get("Items", [])
        print(f"   返回关注 {len(items)} 人:")
        for i, item in enumerate(items):
            print(f"   [{i+1}] {item.get('Fullname','?')} | 粉丝:{item.get('FollowerCount',0)} | {item.get('Headline','?')[:30]}")
        print("   ✅ 用户关注 API 通过")
    elif code == 20001:
        print("   ❌ 鉴权失败")
    else:
        print(f"   ❌ 错误: {data.get('Message','')}")


if __name__ == "__main__":
    print("Pulse — 知乎 API 连通性测试")
    print(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    test_search()
    test_user_contents()
    test_user_followees()
    print()
    print("=" * 50)
    print("测试完成")
