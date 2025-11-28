import requests

# Crossref API endpoint
url = "https://api.crossref.org/works"

# 検索キーワード（例：PEDOT）
params = {
    "query": "PEDOT",
    "sort": "published",
    "order": "desc",
    "rows": 5
}

resp = requests.get(url, params=params)
data = resp.json()

print("\n=== 最新の論文 5件 ===\n")
for item in data["message"]["items"]:
    title = item.get("title", ["(no title)"])[0]
    published = item.get("published-print") or item.get("published-online")
    print("-", title)

