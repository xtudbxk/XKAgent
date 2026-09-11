# deps: stdlib only
"""download_arxiv.py - Download paper TeX source / PDF from arXiv.

Usage (python):
    from skills.download_arxiv.download_arxiv import download_arxiv
    result = download_arxiv("2406.02507")          # JSON string

Usage (CLI):
    python skills/download_arxiv/download_arxiv.py 2406.02507 [--pdf] [--output-dir DIR] [--no-unpack]

Behavior:
    - default: download TeX source (tar.gz / gz / single .tex) -> {out}/{id}/tex/{id}.source
               then unpack into {out}/{id}/tex/unpacked/
    - --pdf:   additionally download PDF -> {out}/{id}/{id}.pdf
    - output dir default: <workdir>/docs/arxiv  (relative path resolved at call time)

Sandbox note:
    Restricted pythonrt (plan/build) bans urllib.request / tarfile / shutil / tempfile
    (import chain -> posix).  This module therefore:
      * downloads via http.client + manual redirect-following (always works)
      * unpacks via a pure-stdlib tar parser (GNU longname / PAX / dir / symlink-skip),
        falling back to stdlib tarfile when it is importable (host Python / build-unsafe)
"""
import os
import sys
import gzip
import json
import time
import http.client

TEX_URL = "https://export.arxiv.org/e-print/{arxiv_id}"
PDF_URL = "https://export.arxiv.org/pdf/{arxiv_id}"
DEFAULT_OUTPUT = "docs/arxiv"          # relative to workdir / CWD
USER_AGENT = "Mozilla/5.0 (download_arxiv skill; python-urllib)"


def normalize_arxiv_id(raw):
    """Extract a bare arXiv ID from an ID or URL like
    '2406.02507' / 'https://arxiv.org/abs/2406.02507' / 'arxiv.org/pdf/2406.02507v2'."""
    import re
    if not raw or not isinstance(raw, str):
        raise ValueError("arxiv_id is required")
    s = raw.strip()
    # URL forms: abs/ID, pdf/ID, e-print/ID; strip trailing query/fragment
    for marker in ("/abs/", "/pdf/", "/e-print/"):
        if marker in s:
            s = s.split(marker, 1)[1]
    s = s.split("?")[0].split("#")[0].strip("/")
    # bare ID pattern: YYMM.NNNNN (optionally vN); legacy 4-digit tail allowed
    m = re.match(r"^(\d{4}\.\d{4,5})(v\d+)?$", s)
    if not m:
        raise ValueError(f"invalid arXiv ID: {raw!r} (expect e.g. 2406.02507)")
    return m.group(1)


def _http_get(url, timeout=120, max_redirects=5):
    """HTTP(S) GET with redirect following via http.client (sandbox-safe).

    Returns (status, final_url, body_bytes). Raises on network/HTTP error."""
    cur = url
    for _ in range(max_redirects + 1):
        proto, rest = cur.split("://", 1)
        host, _, path = rest.partition("/")
        path = "/" + path if path and not path.startswith("/") else (path or "/")
        conn = http.client.HTTPSConnection(host, timeout=timeout)
        try:
            conn.request("GET", path,
                         headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
            resp = conn.getresponse()
            body = resp.read()
        finally:
            conn.close()
        if resp.status in (301, 302, 303, 307, 308):
            loc = resp.getheader("Location")
            if not loc:
                return resp.status, cur, body
            cur = loc if loc.startswith("http") else f"{proto}://{host}{loc}"
            continue
        return resp.status, cur, body
    raise RuntimeError(f"too many redirects: {url}")


def _download(url, dest, timeout=120, retries=2):
    """Download url to dest. Returns bytes written. Retries with backoff."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            status, final_url, data = _http_get(url, timeout=timeout)
            if status != 200:
                raise RuntimeError(f"HTTP {status} for {url} (-> {final_url})")
            with open(dest, "wb") as f:
                f.write(data)
            return len(data)
        except Exception as e:  # noqa: BLE001 - retry on any network error
            last_err = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"download failed after {retries+1} attempts: {url} :: {last_err}")


def _unpack_tar_manual(data, unpack_dir, arxiv_id):
    """Pure-stdlib tar/tar.gz extractor (fallback when tarfile is banned).

    Handles: gzip magic, ustar/GNU tar, GNU longname ('L'), PAX extended
    header ('x'), regular files, dirs; skips symlinks; sanitizes paths.
    Returns list of extracted file names (top-level view)."""
    if data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except Exception:
            # 单个 gzip 的 .tex：按 id.tex 落盘
            tex_name = arxiv_id + ".tex"
            with open(os.path.join(unpack_dir, tex_name), "wb") as f:
                f.write(data)
            return [tex_name]
    # 判别是否为 tar：ustar/GNU magic + 非空 name 字段
    is_tar = (len(data) >= 512 and data[257:263] in (b"ustar\x00", b"ustar ")
              and data[:100].strip(b"\x00") != b"")
    if not is_tar:
        # 纯 .tex（未压缩）
        tex_name = arxiv_id + ".tex"
        with open(os.path.join(unpack_dir, tex_name), "wb") as f:
            f.write(data)
        return [tex_name]
    extracted = []
    pending_longname = None
    pos, n = 0, len(data)
    while pos + 512 <= n:
        hdr = data[pos:pos + 512]
        if hdr == b"\x00" * 512:
            break
        name = hdr[:100].split(b"\x00")[0].decode("utf-8", "replace").rstrip("/")
        try:
            size = int(hdr[124:136].strip(b"\x00") or b"0", 8)
        except ValueError:
            break
        ftype = hdr[156:157]
        nblocks = (size + 511) // 512
        content = data[pos + 512:pos + 512 + nblocks * 512]
        pos += 512 + nblocks * 512
        if ftype == b"L":  # GNU longname
            pending_longname = content.split(b"\x00")[0].decode("utf-8", "replace")
            continue
        if ftype == b"x":  # PAX extended header
            for line in content.split(b"\x00"):
                if b" path=" in line:
                    pending_longname = line.split(b" path=", 1)[1].decode("utf-8", "replace")
            continue
        if pending_longname:
            name = pending_longname
            pending_longname = None
        # 路径穿越防护
        norm = os.path.normpath(name)
        if norm.startswith(("/", "..")) or ".." in norm.split("/"):
            continue
        target = os.path.join(unpack_dir, norm)
        if ftype in (b"0", b"\x00", b""):  # regular file
            os.makedirs(os.path.dirname(target) or unpack_dir, exist_ok=True)
            with open(target, "wb") as f:
                f.write(content[:size])
            extracted.append(norm)
        elif ftype == b"5":  # directory
            os.makedirs(target, exist_ok=True)
        # symlink / other types: skipped (safe)
    return extracted[:20]


def _unpack(source_path, unpack_dir, arxiv_id):
    """Unpack a TeX source archive into unpack_dir. Returns list of names."""
    os.makedirs(unpack_dir, exist_ok=True)
    try:
        import tarfile
        if tarfile.is_tarfile(source_path):
            with tarfile.open(source_path, "r:*") as tf:
                members = [m for m in tf.getmembers()
                           if not m.name.startswith(("/", ".."))]
                tf.extractall(unpack_dir, members=members)
                return [m.name for m in members if m.isfile()][:20]
    except ImportError:
        pass  # tarfile 被禁（受限沙箱）→ 手动解析
    except Exception:
        pass  # 非 tar 文件 → 走手动逻辑
    with open(source_path, "rb") as f:
        data = f.read()
    return _unpack_tar_manual(data, unpack_dir, arxiv_id)


def download_arxiv(arxiv_id, pdf=False, output_dir=DEFAULT_OUTPUT,
                   unpack=True, timeout=120):
    """Download TeX source (+ optional PDF) for an arXiv paper.

    Returns JSON string with paths / sizes / status."""
    aid = normalize_arxiv_id(arxiv_id)
    out = os.path.abspath(os.path.expanduser(output_dir))
    os.makedirs(out, exist_ok=True)
    paper_dir = os.path.join(out, aid)
    tex_dir = os.path.join(paper_dir, "tex")
    os.makedirs(tex_dir, exist_ok=True)

    result = {"arxiv_id": aid, "output_dir": out, "files": [], "errors": []}

    # 1) TeX source (default)
    try:
        src_name = aid + ".source"
        src_path = os.path.join(tex_dir, src_name)
        size = _download(TEX_URL.format(arxiv_id=aid), src_path, timeout=timeout)
        result["files"].append({"type": "tex-source", "path": src_path, "size": size})
        if unpack:
            unpack_dir = os.path.join(tex_dir, "unpacked")
            extracted = _unpack(src_path, unpack_dir, aid)
            result["files"].append({"type": "tex-unpacked", "path": unpack_dir,
                                    "n_files": len(extracted), "samples": extracted})
    except Exception as e:  # noqa: BLE001
        result["errors"].append(f"tex: {e}")

    # 2) PDF (optional)
    if pdf:
        try:
            pdf_path = os.path.join(paper_dir, aid + ".pdf")
            size = _download(PDF_URL.format(arxiv_id=aid), pdf_path, timeout=timeout)
            result["files"].append({"type": "pdf", "path": pdf_path, "size": size})
        except Exception as e:  # noqa: BLE001
            result["errors"].append(f"pdf: {e}")

    result["status"] = "ok" if not result["errors"] else "partial"
    return json.dumps(result, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Download arXiv TeX source / PDF")
    ap.add_argument("arxiv_id", help="arXiv ID or URL, e.g. 2406.02507")
    ap.add_argument("--pdf", action="store_true", help="also download PDF")
    ap.add_argument("--output-dir", default=DEFAULT_OUTPUT,
                    help="output directory (default: docs/arxiv)")
    ap.add_argument("--no-unpack", action="store_true", help="keep archive, skip unpacking")
    ap.add_argument("--timeout", type=int, default=120)
    args = ap.parse_args()
    print(download_arxiv(args.arxiv_id, pdf=args.pdf, output_dir=args.output_dir,
                         unpack=not args.no_unpack, timeout=args.timeout))
