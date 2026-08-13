"""example_dulwich.py — dulwich 在受限沙箱内的完整流程演示。

用法（build/build-unsafe 下）：
    python example_dulwich.py            # 完整演示 init→add→commit→log→branch→status
    python example_dulwich.py --safety   # 安全回归（越界拒绝 + subprocess stub）

设计意图：演示 pythonrt_utils 接入的 dulwich 在受限沙箱内的真实能力边界——
核心 git 操作全可用；subprocess 命令执行、越界 IO 被拒。
纯标准库 + dulwich。
"""

import io
import os
import sys
import tempfile

from dulwich import porcelain, repo


def demo_full_git_flow() -> None:
    """完整 git 流程：init → add → commit×2 → log → branch → status → diff。"""
    workdir = tempfile.mkdtemp(prefix="dulwich_demo_")
    rp = os.path.join(workdir, "demo_repo")
    print(f"[1] porcelain.init({rp})")
    porcelain.init(rp)

    fpath = os.path.join(rp, "a.txt")
    with open(fpath, "w", encoding="utf-8") as fh:
        fh.write("hello\n")
    print("[2] porcelain.add(a.txt)")
    porcelain.add(repo=rp, paths=["a.txt"])

    cid1 = porcelain.commit(repo=rp, message=b"c1",
                            author=b"T <t@t>", committer=b"T <t@t>")
    print(f"[3] commit1: {cid1.decode()[:10]}")

    with open(fpath, "a", encoding="utf-8") as fh:
        fh.write("line2\n")
    porcelain.add(repo=rp, paths=["a.txt"])
    cid2 = porcelain.commit(repo=rp, message=b"c2",
                            author=b"T <t@t>", committer=b"T <t@t>")
    print(f"[4] commit2: {cid2.decode()[:10]}")

    buf = io.StringIO()
    porcelain.log(repo=rp, outstream=buf)
    print(f"[5] log 行数: {len(buf.getvalue().strip().splitlines())}")

    r = repo.Repo(rp)
    commits = [e.commit for e in r.get_walker()]
    print(f"[6] walker commits: {len(commits)} -> "
          f"{[c.message.decode().strip() for c in commits]}")
    r.close()

    branches = porcelain.branch_list(repo=rp)
    print(f"[7] branches: {[b.decode() for b in branches]}")

    staged, _unstaged, untracked = porcelain.status(repo=rp)
    print(f"[8] status: staged={sorted(staged)} untracked={sorted(untracked)}")

    # dulwich 1.2.12 的 diff_tree 返回 None（结果写入 outstream）；补丁数据为
    # bytes → 需二进制流（StringIO 会因 bytes 写入报错，实测踩坑）
    buf2 = io.BytesIO()
    porcelain.diff_tree(rp, commits[1].tree, commits[0].tree, outstream=buf2)
    diff_out = buf2.getvalue()
    print(f"[9] diff 输出行数: {len(diff_out.strip().splitlines())}")
    print("=== FULL_FLOW_OK ===")


def demo_safety() -> None:
    """安全回归：越界拒绝 + subprocess stub 仍生效。"""
    import subprocess

    checks = []

    # 越界 init 拒绝
    try:
        porcelain.init("/home")
        checks.append(("越界 init('/home')", False))
    except PermissionError:
        checks.append(("越界 init('/home') → PermissionError", True))

    # subprocess stub
    try:
        subprocess.Popen(["echo", "hi"])
        checks.append(("subprocess.Popen", False))
    except PermissionError:
        checks.append(("subprocess.Popen → PermissionError", True))

    try:
        subprocess.run(["ls"])
        checks.append(("subprocess.run", False))
    except PermissionError:
        checks.append(("subprocess.run → PermissionError", True))

    # 越界读
    try:
        open("/etc/passwd", "rb")
        checks.append(("读 /etc/passwd", False))
    except PermissionError:
        checks.append(("读 /etc/passwd → PermissionError", True))

    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if all(ok for _n, ok in checks):
        print("=== SAFETY_OK ===")
    else:
        print("=== SAFETY_FAILED ===")
        sys.exit(1)


def main() -> None:
    """CLI 入口：默认完整流程；--safety 仅安全回归。"""
    if "--safety" in sys.argv:
        demo_safety()
    else:
        demo_full_git_flow()
        print()
        demo_safety()


if __name__ == "__main__":
    main()
