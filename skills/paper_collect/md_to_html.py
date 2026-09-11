# deps: stdlib only (re / html / os)
"""论文清单 md → 带链接 HTML 转换器。

总览：解析论文清单 markdown（## 领域分组、### 论文条目、**字段**: 值），
生成带领域导航、可点击 arXiv 链接、双语摘要高亮的单文件 HTML。

Deps: stdlib only（re / html / os）
Usage: 见 skill.md API 速查（importlib 加载后调用 md_to_html）
"""
import re
import html as html_mod

__all__ = ['md_to_html']

_CSS = """
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;max-width:1100px;margin:0 auto;padding:20px;line-height:1.7;color:#222}
h1{border-bottom:3px solid #4a6cf7;padding-bottom:10px}
h2{background:#eef2ff;padding:8px 14px;border-left:5px solid #4a6cf7;margin-top:40px;border-radius:4px}
h3{margin-top:28px;color:#1a1a1a;border-bottom:1px dashed #ccc;padding-bottom:4px}
p{margin:6px 0}
b{color:#333}
a{color:#4a6cf7;text-decoration:none;word-break:break-all}
a:hover{text-decoration:underline}
.abs-en{color:#444;background:#f8f8f8;padding:8px 12px;border-radius:4px;border-left:3px solid #ddd}
.abs-zh{color:#333;background:#f0f7ff;padding:8px 12px;border-radius:4px;border-left:3px solid #4a6cf7}
.noabs{color:#999;font-style:italic}
.nav{background:#f5f5f5;padding:12px;border-radius:6px;font-size:13px;column-count:3;margin:16px 0}
.nav a{display:block;margin:2px 0}
.stats{background:#fff8e6;padding:10px 14px;border-radius:6px;margin:12px 0}
"""


def _linkify(text):
    """把文本中的裸 URL 转为可点击 <a> 链接。

    Args:
        text: 含 URL 的字符串。
    Returns:
        替换后的字符串，URL 包在 <a href target="_blank"> 中。
    Example:
        _linkify('see https://arxiv.org/abs/2603.07455') -> 'see <a href=...>...</a>'
    """
    return re.sub(r'(https?://[^\s<]+)', r'<a href="\1" target="_blank">\1</a>', text)


def md_to_html(md_path, title='论文清单'):
    """将论文清单 markdown 转为带链接的单文件 HTML。

    Args:
        md_path: 论文清单 .md 文件路径（## 领域分组 + ### 论文条目 + **字段**: 值）。
        title: HTML <title> 与页面主标题。
    Returns:
        HTML 字符串（含领域导航、可点击链接、双语摘要样式）。
    Example:
        html = md_to_html('docs/papers.md', '论文清单')
        open('/tmp/out.html', 'w', encoding='utf-8').write(html)
    """
    content = open(md_path, encoding='utf-8').read()
    lines = content.split('\n')
    doms = []
    for ln in lines:
        m = re.match(r'^## (.+?) \((\d+)篇\)', ln)
        if m:
            doms.append((m.group(1), int(m.group(2))))
    nav_items = ['<a href="#dom-%d">%s (%d)</a>' % (i, html_mod.escape(d), n)
                 for i, (d, n) in enumerate(doms)]
    nav_html = '<div class="nav">%s</div>' % '\n'.join(nav_items)
    out = ['<!DOCTYPE html>',
           '<html lang="zh-CN"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1">',
           '<title>%s</title>' % html_mod.escape(title),
           '<style>%s</style></head><body>' % _CSS]
    in_body = False
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if s.startswith('# '):
            out.append('<h1>%s</h1>' % html_mod.escape(s[2:].strip()))
            continue
        if s.startswith('生成时间') or s.startswith('总数:'):
            if not in_body:
                out.append('<div class="stats">%s</div>' % html_mod.escape(s))
            continue
        m = re.match(r'^## (.+?) \((\d+)篇\)', s)
        if m:
            idx = next(i for i, (d, _) in enumerate(doms) if d == m.group(1))
            out.append('<h2 id="dom-%d">%s（%s篇）</h2>' % (idx, html_mod.escape(m.group(1)), m.group(2)))
            continue
        m = re.match(r'^### \(([^)]*)\)(.*)$', s)
        if m:
            pid = m.group(1).strip()
            rest = m.group(2).strip()
            tag = ''
            m2 = re.match(r'^\[(strong|candidate|hot)\]\s*(.*)$', rest)
            if m2:
                color = '#e63946' if m2.group(1) == 'strong' else '#777'
                tag = ' <span style="color:%s;font-size:12px">[%s]</span>' % (color, m2.group(1))
                rest = m2.group(2)
            out.append('<h3 id="paper-%s">(%s) %s%s</h3>'
                       % (html_mod.escape(pid or 'x'), html_mod.escape(pid or '-'),
                          html_mod.escape(rest), tag))
            in_body = True
            continue
        m = re.match(r'^\*\*(.+?)\*\*:\s*(.*)$', s)
        if m:
            k, v = m.group(1), m.group(2)
            ve = _linkify(html_mod.escape(v))
            cls = {'完整摘要': ' class="abs-en"',
                   '完整摘要中译': ' class="abs-zh"',
                   '原文链接': ' style="background:#f0f0f0;padding:4px 8px;border-radius:4px;display:inline-block"'}.get(k, '')
            out.append('<p%s><b>%s</b>: %s</p>' % (cls, html_mod.escape(k), ve))
            continue
        if s.startswith('_（无摘要'):
            out.append('<p class="noabs">%s</p>' % html_mod.escape(s.strip('_')))
            continue
        if s.startswith('统计:'):
            out.append('<div class="stats">%s</div>' % html_mod.escape(s))
            continue
        out.append('<p>%s</p>' % html_mod.escape(s))
    h1_pos = next((i for i, l in enumerate(out) if l.startswith('<h1>')), None)
    if h1_pos is not None:
        out.insert(h1_pos + 1, nav_html)
    out.append('</body></html>')
    return '\n'.join(out)


if __name__ == '__main__':
    # 宿主侧 CLI 入口（沙箱内不依赖）
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else 'papers.md'
    dst = sys.argv[2] if len(sys.argv) > 2 else 'out.html'
    open(dst, 'w', encoding='utf-8').write(md_to_html(src))
