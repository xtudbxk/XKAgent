# deps: stdlib only
"""
skills/openreview — OpenReview 论文 Review + Rebuttal 下载器

功能:
  - search_forum_by_title(title)：按标题搜索 forum ID
  - get_all_notes(forum_id)：下载全部 notes
  - classify_notes(notes)：分类为 review/rebuttal/meta_review
  - download_paper(title)：一站式下载并返回结构化结果

用法:
    from skills.openreview import openreview_downloader as od
    result = od.download_paper("GPSToken")
    print(result["reviews"][0]["rating"])
"""

from .openreview_downloader import (
    search_forum_by_title,
    get_all_notes,
    classify_notes,
    download_paper,
    set_http_request_callback,
)

__version__ = "1.0.0"
