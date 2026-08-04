"""知乎开放平台 API 封装 — Pulse 数据层"""
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import requests

# ── 配置加载 ────────────────────────────────────────────

def _load_secret() -> str:
    """从项目根目录 .env 加载 ZHIHU_ACCESS_SECRET"""
    env_path = Path(__file__).parent.parent / ".env"
    if not env_path.exists():
        raise FileNotFoundError(
            "未找到 .env 文件，请从 .env.example 复制并填入 ZHIHU_ACCESS_SECRET"
        )
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip() == "ZHIHU_ACCESS_SECRET":
                    secret = v.strip()
                    if secret and secret != "your_access_secret_here":
                        return secret
    raise ValueError("请在 .env 中填入真实的 ZHIHU_ACCESS_SECRET")


def _headers(secret: str | None = None) -> dict:
    return {
        "Authorization": f"Bearer {secret or _load_secret()}",
        "X-Request-Timestamp": str(int(time.time())),
        "Content-Type": "application/json",
    }


BASE = "https://developer.zhihu.com/api/v1"


# ── 数据模型 ────────────────────────────────────────────

@dataclass
class ArticleItem:
    """搜索/内容列表返回的单条内容"""
    title: str
    url: str
    content_type: str          # Article | Answer | Zvideo | Pin | Question
    content_text: str          # 摘要 (300-800 字)
    vote_count: int
    comment_count: int
    favorite_count: int = 0
    author_name: str = ""
    author_avatar: str = ""
    author_badge: str = ""
    ranking_score: float = 0.0
    created_at: int | None = None   # Unix 时间戳
    updated_at: int | None = None


@dataclass
class FolloweeItem:
    """关注的用户"""
    fullname: str
    url_token: str
    url: str
    avatar_url: str
    headline: str
    gender: int                 # 0 女/未知, 1 男
    follower_count: int


@dataclass
class Paging:
    """分页信息"""
    is_end: bool
    next_offset: str = ""
    totals: int = 0


@dataclass
class SearchResult:
    """搜索结果"""
    items: list[ArticleItem]
    has_more: bool
    search_hash_id: str


@dataclass
class UserContentsResult:
    """用户内容列表"""
    items: list[ArticleItem]
    paging: Paging


@dataclass
class UserFolloweesResult:
    """用户关注列表"""
    items: list[FolloweeItem]
    paging: Paging


# ── API 调用 ────────────────────────────────────────────

class ZhihuAPIError(Exception):
    """知乎 API 返回的错误"""
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"API Error {code}: {message}")


class QuotaExceeded(ZhihuAPIError):
    """配额/频率限制"""


class AuthError(ZhihuAPIError):
    """鉴权失败"""


def _request(method: str, path: str, params: dict | None = None) -> dict:
    """统一请求封装，处理错误码"""
    url = f"{BASE}{path}"
    resp = requests.request(
        method, url,
        headers=_headers(),
        params=params,
        timeout=15,
    )
    if not resp.ok:
        raise ZhihuAPIError(
            resp.status_code,
            f"HTTP {resp.status_code}: {resp.text[:200]}",
        )
    try:
        data = resp.json()
    except ValueError:
        raise ZhihuAPIError(-1, f"非 JSON 响应: {resp.text[:200]}")

    code = data.get("Code", -1)
    if code == 0:
        return data.get("Data", {})

    msg = data.get("Message", "Unknown error")
    if code == 20001:
        raise AuthError(code, msg)
    if code in (30001, 30002):
        raise QuotaExceeded(code, msg)
    raise ZhihuAPIError(code, msg)


def _parse_article(raw: dict) -> ArticleItem:
    """将 API 返回的 Item 转为 ArticleItem"""
    return ArticleItem(
        title=raw.get("Title") or "",
        url=raw.get("Url") or "",
        content_type=raw.get("ContentType") or "",
        content_text=raw.get("ContentText") or raw.get("Summary") or "",
        vote_count=raw.get("VoteUpCount", raw.get("LikeCount", 0)) or 0,
        comment_count=raw.get("CommentCount") or 0,
        favorite_count=raw.get("FavoriteCount") or 0,
        author_name=raw.get("AuthorName") or "",
        author_avatar=raw.get("AuthorAvatar") or "",
        author_badge=raw.get("AuthorBadgeText") or "",
        ranking_score=float(raw.get("RankingScore") or 0),
        created_at=raw.get("CreatedAt"),
        updated_at=raw.get("EditTime"),
    )


# ── 公开接口 ────────────────────────────────────────────

def search(query: str, count: int = 10) -> SearchResult:
    """知乎站内搜索

    Args:
        query: 搜索关键词（不能为空）
        count: 返回数量，最大 10

    Raises:
        QuotaExceeded: 频率限制或配额用尽
        AuthError: Access Secret 无效
        ZhihuAPIError: 其他 API 错误
    """
    if not query.strip():
        raise ValueError("query 不能为空")
    count = max(1, min(count, 10))

    data = _request("GET", "/content/zhihu_search", {
        "Query": query,
        "Count": count,
    })
    items = [_parse_article(i) for i in data.get("Items", [])]
    return SearchResult(
        items=items,
        has_more=data.get("HasMore", False),
        search_hash_id=data.get("SearchHashId", ""),
    )


def get_my_contents(
    content_type: str = "all",
    limit: int = 20,
    sort_field: str = "ts",
    sort_order: str = "desc",
    offset: int = 0,
) -> UserContentsResult:
    """获取当前调用方本人的创作内容

    Args:
        content_type: all | answer | article | zvideo | pin | question
        limit: 返回数量，最大 50
        sort_field: ts（时间）| like_count（点赞数）
        sort_order: asc | desc
        offset: 分页偏移量，首次传 0，后续传上次返回的 next_offset

    Raises:
        QuotaExceeded: 频率限制或配额用尽
        AuthError: Access Secret 无效
    """
    valid_types = ("all", "answer", "article", "zvideo", "pin", "question")
    if content_type not in valid_types:
        raise ValueError(f"content_type 必须为 {valid_types} 之一")

    data = _request("GET", "/user/contents", {
        "ContentType": content_type,
        "Limit": max(1, min(limit, 50)),
        "SortField": sort_field,
        "SortOrder": sort_order,
        "Offset": offset,
    })
    items = [_parse_article(i) for i in data.get("Items", [])]
    paging = data.get("Paging", {})
    return UserContentsResult(
        items=items,
        paging=Paging(
            is_end=paging.get("IsEnd", True),
            next_offset=paging.get("NextOffset", ""),
            totals=paging.get("Totals", 0),
        ),
    )


def get_all_my_contents(
    content_type: str = "all",
    sort_field: str = "ts",
) -> list[ArticleItem]:
    """获取本人全部创作内容（自动翻页，直到 IsEnd=True）

    注意：会消耗多次 API 调用。
    """
    all_items = []
    offset = 0
    while True:
        result = get_my_contents(
            content_type=content_type,
            limit=50,
            sort_field=sort_field,
            offset=offset,
        )
        all_items.extend(result.items)
        if result.paging.is_end:
            break
        offset = int(result.paging.next_offset) if result.paging.next_offset else 0
    return all_items


def get_my_followees(limit: int = 20, offset: int = 0) -> UserFolloweesResult:
    """获取当前调用方本人的关注列表

    Args:
        limit: 返回数量，最大 50
        offset: 分页偏移量

    Raises:
        QuotaExceeded: 频率限制或配额用尽
        AuthError: Access Secret 无效
    """
    data = _request("GET", "/user/followees", {
        "Limit": max(1, min(limit, 50)),
        "Offset": offset,
    })
    items = []
    for raw in data.get("Items", []):
        items.append(FolloweeItem(
            fullname=raw.get("Fullname", ""),
            url_token=raw.get("UrlToken", ""),
            url=raw.get("Url", ""),
            avatar_url=raw.get("AvatarUrl", ""),
            headline=raw.get("Headline", ""),
            gender=raw.get("Gender", 0),
            follower_count=raw.get("FollowerCount", 0),
        ))
    paging = data.get("Paging", {})
    return UserFolloweesResult(
        items=items,
        paging=Paging(
            is_end=paging.get("IsEnd", True),
            next_offset=paging.get("NextOffset", ""),
            totals=paging.get("Totals", 0),
        ),
    )


def get_my_followee_count() -> int:
    """获取本人关注列表的关注总数

    可用于评估账号活跃度。注意：不是本人的粉丝数。
    """
    result = get_my_followees(limit=1)
    return result.paging.totals


# ── 辅助工具 ────────────────────────────────────────────

def extract_article_id(url: str) -> str | None:
    """从知乎 URL 中提取文章/回答 ID

    >>> extract_article_id('https://zhuanlan.zhihu.com/p/1992754233318077903')
    '1992754233318077903'
    >>> extract_article_id('https://www.zhihu.com/answer/123456789')
    '123456789'
    """
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.rstrip("/")
    if not path or path == "/":
        return None
    parts = path.rsplit("/", 1)
    return parts[-1] if parts[-1] else None


def find_article_by_url(
    query: str, article_url: str, count: int = 10
) -> ArticleItem | None:
    """在搜索结果中匹配指定 URL 的文章

    因为 API 没有「按 URL 查文章」的端点，只能搜索后匹配。
    """
    result = search(query, count=count)
    article_id = extract_article_id(article_url)
    if not article_id:
        return None
    for item in result.items:
        if article_id in item.url:
            return item
    return None


def topic_benchmark(query: str, count: int = 10) -> dict:
    """搜索一个话题，返回该话题下的关键指标基准

    用于竞品对比——知道一个话题下 Top 内容的平均分/赞同/评论，
    才能判断你自己的内容在这个话题里处于什么水平。

    Returns:
        {
            "query": "...",
            "count": 10,
            "avg_ranking_score": 1.92,
            "max_ranking_score": 2.24,
            "avg_votes": 34,
            "total_articles": 10,
            "top3_urls": [...],
        }
    """
    result = search(query, count=count)
    if not result.items:
        return {"query": query, "count": 0, "total_articles": 0}

    scores = [i.ranking_score for i in result.items]
    votes = [i.vote_count for i in result.items]
    return {
        "query": query,
        "count": len(result.items),
        "avg_ranking_score": round(sum(scores) / len(scores), 3),
        "max_ranking_score": round(max(scores), 3),
        "min_ranking_score": round(min(scores), 3),
        "avg_votes": round(sum(votes) / len(votes), 1),
        "total_votes": sum(votes),
        "top3_urls": [i.url for i in result.items[:3]],
    }
