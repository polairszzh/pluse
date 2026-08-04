"""搜索知乎 API，收集 benchmark 测试用文章"""
import os, json, time, requests, urllib.parse

env_path = r'G:\pluse\.env'
secret = ''
with open(env_path, encoding='utf-8') as f:
    for line in f:
        if line.strip() and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            if k.strip() == 'ZHIHU_ACCESS_SECRET':
                secret = v.strip()

headers = {
    'Authorization': f'Bearer {secret}',
    'X-Request-Timestamp': str(int(time.time())),
    'Content-Type': 'application/json',
}

queries = [
    ('知乎内容创作', 'zhihu_content'),
    ('AI搜索优化', 'ai_search'),
    ('个人品牌运营', 'personal_brand'),
]

results = {}
for query, tag in queries:
    qs = urllib.parse.quote(query)
    r = requests.get(
        f'https://developer.zhihu.com/api/v1/content/zhihu_search?Query={qs}&Count=10',
        headers=headers,
    )
    data = r.json()
    if data.get('Code') != 0:
        print(f'  {tag}: API error {data.get("Code")}')
        continue
    items = sorted(data['Data']['Items'], key=lambda x: -x['RankingScore'])
    results[tag] = []
    for i in items:
        results[tag].append({
            'title': i['Title'][:100],
            'url': i['Url'],
            'score': round(i['RankingScore'], 3),
            'votes': i['VoteUpCount'],
            'comments': i['CommentCount'],
            'type': i['ContentType'],
        })

for tag, items in results.items():
    print(f'\n=== {tag} ===')
    for idx, item in enumerate(items):
        print(f'  [{idx+1}] Score:{item["score"]} 赞:{item["votes"]} 评:{item["comments"]}')
        print(f'       {item["title"]}')
        print(f'       {item["url"]}')

os.makedirs(r'G:\pluse\data', exist_ok=True)
with open(r'G:\pluse\data\benchmark_articles.json', 'w', encoding='utf-8') as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print('\nSaved to data/benchmark_articles.json')
