# deps: stdlib only, pdfminer.high_level（需 build-unsafe 环境，执行前提示用户切换模式）, pdfminer.layout（需 build-unsafe 环境，执行前提示用户切换模式）
"""pdfreader: 嵌入图像提取（pdfminer.six LTImage）。输出 images/iNNN.{jpg,png} + images_meta.json"""
import json, os, struct, zlib


def _miner():
    try:
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LAParams, LTImage, LTContainer
    except ModuleNotFoundError as e:
        raise RuntimeError("PDF解析库不可用: 受限沙箱请切 build-unsafe；或宿主 pip install pdfminer.six") from e
    return dict(extract_pages=extract_pages, LAParams=LAParams, LTImage=LTImage, LTContainer=LTContainer)


def _png(w, h, data, colorspace):
    """原始像素流 → PNG（DeviceGray / DeviceRGB）"""
    color_type = 2 if (colorspace and "RGB" in str(colorspace)) else 0
    nch = 3 if color_type == 2 else 1
    def chunk(tag, payload):
        c = tag + payload
        return struct.pack(">I", len(payload)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, 0)
    raw = b"".join(b"\x00" + data[y*w*nch:(y+1)*w*nch] for y in range(h))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def _attrs(stream):
    """流属性 dict：兼容 get_attrs() / attrs；值做基本类型归一"""
    d = {}
    try:
        if hasattr(stream, "get_attrs"):
            d = stream.get_attrs()
        elif hasattr(stream, "attrs"):
            d = stream.attrs
    except Exception:
        d = {}
    out = {}
    for k, v in (d or {}).items():
        try:
            out[str(k).lstrip("/")] = int(v) if str(v).isdigit() else str(v)
        except Exception:
            out[str(k).lstrip("/")] = str(v)
    return out


def run(pdf_path: str, out_dir: str) -> str:
    m = _miner()
    img_dir = os.path.join(out_dir, "images")
    os.makedirs(img_dir, exist_ok=True)
    metas, seen = [], {}
    def _collect_imgs(el, out):
        if not isinstance(el, m["LTContainer"]):
            return
        for sub in el:
            if isinstance(sub, m["LTImage"]):
                out.append(sub)
            elif isinstance(sub, m["LTContainer"]):
                _collect_imgs(sub, out)

    for page_no, page_layout in enumerate(m["extract_pages"](pdf_path, laparams=m["LAParams"]()), start=1):
        imgs = []
        for el in page_layout:
            _collect_imgs(el, imgs)
        for el in imgs:
            if not isinstance(el, m["LTImage"]):
                continue
            name = getattr(el, "name", "") or "img"
            if "smask" in str(name).lower():
                continue
            stream = el.stream
            attrs = _attrs(stream)
            raw = None
            try:
                raw = stream.get_rawdata()
            except Exception:
                raw = None
            ext = None
            if raw and raw[:3] == b"\xff\xd8\xff":
                ext = "jpg"
            elif raw and raw[:8] == b"\x89PNG\r\n\x1a\n":
                ext = "png"
            if ext is None:
                try:
                    filters = str(stream.get_filters())
                except Exception:
                    filters = ""
                if "FlateDecode" in filters:
                    try:
                        d = stream.get_data()
                        aw = int(attrs.get("Width") or 0)
                        ah = int(attrs.get("Height") or 0)
                        cs = attrs.get("ColorSpace", "/DeviceGray")
                        if aw > 0 and ah > 0 and len(d) >= aw * ah:
                            png = _png(aw, ah, d, cs)
                            if png[:8] == b"\x89PNG\r\n\x1a\n":
                                ext, raw = "png", png
                    except Exception:
                        ext = None
            if ext is None:
                continue
            aw = int(attrs.get("Width") or 0)
            ah = int(attrs.get("Height") or 0)
            if not aw or not ah:   # attrs 缺失 → 回退 bbox 显示尺寸
                aw = int(round(el.x1 - el.x0))
                ah = int(round(el.y1 - el.y0))
            if name in seen:
                seen[name]["pages"].append(page_no)
                continue
            iid = "i%03d" % (len(metas) + 1)
            fname = iid + "." + ext
            with open(os.path.join(img_dir, fname), "wb") as f:
                f.write(raw)
            rec = {"id": iid, "name": name, "page": page_no, "pages": [page_no],
                   "bbox": [round(el.x0, 2), round(el.y0, 2), round(el.x1, 2), round(el.y1, 2)],
                   "width": aw, "height": ah, "ext": ext, "path": "images/" + fname,
                   "decorative": (aw < 30 or ah < 30), "extracted": True}
            metas.append(rec)
            seen[name] = rec
    for rec in metas:
        with open(os.path.join(img_dir, rec["id"] + ".md"), "w", encoding="utf-8") as f:
            f.write("# %s\n\n- 状态: unreadable_agent: 尚未运行 agent 图片识别\n- 位置: [p%03d-I%s]\n" % (
                rec["id"], rec["page"], rec["id"][-2:]))
    with open(os.path.join(out_dir, "images_meta.json"), "w", encoding="utf-8") as f:
        json.dump({"images": metas}, f, ensure_ascii=False, indent=2)
    return json.dumps({"ok": True, "images": len(metas),
                       "decorative": sum(1 for x in metas if x["decorative"]),
                       "out": os.path.abspath(img_dir)}, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="pdfreader extract_images")
    ap.add_argument("pdf"); ap.add_argument("--out", default=".")
    a = ap.parse_args()
    print(run(a.pdf, a.out))
