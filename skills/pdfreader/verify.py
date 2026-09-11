# deps: stdlib only
"""pdfreader: 完整性校验 → __protocol__.md + 校验报告"""
import json, os

def _is_recognized(out_dir, img):
    """图片解读状态：iXXX.md 含 '状态: recognized' 才算已识别"""
    p = os.path.join(out_dir, "images", img["id"] + ".md")
    if not os.path.exists(p):
        return False
    return "状态: recognized" in open(p, encoding="utf-8").read()


def run(out_dir: str) -> str:
    issues, ok = [], True
    def rd(name, default=None):
        p = os.path.join(out_dir, name)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return f.read()
        issues.append("缺少文件: " + name)
        return default
    # JSON 数据校验
    try:
        with open(os.path.join(out_dir, "content.json"), encoding="utf-8") as f:
            content = json.load(f)
    except Exception as e:
        content = None
        ok = False
        issues.append("content.json 无法解析: %s" % e)
    if content:
        seen_anchors = set()
        for p in content["pages"]:
            pno = p["page"]
            if pno < 1 or pno > content["pages"][-1]["page"]:
                ok = False; issues.append("页号越界: %s" % pno)
            for no, ln in enumerate(p["lines"], start=1):
                if ln["no"] != no:
                    ok = False; issues.append("第%s页行号不连续: 期望%s 实为%s" % (pno, no, ln["no"]))
                a = "[p%03d-L%04d]" % (pno, no)
                if a in seen_anchors:
                    ok = False; issues.append("锚点重复: " + a)
                seen_anchors.add(a)
        if len(content["pages"]) != content["pages"][-1]["page"]:
            ok = False; issues.append("页号不连续")
    # 图片/表格/文件
    img_meta = []
    try:
        with open(os.path.join(out_dir, "images_meta.json"), encoding="utf-8") as f:
            img_meta = json.load(f).get("images", [])
        for i in img_meta:
            if not os.path.exists(os.path.join(out_dir, i["path"])):
                ok = False; issues.append("图片文件缺失: " + i["path"])
    except FileNotFoundError:
        pass
    try:
        with open(os.path.join(out_dir, "tables.json"), encoding="utf-8") as f:
            for t in json.load(f).get("tables", []):
                lr = t.get("line_range") or [None, None]
                if lr[0] and lr[1] and lr[0] > lr[1]:
                    ok = False; issues.append("表 %s 行区间倒置" % t["id"])
    except FileNotFoundError:
        pass
    for fn in ["__info__.json", "__overview__.md", "content.md", "__index__.md"]:
        if rd(fn) is None:
            ok = False
    # 统计一致
    try:
        with open(os.path.join(out_dir, "__info__.json"), encoding="utf-8") as f:
            info = json.load(f)
        if content and info.get("total_lines") != content["total_lines"]:
            ok = False; issues.append("__info__ 行数统计不一致")
    except Exception:
        pass
    # __protocol__.md
    protocol = ["# 校验协议（__protocol__.md）", "",
                "- 校验时间: %s" % __import__("datetime").datetime.now().isoformat(timespec="seconds"),
                "- 行锚点: 唯一/连续 → %s" % ("✅" if ok else "❌"),
                "- 页号: 连续 → %s" % ("✅" if ok else "❌"),
                "- 图片: %s 张（%s 装饰跳过）" % (len(img_meta), sum(1 for i in img_meta if i.get("decorative"))),
                "- 识别状态: %s 已识别 / %s 未解读" % (
                    sum(1 for i in img_meta if not i.get("decorative") and _is_recognized(out_dir, i)),
                    sum(1 for i in img_meta if not i.get("decorative") and not _is_recognized(out_dir, i))),
                "- agent 图片识别: %s" % ("已运行" if os.path.exists(os.path.join(out_dir, "vision.json")) else "未运行（占位/降级）"),
                "", "## 方法说明", "- 行 = 视觉行（LLM 布局行），页内 top 升序编号",
                "- 锚点 [p003-L0012] → 原 PDF 第 3 页第 12 行",
                "", "## 检查项", *(["- ❌ " + i for i in issues] or ["- ✅ 全部通过"]), ""]
    with open(os.path.join(out_dir, "__protocol__.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(protocol))
    return json.dumps({"ok": ok, "issues": issues}, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="pdfreader verify")
    ap.add_argument("out_dir")
    a = ap.parse_args()
    print(run(a.out_dir))
