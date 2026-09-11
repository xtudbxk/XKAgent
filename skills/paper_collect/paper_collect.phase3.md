# Phase 3：抓取完整摘要（arXiv API）

## 目标
为有 arxiv_id 的论文批量抓取完整 abstract。

## 流程
1. 从 index.json 提取所有 arxiv_id（去重）
2. 分批 50 篇/次调 arXiv API（id_list 参数），间隔 ≥3s 防限流
3. 解析 Atom feed（<entry> 内 id/title/summary）
4. 断点续传：缓存 /tmp/arxiv_abstracts.json，只抓缺失
5. 归一化：API 返回 ID 带 vN 版本号，需去掉（否则与 index 不匹配）

## 代码

```python
import json, time, requests, re, os

def fetch_abstracts(ids, cache='/tmp/arxiv_abstracts.json', batch=50, sleep=3.0):
    """分批抓取 arXiv 完整摘要，断点续传。
    Args: ids: arxiv_id 列表；cache: 缓存 json 路径。
    Returns: {arxiv_id: {title, abstract, url}} dict。
    """
    done = {}
    if os.path.exists(cache):
        done = json.load(open(cache, encoding='utf-8'))
    todo = [i for i in ids if i not in done]
    for i in range(0, len(todo), batch):
        chunk = todo[i:i+batch]
        r = requests.get('http://export.arxiv.org/api/query',
                         params={'id_list': ','.join(chunk), 'max_results': batch},
                         timeout=40)
        if r.status_code == 200:
            for e in re.findall(r'<entry>(.*?)</entry>', r.text, re.S):
                m_id = re.search(r'<id>http://arxiv.org/abs/([^<]+)</id>', e)
                m_a = re.search(r'<summary>(.*?)</summary>', e, re.S)
                if m_id:
                    aid = re.sub(r'v\d+$', '', m_id.group(1).strip())
                    done[aid] = {
                        'title': re.sub(r'\s+', ' ', re.search(r'<title>(.*?)</title>', e, re.S).group(1)).strip(),
                        'abstract': re.sub(r'\s+', ' ', m_a.group(1)).strip(),
                        'url': 'https://arxiv.org/abs/%s' % aid}
        time.sleep(sleep)
        json.dump(done, open(cache, 'w', encoding='utf-8'), ensure_ascii=False)
    return done
```

## 输出
arxiv_abstracts.json（997 条：title/abstract/url）
