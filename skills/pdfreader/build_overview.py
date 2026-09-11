# deps: stdlib only
"""pdfreader: 汇总产物 —— __info__.json / content.md(锚点) / pages/ / tables/ / __overview__.md / __index__.md"""
import json, os, re
from collections import Counter

STOPWORDS = set("""a an the and or of to in for on with is are as by at from this that it its be was were has have not
但 与 和 或 的 是 在 于 为 上 下 中 你 我 他 它 一 个 人 也 就 都 而 及 并 被 把 对 从 到 之""".split())

def _anchor(page_no, line_no):
    return "[p%03d-L%04d]" % (page_no, line_no)

def run(out_dir: str) -> str:
    with open(os.path.join(out_dir, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    with open(os.path.join(out_dir, "content.json"), encoding="utf-8") as f:
        content = json.load(f)
    with open(os.path.join(out_dir, "tables.json"), encoding="utf-8") as f:
        tables = json.load(f).get("tables", [])
    img_meta = []
    if os.path.exists(os.path.join(out_dir, "images_meta.json")):
        with open(os.path.join(out_dir, "images_meta.json"), encoding="utf-8") as f:
            img_meta = json.load(f).get("images", [])

    # ---- content.md（锚点全文）----
    cm = ["# %s — 全文（带行锚点）" % meta["filename"], ""]
    for p in content["pages"]:
        cm.append("\n==== Page %s ====" % p["page"])
        for ln in p["lines"]:
            cm.append("%s %s" % (_anchor(p["page"], ln["no"]), ln["text"]))
    # ---- content.md（锚点全文）----
    with open(os.path.join(out_dir, "content.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(cm) + "\n")
    # ---- pages/pNNN.md ----
    pdir = os.path.join(out_dir, "pages")
    os.makedirs(pdir, exist_ok=True)
    for p in content["pages"]:
        pno = p["page"]
        imgs = [i for i in img_meta if i["page"] == pno]
        tbls = [t for t in tables if t.get("page") == pno]
        lines = ["# Page %s（共 %s 行）" % (pno, len(p["lines"])), ""]
        for ln in p["lines"]:
            lines.append("%s %s" % (_anchor(pno, ln["no"]), ln["text"]))
        if tbls:
            lines += ["", "## 表格"] + ["- [%s](tables/%s.md)" % (t["id"], t["id"]) for t in tbls]
        if imgs:
            lines += ["", "## 图片"] + ["- %s ([p%03d-I%s])" % (i["id"], pno, i["id"][-2:]) for i in imgs]
        with open(os.path.join(pdir, "p%03d.md" % pno), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    # ---- tables/tNNN.md ----
    tdir = os.path.join(out_dir, "tables")
    os.makedirs(tdir, exist_ok=True)
    for t in tables:
        lr = t.get("line_range") or [None, None]
        head = "# %s — 第 %s 页" % (t["id"], t.get("page"))
        rang = "[p%03d-L%04d..L%04d]" % (t["page"], lr[0], lr[1]) if lr[0] else ""
        rows = ["| " + " | ".join(r) + " |" for r in t["rows"]]
        with open(os.path.join(tdir, t["id"] + ".md"), "w", encoding="utf-8") as f:
            f.write(head + " " + rang + "\n\n" + "\n".join(rows) + "\n")
    # ---- __info__.json ----
    info = dict(meta)
    info.update({"pages": len(content["pages"]), "total_lines": content["total_lines"],
                 "total_tables": len(tables), "images": len(img_meta),
                 "images_recognized": 0, "images_unreadable": 0,
                 "version": "1.0.0", "mode": "full"})
    with open(os.path.join(out_dir, "__info__.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    # ---- __overview__.md 骨架 ----
    warns = []
    for i in img_meta:
        if not i.get("decorative"):
            warns.append(i["id"])
    ov = ["# %s — PDF 概览（pdfreader）" % meta["pdf_name"], "",
          "## 来源", "- 文件: `%s` | 页数: %s | 总行数: %s | 表格: %s | 图片: %s" %
          (meta["filename"], len(content["pages"]), content["total_lines"], len(tables), len(img_meta)), "",
          "## 每页速览", "", "| 页 | 行数 | 预览 | 图表 |", "|---|---|---|---|"]
    for p in content["pages"]:
        prev = (p["lines"][0]["text"][:40] if p["lines"] else "（无文字层）")
        imgs = sum(1 for i in img_meta if i["page"] == p["page"])
        tbls = sum(1 for t in tables if t.get("page") == p["page"])
        ov.append("| %s | %s | %s | %s图%s表 |" % (p["page"], len(p["lines"]), prev, imgs, tbls))
    if img_meta:
        # 读取 vision 结果补充描述
        vis = {}
        vp = os.path.join(out_dir, "vision.json")
        if os.path.exists(vp):
            with open(vp, encoding="utf-8") as f:
                raw = json.load(f)
            items = raw if isinstance(raw, list) else (raw.get("results") or raw.get("images") or [])
            for v in (items if isinstance(items, list) else [items]):
                if isinstance(v, dict) and v.get("image_id"):
                    vis[v["image_id"]] = v
        ov += ["", "## 图表清单", "", "| 图 | 页 | 类型 | 状态 | 描述 |", "|---|---|---|---|---|"]
        for i in img_meta:
            v = vis.get(i["id"], {})
            if i.get("decorative"):
                st, desc = "装饰(跳过)", ""
            elif v.get("image_id"):
                st = "✅ 已识别 (confidence=%s)" % (v.get("confidence") or "?")
                desc = (v.get("content_desc") or "")[:48]
            else:
                st = "⚠️ 未解读（agent 图片识别未运行/不可用）"
                desc = ""
            ov.append("| %s | %s | %s | %s | %s |" % (i["id"], i["page"], v.get("kind") or "-", st, desc))
        unread = [i["id"] for i in img_meta if not i.get("decorative") and not vis.get(i["id"], {}).get("image_id")]
        if unread:
            ov += ["", "> ⚠️ 提醒: 存在未解读图片 %s。原因见 `__protocol__.md`；可在支持视觉的模型中执行 S4 补解读。" % ", ".join(unread)]
    ov += ["", "## 导航", "- 全文(带行锚点): `content.md`", "- 逐页: `pages/`",
           "- 表格: `tables/`", "- 图片: `images/`", "- 主题索引: `__index__.md`",
           "- 校验记录: `__protocol__.md`", "", "> 行锚点 `[p003-L0012]` = 第3页第12行，可定位回原 PDF。"]
    with open(os.path.join(out_dir, "__overview__.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(ov) + "\n")
    # ---- __index__.md（词频→锚点）----
    word_hits = Counter()
    for p in content["pages"]:
        for ln in p["lines"]:
            for w in re.findall(r"[A-Za-z]{3,}|[\u4e00-\u9fff]{2,}", ln["text"].lower()):
                if w not in STOPWORDS:
                    word_hits[w] += 1
    idx = ["# 主题索引（词频 Top 60）", ""]
    for w, c in word_hits.most_common(60):
        anchors = []
        for p in content["pages"]:
            for ln in p["lines"]:
                if w in ln["text"].lower():
                    anchors.append(_anchor(p["page"], ln["no"]))
        idx.append("- **%s** (%s次): %s" % (w, c, ", ".join(anchors[:5])))
    with open(os.path.join(out_dir, "__index__.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(idx) + "\n")
    return json.dumps({"ok": True, "pages": len(content["pages"]), "lines": content["total_lines"],
                       "tables": len(tables), "images": len(img_meta)}, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="pdfreader build_overview")
    ap.add_argument("out_dir")
    a = ap.parse_args()
    print(run(a.out_dir))
