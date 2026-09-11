# deps: stdlib only, pdfminer（需 build-unsafe 环境，执行前提示用户切换模式）, pdfminer.high_level（需 build-unsafe 环境，执行前提示用户切换模式）, pdfminer.layout（需 build-unsafe 环境，执行前提示用户切换模式）
"""pdfreader: 文字+表格+元数据提取（pdfminer.six）。
坐标约定：x0/y0/x1/y1（PDF 坐标，y 向上；视觉行序 = y0 降序）。
函数返回字符串(JSON)；__main__ 可 CLI。"""
import hashlib, json, os, sys, zipfile, urllib.request

MINER_IMPORT_ERR = "PDF解析库(pdfminer.six)不可用: 受限沙箱请切 build-unsafe；或宿主 pip install pdfminer.six 后用 !python3 <本脚本> <pdf> --out <dir>"


def ensure_libs():
    """确保 pdfminer.six 可导入；缺失时从 PyPI 下载纯py wheel 注入。"""
    try:
        import pdfminer  # noqa
        return True
    except ModuleNotFoundError as e:
        if "sandbox" in str(e).lower():
            return False
    libdir = "/tmp/pdfreader_libs"
    os.makedirs(libdir, exist_ok=True)
    try:
        req = urllib.request.Request("https://pypi.org/pypi/pdfminer.six/json",
                                     headers={"User-Agent": "Mozilla/5.0"})
        import json as _j
        with urllib.request.urlopen(req, timeout=30) as r:
            info = _j.loads(r.read())
        url = fn = None
        for f in info["urls"]:
            if f["packagetype"] == "bdist_wheel" and f["filename"].endswith("py3-none-any.whl"):
                url, fn = f["url"], f["filename"]
                break
        if not url:
            return False
        dest = os.path.join(libdir, fn)
        if not os.path.exists(dest):
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=120) as r:
                data = r.read()
            with open(dest, "wb") as f:
                f.write(data)
        with zipfile.ZipFile(dest) as z:
            z.extractall(os.path.join(libdir, "pkg"))
        sys.path.insert(0, os.path.join(libdir, "pkg"))
        import pdfminer  # noqa
        return True
    except Exception:
        return False


def _miner():
    if not ensure_libs():
        raise RuntimeError(MINER_IMPORT_ERR)
    from pdfminer.layout import LAParams, LTTextLine, LTChar, LTContainer, LTLine, LTRect, LTCurve
    from pdfminer.high_level import extract_pages
    return dict(LAParams=LAParams, LTTextLine=LTTextLine, LTChar=LTChar,
                LTContainer=LTContainer, LTLine=LTLine, LTRect=LTRect, LTCurve=LTCurve,
                extract_pages=extract_pages)


def _walk_lines(el, out, m):
    """递归收集 LTTextLine（位于 LTTextBox 内层）"""
    for sub in el:
        if isinstance(sub, m["LTTextLine"]):
            out.append(sub)
        elif isinstance(sub, m["LTContainer"]):
            _walk_lines(sub, out, m)


def _grid_tables(page_layout, lines, page_no, m):
    """线段(LTLine/LTRect/LTCurve)聚类→网格→字符归单元格"""
    shapes = [el for el in page_layout
              if isinstance(el, (m["LTLine"], m["LTRect"], m["LTCurve"]))]
    hs, vs = [], []
    for s in shapes:
        x0, y0, x1, y1 = s.x0, s.y0, s.x1, s.y1
        if (y1 - y0) <= 2.0:
            hs.append(round(y0, 1))
        elif (x1 - x0) <= 2.0:
            vs.append(round(x0, 1))
        else:
            hs += [round(y0, 1), round(y1, 1)]
            vs += [round(x0, 1), round(x1, 1)]
    if len(hs) < 2 or len(vs) < 2:
        return []

    def cluster(vals, tol=3.0):
        vals = sorted(set(vals))
        out = []
        for v in vals:
            if out and abs(v - out[-1][-1]) <= tol:
                out[-1].append(v)
            else:
                out.append([v])
        return [sum(c) / len(c) for c in out]

    ylines = cluster(hs)   # PDF y（大=上）：表顶 y 最大
    xlines = cluster(vs)
    if len(ylines) < 2 or len(xlines) < 2:
        return []
    chars = []
    def walk_chars(el):
        if not isinstance(el, m["LTContainer"]):
            return
        for sub in el:
            if isinstance(sub, m["LTChar"]):
                chars.append((sub.get_text(), sub.x0, sub.y0, sub.x1, sub.y1))
            elif isinstance(sub, m["LTContainer"]):
                walk_chars(sub)
    for el in page_layout:
        walk_chars(el)
    rows = []
    # 视觉行序 = 从上到下（PDF y 大→小）
    for j in range(len(ylines) - 2, -1, -1):
        lo, hi = ylines[j], ylines[j + 1]   # [下界, 上界]
        row = []
        for i in range(len(xlines) - 1):
            cx0, cx1 = xlines[i], xlines[i + 1]
            cell = "".join(ch for ch, a, b, c, d in chars
                           if ch.strip() and (a + c) / 2.0 >= cx0 - 0.5 and (a + c) / 2.0 <= cx1 + 0.5
                           and (b + d) / 2.0 >= lo - 0.5 and (b + d) / 2.0 <= hi + 0.5)
            row.append(cell.strip())
        if any(r for r in row):
            rows.append(row)
    if not rows:
        return []
    top_y, bottom_y = max(ylines), min(ylines)
    in_tbl = [ln for ln in lines if not (ln["y1"] < bottom_y or ln["y0"] > top_y)]
    line_range = [min(l["no"] for l in in_tbl), max(l["no"] for l in in_tbl)] if in_tbl else [None, None]
    return [{"page": page_no, "bbox": [min(xlines), min(ylines), max(xlines), max(ylines)],
             "rows": rows, "line_range": line_range}]


def run(pdf_path: str, out_dir: str) -> str:
    """主入口：写 meta.json / content.json / tables.json → 报告 JSON"""
    m = _miner()
    laparams = m["LAParams"](char_margin=2.0, line_margin=0.5, word_margin=0.1)
    os.makedirs(out_dir, exist_ok=True)
    with open(pdf_path, "rb") as f:
        data = f.read()
    meta_info = {
        "filename": os.path.basename(pdf_path),
        "pdf_name": os.path.splitext(os.path.basename(pdf_path))[0],
        "path": os.path.abspath(pdf_path), "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest()[:16],
    }
    pages_out, tables_all, nlines = [], [], 0
    for page_no, page_layout in enumerate(m["extract_pages"](pdf_path, laparams=laparams), start=1):
        raw_lines = []
        _walk_lines(page_layout, raw_lines, m)
        lines = []
        for el in raw_lines:
            t = el.get_text().strip()
            if t:
                lines.append({"text": t, "x0": round(el.x0, 2), "y0": round(el.y0, 2),
                              "x1": round(el.x1, 2), "y1": round(el.y1, 2)})
        # 视觉行序：PDF y 向上 → y0 降序
        lines.sort(key=lambda ln: (-ln["y0"], ln["x0"]))
        for no, ln in enumerate(lines, start=1):
            ln["no"] = no
        nlines += len(lines)
        pages_out.append({"page": page_no, "width": round(page_layout.width, 2),
                          "height": round(page_layout.height, 2), "lines": lines})
        for tbl in _grid_tables(page_layout, lines, page_no, m):
            tbl["id"] = "t%03d" % (len(tables_all) + 1)
            tables_all.append(tbl)
    content = {"pdf": os.path.basename(pdf_path), "pages": pages_out, "total_lines": nlines}
    meta_info.update({"pages": len(pages_out), "total_lines": nlines, "total_tables": len(tables_all)})
    for name, obj in [("meta.json", meta_info), ("content.json", content),
                      ("tables.json", {"tables": tables_all})]:
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    return json.dumps({"ok": True, "pages": len(pages_out), "lines": nlines,
                       "tables": len(tables_all), "out": os.path.abspath(out_dir)},
                      ensure_ascii=False, indent=2)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="pdfreader extract_light")
    ap.add_argument("pdf"); ap.add_argument("--out", default=".")
    a = ap.parse_args()
    print(run(a.pdf, a.out))
