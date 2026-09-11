# deps: stdlib only
"""pdfreader: 图↔行区间关联 + images/iNNN.md 解读文件生成（vision.json 可选）"""
import json, os


def run(out_dir: str, vision_json: str | None = None) -> str:
    with open(os.path.join(out_dir, "content.json"), encoding="utf-8") as f:
        content = json.load(f)
    with open(os.path.join(out_dir, "images_meta.json"), encoding="utf-8") as f:
        img_meta = json.load(f)
    vision = {}
    if vision_json and os.path.exists(vision_json):
        with open(vision_json, encoding="utf-8") as f:
            raw = json.load(f)
        items = raw
        if isinstance(raw, dict):
            items = raw.get("results") or raw.get("images") or [raw]
        if isinstance(items, dict):
            items = [items]
        for v in items:
            if isinstance(v, dict) and v.get("image_id"):
                vision[v["image_id"]] = v
    pages_lines = {p["page"]: p["lines"] for p in content["pages"]}
    merged = []
    for rec in img_meta["images"]:
        lines = pages_lines.get(rec["page"], [])
        # 图中心（PDF 坐标 y 向上：y0=下界, y1=上界）
        ix0, iy0, ix1, iy1 = rec["bbox"]
        icy = (iy0 + iy1) / 2.0
        above = [ln for ln in lines if (ln["y0"] + ln["y1"]) / 2.0 > icy]
        below = [ln for ln in lines if (ln["y0"] + ln["y1"]) / 2.0 <= icy]
        la = min(above, key=lambda ln: (ln["y0"] + ln["y1"]) / 2.0)["no"] if above else None
        lb = max(below, key=lambda ln: (ln["y0"] + ln["y1"]) / 2.0)["no"] if below else None
        if la is not None and lb is not None and la <= lb:
            line_range = [la, lb]
        elif la is not None:
            line_range = [la, la]
        elif lb is not None:
            line_range = [lb, lb]
        else:
            line_range = None
        context = [ln["text"] for ln in lines
                   if line_range and line_range[0] <= ln["no"] <= line_range[1]]
        v = vision.get(rec["id"], {})
        status = "decorative" if rec.get("decorative") else (
            "recognized" if v.get("image_id") else "unreadable_agent")
        md = ["# %s" % rec["id"], "",
              "- 位置: [p%03d-I%s]" % (rec["page"], rec["id"][-2:]),
              "- 类型: %s" % (v.get("kind") or "unknown"),
              "- 状态: %s" % status]
        if v.get("caption_title"):
            md.append("- 标题: %s" % v["caption_title"])
        md += ["", "## 内容描述", v.get("content_desc") or "（agent 未识别或未运行）", "",
               "## 图内文字(OCR)", v.get("ocr_text") or "（无）", "",
               "## 关键数值", json.dumps(v.get("values") or {}, ensure_ascii=False), "",
               "## 关联行上下文", *(["- %s" % c for c in context] or ["（无）"]), ""]
        if v.get("unreadable_reason"):
            md += ["## 备注", v["unreadable_reason"]]
        with open(os.path.join(out_dir, "images", rec["id"] + ".md"), "w", encoding="utf-8") as f:
            f.write("\n".join(md))
        merged.append({"id": rec["id"], "page": rec["page"], "line_range": line_range,
                       "above_line": la, "below_line": lb, "status": status, "context": context})
    with open(os.path.join(out_dir, "merged.json"), "w", encoding="utf-8") as f:
        json.dump({"images": merged}, f, ensure_ascii=False, indent=2)
    return json.dumps({"ok": True, "images": len(merged),
                       "recognized": sum(1 for x in merged if x["status"] == "recognized"),
                       "unreadable": sum(1 for x in merged if x["status"] == "unreadable_agent")},
                      ensure_ascii=False, indent=2)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="pdfreader merge_align")
    ap.add_argument("out_dir"); ap.add_argument("--vision", default=None)
    a = ap.parse_args()
    print(run(a.out_dir, a.vision))
