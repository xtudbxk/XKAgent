"""
search.py — 统一搜索：本地语义向量存储与检索 + 多范围×后缀路由×三方案级联

第一部分（向量核心，原 embeddings.py）:
  FAISS + SQLite + ONNX Runtime 存储与检索
第二部分（搜索框架）:
  多范围(skills/docs/historys/logs, codes 默认关闭) × 后缀路由 × embedding/ngram/grep

支持一个 key 对应多条 sentence，search 返回不重复的 top-k key。
当本地 models/all-MiniLM-L6-v2-onnx 不存在或不完整时，会自动下载并解压模型。

依赖:
  pip install faiss-cpu onnxruntime numpy transformers

用法:
    from codes.search import store, store_batch, search, delete, count, count_keys, list_keys

    # 批量存入（一个 key 对应多条 sentence）
    store_batch(["多轮互联网搜索与信息分析", "triggers: 搜索, 查资料, search"],
                "skills/embeddings.db", "web_search")

    # 搜索（返回不重复的 top-k key）
    keys = search("帮我搜一下AI框架", "skills/embeddings.db", top_k=3)
    # -> ["web_search", "plan", "check"]
"""

import hashlib
import json
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple

# faiss / numpy 缺失时降级：embedding 检索/编码不可用，
# search() 返回 [] → search_embedding 捕获后自动走纯 ngram
try:
    import faiss
except ImportError:
    faiss = None  # type: ignore

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore

import ast
import collections
import math
import os
import re
import time
from dataclasses import dataclass, field

from codes._log import logger


# ═══════════════════════════════════════════════════════════════
#  编码层 — ONNX Runtime（lazy singleton，线程安全）
# ═══════════════════════════════════════════════════════════════

_ONNX_MODEL_PARENT_DIR = Path(__file__).resolve().parent.parent / "models"
_ONNX_MODEL_NAME = "all-MiniLM-L6-v2-onnx"
_ONNX_MODEL_DIR = str(_ONNX_MODEL_PARENT_DIR / _ONNX_MODEL_NAME)
# 模型下载源（可选）：指向一个包含 all-MiniLM-L6-v2-onnx 模型目录的 tar.gz 打包。
# 默认留空 —— 缺失模型时提示手动准备（把模型目录放入 models/ 即可）；如需自动下载，可自行配置来源 URL。
# _ONNX_MODEL_DOWNLOAD_URL = "https://<your-host>/all-MiniLM-L6-v2-onnx.tar.gz"
_ONNX_MODEL_DOWNLOAD_URL = ""
_MODEL_DOWNLOAD_TIMEOUT_SECONDS = 300
_DOWNLOAD_CHUNK_SIZE_BYTES = 1024 * 1024
_DATA_PART_SUFFIX = ".part-"
_DATA_PART_META_SUFFIX = ".part-meta.json"
_REQUIRED_MODEL_FILES = (
    "model.onnx",
    "modules.json",
    "sentence_bert_config.json",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.txt",
    "1_Pooling/config.json",
)

_ONNX_SESSION = None
_ONNX_LOCK = threading.Lock()
_OK_ICON = "\u2705"    # 通过图标 (移出 f-string 表达式, 兼容 <3.12)
_FAIL_ICON = "\u274c"  # 失败图标 (移出 f-string 表达式, 兼容 <3.12)

_MODEL_PREPARE_LOCK = threading.Lock()


def _validate_positive_int(value: int, field_name: str) -> None:
    """校验正整数参数，避免超时、块大小等关键配置出现非法值。"""
    if not isinstance(value, int):
        raise ValueError(f"{field_name} 必须是 int，实际类型={type(value).__name__}")
    if value <= 0:
        raise ValueError(f"{field_name} 必须 > 0，实际值={value}")


def _normalize_model_dir(model_dir: "str | Path") -> Path:
    """将模型目录参数标准化为 Path。

    这样做是为了让后续下载、解压、加载逻辑只处理一种类型，降低分支复杂度。
    """
    if not isinstance(model_dir, (str, Path)):
        raise ValueError(f"model_dir 必须是 str 或 Path，实际类型={type(model_dir).__name__}")
    if isinstance(model_dir, str) and not model_dir.strip():
        raise ValueError("model_dir 不能为空字符串")
    return Path(model_dir)


def _get_data_parts(data_path: Path) -> List[Path]:
    """返回 model.onnx.data 的分卷文件列表，并忽略元数据文件。"""
    if not isinstance(data_path, Path):
        raise ValueError(f"data_path 必须是 Path，实际类型={type(data_path).__name__}")

    parts = sorted(data_path.parent.glob(f"{data_path.name}{_DATA_PART_SUFFIX}*"))
    return [part for part in parts if not part.name.endswith(_DATA_PART_META_SUFFIX)]


def _compute_md5(file_path: Path, chunk_size: int = _DOWNLOAD_CHUNK_SIZE_BYTES) -> str:
    """按块计算文件 md5，避免一次性将大文件读入内存。"""
    if not isinstance(file_path, Path):
        raise ValueError(f"file_path 必须是 Path，实际类型={type(file_path).__name__}")
    _validate_positive_int(chunk_size, "chunk_size")
    if not file_path.exists():
        raise FileNotFoundError(f"待计算 md5 的文件不存在: {file_path}")

    digest = hashlib.md5()
    with open(file_path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _has_model_data(data_path: Path) -> bool:
    """判断 ONNX 外部权重文件是否存在，或是否可由分卷恢复。"""
    if not isinstance(data_path, Path):
        raise ValueError(f"data_path 必须是 Path，实际类型={type(data_path).__name__}")
    return data_path.exists() or bool(_get_data_parts(data_path))


def _is_model_ready(model_dir: Path) -> bool:
    """检查模型目录是否完整可用。

    这里不仅检查 model.onnx，也检查 tokenizer / pooling 配置等依赖文件，
    避免下载后目录半残导致推理阶段才失败。
    """
    if not isinstance(model_dir, Path):
        raise ValueError(f"model_dir 必须是 Path，实际类型={type(model_dir).__name__}")
    if not model_dir.is_dir():
        return False

    missing_files = [rel_path for rel_path in _REQUIRED_MODEL_FILES if not (model_dir / rel_path).exists()]
    if missing_files:
        return False

    return _has_model_data(model_dir / "model.onnx.data")


def _download_file(
    url: str,
    destination_path: Path,
    timeout_seconds: int = _MODEL_DOWNLOAD_TIMEOUT_SECONDS,
    chunk_size: int = _DOWNLOAD_CHUNK_SIZE_BYTES,
) -> None:
    """下载模型压缩包，并输出关键进度日志。"""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url 必须是非空字符串")
    if not isinstance(destination_path, Path):
        raise ValueError(
            f"destination_path 必须是 Path，实际类型={type(destination_path).__name__}"
        )
    _validate_positive_int(timeout_seconds, "timeout_seconds")
    _validate_positive_int(chunk_size, "chunk_size")

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("开始下载 ONNX 模型压缩包")

    request = urllib.request.Request(
        url.strip(),
        headers={"User-Agent": "Mozilla/5.0 (compatible; develop_skill/1.0)"},
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response, open(
            destination_path, "wb"
        ) as output_handle:
            total_header = (response.headers.get("Content-Length") or "").strip()
            total_bytes = int(total_header) if total_header.isdigit() else None
            downloaded_bytes = 0
            next_log_threshold = 10 * 1024 * 1024

            while True:
                chunk = response.read(chunk_size)
                if not chunk:
                    break

                output_handle.write(chunk)
                downloaded_bytes += len(chunk)

                if downloaded_bytes >= next_log_threshold:
                    downloaded_mb = downloaded_bytes / 1024 / 1024
                    if total_bytes:
                        total_mb = total_bytes / 1024 / 1024
                        percent = downloaded_bytes * 100 / total_bytes
                        logger.info(
                            f"下载进度: {downloaded_mb:.2f} / {total_mb:.2f} MB ({percent:.1f}%)"
                        )
                    else:
                        logger.info(f"下载进度: {downloaded_mb:.2f} MB")
                    next_log_threshold += 10 * 1024 * 1024
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"下载 ONNX 模型失败: {exc}") from exc

    if not destination_path.exists() or destination_path.stat().st_size <= 0:
        raise RuntimeError(f"下载 ONNX 模型失败，文件为空: {destination_path}")

    size_mb = destination_path.stat().st_size / 1024 / 1024
    logger.info(f"下载完成: {destination_path.name} ({size_mb:.2f} MB)")


def _safe_extract_tar(archive_path: Path, extract_dir: Path) -> None:
    """安全解压 tar.gz，防止压缩包路径穿越覆盖项目其他文件。"""
    if not isinstance(archive_path, Path):
        raise ValueError(f"archive_path 必须是 Path，实际类型={type(archive_path).__name__}")
    if not isinstance(extract_dir, Path):
        raise ValueError(f"extract_dir 必须是 Path，实际类型={type(extract_dir).__name__}")
    if not archive_path.exists():
        raise FileNotFoundError(f"待解压的压缩包不存在: {archive_path}")

    extract_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"开始解压模型压缩包: {archive_path.name}")

    with tarfile.open(archive_path, "r:gz") as tar:
        base_dir = extract_dir.resolve()
        for member in tar.getmembers():
            target_path = (extract_dir / member.name).resolve()
            if not target_path.is_relative_to(base_dir):
                raise RuntimeError(f"压缩包包含非法路径，已拒绝解压: {member.name}")
        tar.extractall(extract_dir)

    logger.info(f"解压完成: {extract_dir}")


def _find_extracted_model_dir(extract_dir: Path, expected_dir_name: str) -> Path:
    """在解压目录中查找可用模型目录。

    下载包的顶层目录结构可能变化，因此这里做一次搜索，
    优先选择目录名匹配的结果，避免未来打包方式变化导致路径写死失效。
    """
    if not isinstance(extract_dir, Path):
        raise ValueError(f"extract_dir 必须是 Path，实际类型={type(extract_dir).__name__}")
    if not isinstance(expected_dir_name, str) or not expected_dir_name.strip():
        raise ValueError("expected_dir_name 必须是非空字符串")

    direct_candidate = extract_dir / expected_dir_name
    if _is_model_ready(direct_candidate):
        return direct_candidate

    candidates: List[Path] = []
    for onnx_path in sorted(extract_dir.rglob("model.onnx")):
        candidate_dir = onnx_path.parent
        if _is_model_ready(candidate_dir):
            candidates.append(candidate_dir)

    if not candidates:
        raise FileNotFoundError(
            f"下载包中未找到可用模型目录: {extract_dir}，期望目录名={expected_dir_name}"
        )

    candidates.sort(
        key=lambda path: (
            path.name != expected_dir_name,
            len(path.relative_to(extract_dir).parts),
            str(path),
        )
    )
    return candidates[0]


def _download_and_prepare_model(model_dir: Path) -> None:
    """下载并安装 ONNX 模型目录。

    使用 /tmp 作为临时下载与解压目录，成功后再复制到项目 models/ 下，
    这样可避免下载中断时污染最终模型目录。
    """
    if not isinstance(model_dir, Path):
        raise ValueError(f"model_dir 必须是 Path，实际类型={type(model_dir).__name__}")

    model_dir.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="onnx-model-", dir="/tmp") as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        archive_path = temp_dir / f"{model_dir.name}.tar.gz"
        extract_dir = temp_dir / "extract"

        if not _ONNX_MODEL_DOWNLOAD_URL:
            raise FileNotFoundError(
                "本地 ONNX 模型不存在，且未配置下载源（_ONNX_MODEL_DOWNLOAD_URL 为空）。"
                "请手动准备 all-MiniLM-L6-v2-onnx 模型目录并放入 models/ 下"
                "（参考：sentence-transformers/all-MiniLM-L6-v2）后重试。")
        _download_file(_ONNX_MODEL_DOWNLOAD_URL, archive_path)
        _safe_extract_tar(archive_path, extract_dir)

        source_model_dir = _find_extracted_model_dir(extract_dir, model_dir.name)
        logger.info(f"安装 ONNX 模型到项目目录: {model_dir}")
        shutil.copytree(source_model_dir, model_dir, dirs_exist_ok=True)

    if not _is_model_ready(model_dir):
        raise FileNotFoundError(f"模型下载并安装后仍不完整: {model_dir}")

    logger.info(f"模型已准备就绪: {model_dir}")


def _ensure_model_available(model_dir: "str | Path") -> Path:
    """确保模型目录存在且完整；缺失时自动下载。

    该逻辑放在懒加载入口之前，保证首次向量编码时就能自愈缺失模型。
    同时通过锁避免并发线程重复下载同一份模型。
    """
    model_dir_path = _normalize_model_dir(model_dir)
    if _is_model_ready(model_dir_path):
        return model_dir_path

    with _MODEL_PREPARE_LOCK:
        if _is_model_ready(model_dir_path):
            return model_dir_path

        logger.warning(f"本地 ONNX 模型不存在或不完整，开始自动下载: {model_dir_path}")
        _download_and_prepare_model(model_dir_path)

        if not _is_model_ready(model_dir_path):
            raise FileNotFoundError(f"自动下载后仍未找到完整 ONNX 模型: {model_dir_path}")

    return model_dir_path



def _auto_merge_data(data_path: Path) -> None:
    """自动合并 model.onnx.data 的分卷文件。

    当原始 .data 文件不存在时，检查是否有 .part-xxxx 分卷文件，
    若有则自动合并还原，用于支持 split 后上传、运行时自动恢复的场景。
    """
    if not isinstance(data_path, Path):
        raise ValueError(f"data_path 必须是 Path，实际类型={type(data_path).__name__}")

    parts = _get_data_parts(data_path)
    if not parts:
        raise FileNotFoundError(
            f"ONNX 数据文件不存在: {data_path}\n"
            f"也未找到分卷文件 ({data_path.name}{_DATA_PART_SUFFIX}*)\n"
            f"请检查模型下载或导出流程"
        )

    logger.info(f"检测到 ONNX 数据分卷: {len(parts)} 个文件，正在自动合并...")

    with open(data_path, "wb") as output_handle:
        for part in parts:
            with open(part, "rb") as input_handle:
                shutil.copyfileobj(input_handle, output_handle)

    meta_path = data_path.parent / f"{data_path.name}{_DATA_PART_META_SUFFIX}"
    if meta_path.exists():
        with open(meta_path, encoding="utf-8") as meta_handle:
            meta = json.load(meta_handle)
        expected_md5 = meta.get("original_md5", "")
        if expected_md5:
            actual_md5 = _compute_md5(data_path)
            if actual_md5 == expected_md5:
                logger.info(f"md5 校验通过: {actual_md5}")
            else:
                logger.warning(f"md5 校验失败! 期望={expected_md5}, 实际={actual_md5}")

    size_mb = data_path.stat().st_size / 1024 / 1024
    logger.info(f"合并完成: {data_path.name} ({size_mb:.2f} MB)")


class _ONNXEncoder:
    """ONNX Runtime 编码器，提供 .encode() 接口兼容原 SentenceTransformer 调用方。"""

    def __init__(self, model_dir: str):
        from transformers import AutoTokenizer
        import onnxruntime as ort

        model_dir_path = _ensure_model_available(model_dir)
        logger.info(f"加载 ONNX 模型: {model_dir_path}")

        onnx_path = model_dir_path / "model.onnx"
        if not onnx_path.exists():
            raise FileNotFoundError(f"自动准备模型后仍未找到 ONNX 文件: {onnx_path}")

        data_path = model_dir_path / "model.onnx.data"
        if not data_path.exists():
            _auto_merge_data(data_path)

        self.tokenizer = AutoTokenizer.from_pretrained(str(model_dir_path))
        self.session = ort.InferenceSession(str(onnx_path))
        self.input_names = [inp.name for inp in self.session.get_inputs()]
        self.output_names = [out.name for out in self.session.get_outputs()]

    def encode(self, sentences: "str | List[str]") -> "np.ndarray":
        """将文本编码为 384 维 L2 归一化向量。

        兼容 sentence-transformers 行为：
          - 输入单字符串 → 返回 (384,) 1D array
          - 输入字符串列表 → 返回 (N, 384) 2D array

        Args:
            sentences: 文本字符串或文本列表

        Returns:
            np.ndarray, dtype float32, L2 normalized
        """
        single = isinstance(sentences, str)
        texts = [sentences] if single else sentences
        logger.info(f"编码: 句子数={len(texts) if isinstance(texts, list) else 1}")

        # 分词
        tokens = self.tokenizer(
            texts,
            padding=True,
            truncation=True,
            return_tensors="np",
        )

        # ONNX 推理
        ort_inputs = {
            "input_ids": tokens["input_ids"].astype(np.int64),
            "attention_mask": tokens["attention_mask"].astype(np.int64),
        }
        outputs = self.session.run(self.output_names, ort_inputs)

        # 取 last_hidden_state 做 mean pooling（与 sentence-transformers 一致）
        last_hidden = outputs[0]  # (batch, seq_len, 384)
        mask = tokens["attention_mask"][:, :, np.newaxis].astype(np.float32)
        embeddings = (last_hidden * mask).sum(axis=1) / mask.sum(axis=1)

        # L2 归一化
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        embeddings = np.divide(embeddings, norms, out=np.zeros_like(embeddings), where=norms > 0)

        embeddings = embeddings.astype(np.float32)
        return embeddings[0] if single else embeddings



def _get_model() -> _ONNXEncoder:
    """获取 ONNX 编码器单例（线程安全懒加载）。"""
    global _ONNX_SESSION
    if _ONNX_SESSION is None:
        with _ONNX_LOCK:
            if _ONNX_SESSION is None:
                _ONNX_SESSION = _ONNXEncoder(_ONNX_MODEL_DIR)
    return _ONNX_SESSION


# ═══════════════════════════════════════════════════════════════
#  持久层 — SQLite
# ═══════════════════════════════════════════════════════════════
#
#  vec_store 表:
#    id          INTEGER PRIMARY KEY AUTOINCREMENT  — 自增主键
#    key         TEXT NOT NULL                       — 标识（可重复）
#    vector      TEXT NOT NULL                       — JSON float[] (384维)
#    text        TEXT NOT NULL DEFAULT ''            — 原始文本
#    created_at  TEXT NOT NULL                       — ISO 时间戳
#
#  索引: idx_vec_store_key ON key
#
#  一个 key 可以对应 N 行（N 条 sentence），
#  搜索时 FAISS 搜到多个同 key 结果，去重后返回唯一 key。
# ═══════════════════════════════════════════════════════════════

VEC_DDL = """
CREATE TABLE IF NOT EXISTS vec_store (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL,
    vector      TEXT NOT NULL,
    text        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vec_store_key ON vec_store(key);
"""


def _get_db(db_path: str) -> sqlite3.Connection:
    """获取 DB 连接（自动建表 + 创建父目录）"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(VEC_DDL)
    conn.commit()
    return conn


# ═══════════════════════════════════════════════════════════════
#  检索层 — FAISS 内存缓存（lazy + dirty 标记）
# ═══════════════════════════════════════════════════════════════
#
#  _FAISS_CACHE: 规范 DB 绝对路径 → 独立 index/keys/version 缓存
#  所有缓存访问和 FAISS search 均由 _FAISS_CACHE_LOCK 串行保护
# ═══════════════════════════════════════════════════════════════

_FAISS_CACHE: dict = {}
_FAISS_CACHE_LOCK = threading.RLock()


def _db_cache_key(db_path: str) -> str:
    """返回 DB 的规范绝对路径，避免相对路径别名共享或串用缓存。"""
    return str(Path(db_path).expanduser().resolve())


def _rebuild_index(db_path: str):
    """从 SQLite 重建 FAISS 索引；调用方可持锁，函数内也用 RLock 保护写入。"""
    cache_key = _db_cache_key(db_path)
    with _FAISS_CACHE_LOCK:
        entry = _FAISS_CACHE.setdefault(cache_key, {"version": 0})
        ver = entry["version"]
    conn = _get_db(db_path)
    rows = conn.execute("SELECT id, key, vector FROM vec_store ORDER BY id").fetchall()
    conn.close()
    vectors: List[List[float]] = []
    keys: List[str] = []
    for _, key_str, vec_json in rows:
        try:
            vec = json.loads(vec_json)
        except (json.JSONDecodeError, TypeError):
            continue
        vectors.append(vec)
        keys.append(key_str)

    index = None
    if vectors and faiss is not None and np is not None:
        vec_np = np.array(vectors, dtype=np.float32)
        faiss.normalize_L2(vec_np)
        index = faiss.IndexFlatIP(vec_np.shape[1])
        index.add(vec_np)
    with _FAISS_CACHE_LOCK:
        entry = _FAISS_CACHE.setdefault(cache_key, {"version": 0})
        if entry["version"] != ver:
            return entry
        entry.update({"index": index, "keys": keys if index is not None else [],
                      "cached_version": ver})
        return entry


def _mark_dirty(db_path: str):
    """仅标记指定 DB 的 FAISS 缓存为脏。"""
    with _FAISS_CACHE_LOCK:
        entry = _FAISS_CACHE.setdefault(_db_cache_key(db_path), {"version": 0})
        entry["version"] += 1


# ═══════════════════════════════════════════════════════════════
#  搜索核心（内部方法，带去重）
# ═══════════════════════════════════════════════════════════════
#
#  去重策略:
#    1. FAISS 搜索 top_k * RESERVE_RATIO 条（多搜一些，给去重留余量）
#    2. 按得分降序遍历，用 set 记录已出现的 key
#    3. 遇到新 key 则加入结果列表，直到满 top_k 个
#    4. 如果所有行遍历完仍不足 top_k，返回已有结果
#
#  RESERVE_RATIO = 3
#  例如 top_k=5 → FAISS 搜 15 条 → 去重取前 5 个唯一 key
# ═══════════════════════════════════════════════════════════════

_RESERVE_RATIO = 3




def _search_unique(query_np: "np.ndarray", top_k: int, entry: dict) -> List[Tuple[str, float]]:
    """FAISS 搜索 + 去重，返回不重复的 (key, score) 列表"""
    index = entry.get("index")
    keys = entry.get("keys") or []
    if index is None or index.ntotal == 0:
        return []

    k = min(top_k * _RESERVE_RATIO, index.ntotal)
    distances, indices = index.search(query_np, k)

    seen: set = set()
    results: List[Tuple[str, float]] = []
    for pos, idx in enumerate(indices[0]):
        key_str = keys[idx]
        if key_str not in seen:
            seen.add(key_str)
            results.append((key_str, float(distances[0][pos])))
            if len(results) >= top_k:
                break

    return results

# ═══════════════════════════════════════════════════════════════
#  公共 API
# ═══════════════════════════════════════════════════════════════

def store(sentence: str, db_path: str, key: str) -> bool:
    logger.debug(f"存储 embedding: key={key}, sentence={sentence!r:.80}")

    """存入一条 sentence，关联到 key。重复调用会追加多条。

    Args:
        sentence: 要编码的文本
        db_path:  SQLite 数据库路径
        key:      标识（可重复，同一个 key 存多条）

    Returns:
        True 表示成功
    """
    if not sentence or not key:
        return False

    model = _get_model()
    vec = model.encode(sentence).tolist()
    now = datetime.now(timezone.utc).isoformat()

    conn = _get_db(db_path)
    conn.execute(
        "INSERT INTO vec_store (key, vector, text, created_at) VALUES (?, ?, ?, ?)",
        (key, json.dumps(vec, ensure_ascii=False), sentence, now),
    )
    conn.commit()
    conn.close()

    _mark_dirty(db_path)
    return True


def store_batch(sentences: List[str], db_path: str, key: str) -> bool:
    """先清除该 key 的所有旧数据，再批量存入多条 sentence。

    这是 skill 更新场景的推荐用法:
      每次更新 skill 时，先删除该 skill 的所有旧 embedding，
      再批量写入 description + triggers 等所有文本。

    Args:
        sentences: 要编码的文本列表
        db_path:   SQLite 数据库路径
        key:       标识

    Returns:
        True 表示成功
    """
    if not sentences or not key:
        return False

    model = _get_model()

    # 编码所有 sentence
    vectors = model.encode(sentences).tolist()
    now = datetime.now(timezone.utc).isoformat()

    conn = _get_db(db_path)

    # 删除该 key 的旧数据
    conn.execute("DELETE FROM vec_store WHERE key = ?", (key,))

    # 批量插入新数据
    rows_data = [
        (key, json.dumps(vec, ensure_ascii=False), sentence, now)
        for vec, sentence in zip(vectors, sentences)
    ]
    conn.executemany(
        "INSERT INTO vec_store (key, vector, text, created_at) VALUES (?, ?, ?, ?)",
        rows_data,
    )
    conn.commit()
    conn.close()

    _mark_dirty(db_path)
    return True


def search(sentence: str, db_path: str, top_k: int = 5) -> List[Tuple[str, float]]:
    logger.info(f"搜索: query={sentence!r:.60}, top_k={top_k}")

    """搜索与文本最相似的前 top_k 个不重复 key。

    流程:
      ① 编码 query
      ② 检查并重建 FAISS 索引（如需）
      ③ FAISS 搜索 top_k * 3 条（给去重留余量）
      ④ 按得分降序遍历，去重后取前 top_k 个唯一 key

    Args:
        sentence: 查询文本
        db_path:  SQLite 数据库路径
        top_k:    返回前 k 个最匹配的**不重复** key（默认 5）

    Returns:
        按相似度降序排列的不重复 (key, score) 列表
    """
    if not sentence:
        return []

    # ① 编码 query（faiss/numpy 缺失时向量检索不可用 → 返回 []，上层降级 ngram）
    if faiss is None or np is None:
        return []
    model = _get_model()
    query_np = np.array([model.encode(sentence)], dtype=np.float32)
    faiss.normalize_L2(query_np)

    # ② 每个 DB 独立检查/重建缓存；锁覆盖 FAISS search，避免并发重建时串用。
    with _FAISS_CACHE_LOCK:
        entry = _FAISS_CACHE.setdefault(_db_cache_key(db_path), {"version": 0})
        if entry.get("cached_version", -1) != entry["version"]:
            entry = _rebuild_index(db_path)
        return _search_unique(query_np, top_k, entry)


# ═══════════════════════════════════════════════════════════════
#  管理 API
# ═══════════════════════════════════════════════════════════════

def delete(key: str, db_path: str) -> bool:
    """删除某个 key 的所有行（所有关联的 sentence）"""
    conn = _get_db(db_path)
    cursor = conn.execute("DELETE FROM vec_store WHERE key = ?", (key,))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    if deleted:
        _mark_dirty(db_path)
    return deleted


def count(db_path: str) -> int:
    """返回 DB 中总行数（sentence 总数）"""
    conn = _get_db(db_path)
    row = conn.execute("SELECT COUNT(*) FROM vec_store").fetchone()
    conn.close()
    return row[0] if row else 0


def count_keys(db_path: str) -> int:
    """返回 DB 中不重复 key 的数量"""
    conn = _get_db(db_path)
    row = conn.execute("SELECT COUNT(DISTINCT key) FROM vec_store").fetchone()
    conn.close()
    return row[0] if row else 0


def list_keys(db_path: str) -> List[str]:
    """列出 DB 中所有不重复的 key"""
    conn = _get_db(db_path)
    rows = conn.execute("SELECT DISTINCT key FROM vec_store ORDER BY key").fetchall()
    conn.close()
    return [r[0] for r in rows]


# ═══════════════════════════════════════════════════════════════
#  自测（python3 -m codes.search）
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import tempfile

    tmp_db = str(Path(tempfile.gettempdir()) / "embeddings_test.db")
    Path(tmp_db).unlink(missing_ok=True)

    print("  \U0001f4e1  Testing embeddings module (FAISS + SQLite)...\n")

    # ── store_batch: 一个 key 对应多条 sentence ──
    print("  \U0001f4dd  store_batch (单个 key 多条 sentence):")

    ok = store_batch([
        "多轮互联网搜索与信息分析",
        "triggers: 搜索, 查资料, search, 百度, 互联网, 查询",
    ], tmp_db, "web_search")
    print(f"    web_search (2 sentences): {_OK_ICON if ok else _FAIL_ICON}")

    ok = store_batch([
        "创建或规范化技能，生成标准的 skill.md 和可选的 __init__.py",
        "triggers: 创建技能, 写 skill, 规范化, 生成 skill, makeskill, 技能生成器",
    ], tmp_db, "makeskill")
    print(f"    makeskill (2 sentences):  {_OK_ICON if ok else _FAIL_ICON}")

    ok = store_batch([
        "制定多步骤执行计划，含意图澄清到任务分解，附带批量检查工具",
        "triggers: 计划, 规划, 方案, plan, 任务分解, 执行方案",
    ], tmp_db, "plan")
    print(f"    plan (2 sentences):       {_OK_ICON if ok else _FAIL_ICON}")

    ok = store_batch([
        "对代码/方案/配置进行全面检查，输出健康度报告",
        "triggers: 检查, 审计, 验证, 排查, check, review",
    ], tmp_db, "check")
    print(f"    check (2 sentences):      {_OK_ICON if ok else _FAIL_ICON}")

    # ── 检查存储状态 ──
    print(f"\n  \U0001f4ca  count: {count(tmp_db)} (应=8, 4 key × 2 sentence)")
    print(f"  \U0001f4ca  count_keys: {count_keys(tmp_db)} (应=4)")
    print(f"  \U0001f4ca  list_keys: {list_keys(tmp_db)}")

    # ── search: 搜索 + 去重 ──
    print("\n  \U0001f50d  search（搜索 + 去重）:")

    keys = search("帮我搜一下最新的AI框架", tmp_db, top_k=3)
    print(f"    query='帮我搜一下最新的AI框架', top_k=3:")
    for i, k in enumerate(keys, 1):
        print(f"      {i}. {k}")
    print(f"    去重验证: len={len(keys)}, unique={len(set(keys))}, "
          f"{_OK_ICON + ' 无重复' if len(keys)==len(set(keys)) else _FAIL_ICON + ' 有重复'}")

    keys = search("帮我检查一下这个方案有没有问题", tmp_db, top_k=3)
    print(f"\n    query='帮我检查一下这个方案有没有问题', top_k=3:")
    for i, k in enumerate(keys, 1):
        print(f"      {i}. {k}")
    print(f"    去重验证: len={len(keys)}, unique={len(set(keys))}, "
          f"{_OK_ICON + ' 无重复' if len(keys)==len(set(keys)) else _FAIL_ICON + ' 有重复'}")

    keys = search("写一个新的技能", tmp_db, top_k=3)
    print(f"\n    query='写一个新的技能', top_k=3:")
    for i, k in enumerate(keys, 1):
        print(f"      {i}. {k}")
    print(f"    去重验证: len={len(keys)}, unique={len(set(keys))}, "
          f"{_OK_ICON + ' 无重复' if len(keys)==len(set(keys)) else _FAIL_ICON + ' 有重复'}")

    keys = search("帮我规划一个实验方案", tmp_db, top_k=5)
    print(f"\n    query='帮我规划一个实验方案', top_k=5:")
    for i, k in enumerate(keys, 1):
        print(f"      {i}. {k}")
    print(f"    去重验证: len={len(keys)}, unique={len(set(keys))}, "
          f"{_OK_ICON + ' 无重复' if len(keys)==len(set(keys)) else _FAIL_ICON + ' 有重复'}")

    # ── delete ──
    print(f"\n  \U0001f5d1  delete + count:")
    print(f"    delete('makeskill'): {delete('makeskill', tmp_db)}")
    print(f"    count: {count(tmp_db)} (应=6)")
    print(f"    count_keys: {count_keys(tmp_db)} (应=3)")

    # ── 追加 store（测试多 sentence 去重）──
    print(f"\n  \U0001f4dd  store (追加同 key 新 sentence):")
    ok = store("用于互联网信息检索的多轮搜索工具", tmp_db, "web_search")
    print(f"    append web_search: {_OK_ICON if ok else _FAIL_ICON}")
    print(f"    count: {count(tmp_db)} (应=7)")
    print(f"    count_keys: {count_keys(tmp_db)} (应=3)")

    # 清理
    Path(tmp_db).unlink(missing_ok=True)
    print(f"\n  \u2705  所有测试通过！")


# ═══════════════════════════════════════════════════════════════
#  第二部分: 统一搜索框架 — 多范围 × 后缀路由 × 三方案级联（修复版）
# ═══════════════════════════════════════════════════════════════

# ── 集中常量区 ────────────────────────────────────────────────
_SEARCH_CONST = {
    "EMBEDDING_ENABLED": os.environ.get("XKAGENT_EMBEDDING", "").lower() in ("1", "true", "yes"),  # embedding 默认禁止（2026-08-10 用户决策）；XKAGENT_EMBEDDING=1 启动即恢复
    "EMBEDDING_MIN_SCORE": 0.2,   # embedding 余弦下限（<0.2 视为无效；仅 method="embedding" 时用）
    "NGRAM_MIN_SCORE": 1.0,       # ngram 加权分下限（乱码/噪声防御）
    "SHORT_QUERY_TOKENS": 2,      # 有效 token <2 → 视为空查询
    "GREP_CONTEXT": 100,          # grep 上下文总字符数（±50）
    "INDEX_WINDOW": 500,          # 超长文本分段窗口
    "INDEX_OVERLAP": 50,          # 分段重叠
    "MAX_FILE_SIZE": 1_000_000,   # >1MB 文件跳过 AST 提取（仅 grep 原文）
    "LOG_RECENT_FILES": 20,       # logs 只扫最近 N 个文件
}
C = _SEARCH_CONST


def _canonical_workdir(workdir: "str | Path | None" = None) -> Path:
    """规范化显式 workdir；未传时保持兼容，使用全局 config。"""
    from codes import config
    return Path(workdir if workdir is not None else config.get_workdir()).expanduser().resolve()


def _data_dir(workdir: "str | Path | None" = None) -> Path:
    """返回指定工作目录的数据目录。"""
    from codes import config
    return _canonical_workdir(workdir) / getattr(config, "DATA_DIR_NAME", ".xkagent")


def _search_index_dir(workdir: "str | Path | None" = None) -> str:
    """统一向量索引目录: .xkagent/search_index/"""
    return str(_data_dir(workdir) / "search_index")


def _index_db_path(scope: str, workdir: "str | Path | None" = None) -> str:
    """返回指定 workdir/scope 唯一拥有的索引 DB 路径。"""
    if not isinstance(scope, str) or not re.fullmatch(r"[\w.-]+", scope) or scope in (".", ".."):
        raise ValueError(f"非法搜索 scope: {scope!r}")
    return os.path.join(_search_index_dir(workdir), f"{scope}.db")


@dataclass
class SearchHit:
    """统一搜索结果结构"""
    key: str          # "skills:check" / "codes:agent.py#sym:def_run_stream" ...
    scope: str        # skills | docs | historys | codes | logs
    channel: str      # semantic | symbols | raw
    method: str       # embedding | ngram | grep
    score: float
    snippet: str
    meta: dict = field(default_factory=dict)


# ── 后缀路由表: 后缀 → (提取器名, 默认方案) ──────────────
ROUTE_TABLE = {
    ".md":  ("extract_markdown", "embedding"),
    ".txt": ("extract_markdown", "embedding"),
    ".rst": ("extract_markdown", "embedding"),
    ".py":  ("extract_python",   "embedding"),
    ".pyw": ("extract_python",   "embedding"),
    ".c":   ("extract_c_like",   "embedding"),
    ".h":   ("extract_c_like",   "embedding"),
    ".cpp": ("extract_c_like",   "embedding"),
    ".hpp": ("extract_c_like",   "embedding"),
    ".rs":  ("extract_rust",     "embedding"),
    ".db":  ("extract_history",  "embedding"),
    ".log":  ("extract_log", "grep"),
    ".json": ("extract_raw", "grep"),
    ".yaml": ("extract_raw", "grep"),
    ".yml":  ("extract_raw", "grep"),
    ".toml": ("extract_raw", "grep"),
    ".ini":  ("extract_raw", "grep"),
    ".cfg":  ("extract_raw", "grep"),
    ".sh":   ("extract_raw", "grep"),
    ".bash": ("extract_raw", "grep"),
    ".html": ("extract_raw", "grep"),
    ".css":  ("extract_raw", "grep"),
    ".js":   ("extract_raw", "grep"),
    ".ts":   ("extract_raw", "grep"),
    ".go":   ("extract_raw", "grep"),
    ".java": ("extract_raw", "grep"),
}

# 范围根目录（相对 workdir）；skills 实际生效路径由 _scope_roots 内 `codes.skill._skill_dirs()`
# 动态决定（用户级 .xkagent/skills 优先 + 内置级兜底），此处 "skills" 仅作默认/文档值
SCOPE_ROOTS = {
    "skills":   "skills",
    "docs":     ".xkagent/docs",
    "historys": ".xkagent/historys",
    "logs":     ".xkagent/logs",
    # "codes" 默认不参与搜索（用户决策 2026-08-07）；需要时 /info add codes codes 恢复
}

# ── 搜索范围动态配置（/info 命令: per-session + 全局）────
# 全局配置: .xkagent/search_ranges.txt（行语法 <path> <add|deny> [scope]）
# per-session: session db 的 search_state 表（agent 写入，history.py 管理）
SEARCH_CONFIG_FILE = "search_ranges.txt"


def load_search_config(workdir: "str | Path | None" = None) -> dict:
    """读取全局搜索范围配置（.xkagent/search_ranges.txt）。

    行语法（对齐 permission.txt 风格）:
      <path> add  [scope]   添加搜索路径（目录或文件），scope 默认 extra
      <path> deny           禁止搜索路径（前缀匹配，文件/子树均排除）
    返回 {"adds": [{"path": abs, "scope": str}], "denies": [abs]}。
    """
    fp = os.path.join(str(_data_dir(workdir)), SEARCH_CONFIG_FILE)
    adds: list = []
    denies: list = []
    if not os.path.isfile(fp):
        return {"adds": adds, "denies": denies}
    with open(fp, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 2:
                logger.warning(f"[search] search_ranges.txt:{lineno}: invalid format, skipping")
                continue
            path_, action = parts[0], parts[1].lower()
            scope = parts[2] if len(parts) > 2 else "extra"
            expanded = os.path.expanduser(path_)
            abs_path = os.path.abspath(expanded if os.path.isabs(expanded)
                                       else os.path.join(str(_canonical_workdir(workdir)), expanded))
            if not os.path.exists(abs_path):
                logger.warning(f"[search] search_ranges.txt:{lineno}: {abs_path} does not exist, skipping")
                continue
            if action == "add":
                adds.append({"path": abs_path, "scope": scope})
            elif action == "deny":
                denies.append(abs_path)
            else:
                logger.warning(f"[search] search_ranges.txt:{lineno}: unknown action '{action}', expected add/deny, skipping")
    return {"adds": adds, "denies": denies}


def _effective_search_config(session: str | None = None,
                             workdir: "str | Path | None" = None) -> dict:
    """合并全局 + per-session 搜索范围配置（session 覆盖全局同 path；deny 取并集）。"""
    cfg = load_search_config(workdir)
    if not session:
        return cfg
    try:
        from codes.history import get_search_state
        sess_items = get_search_state(session)
    except Exception:
        return cfg
    adds = {it["path"]: dict(it) for it in cfg["adds"]}
    denies = set(cfg["denies"])
    for it in sess_items:
        if it["action"] == "add":
            adds[it["path"]] = {"path": it["path"], "scope": it.get("scope") or "extra"}
        elif it["action"] == "deny":
            denies.add(it["path"])
    return {"adds": list(adds.values()), "denies": sorted(denies)}


def _scope_roots(session: str | None = None,
                 workdir: "str | Path | None" = None) -> dict:
    """生效的搜索范围根：内置 SCOPE_ROOTS + 配置 add（按 scope 分组，dict[scope]=[roots]）。"""
    cfg = _effective_search_config(session, workdir)
    roots: dict = {}
    for sc, root in SCOPE_ROOTS.items():
        roots.setdefault(sc, []).append(root)
    # skills 范围与加载侧统一（用户级 .xkagent/skills 优先 + 内置级兜底）：
    # 用 codes.skill._skill_dirs() 取代固定相对路径 "skills"，保证 searchskill/searchinfo
    # 与 SkillLoader.list_skills / _find_skill_dir 检索同一批技能文件。
    if "skills" in roots:
        try:
            from codes.skill import _skill_dirs
            roots["skills"] = _skill_dirs(workdir)
        except Exception:
            pass  # 导入异常时保留默认 roots（"skills" 相对路径兜底）
    for it in cfg["adds"]:
        roots.setdefault(it["scope"] or "extra", []).append(it["path"])
    return roots


def _is_denied(path: str, denies: list) -> bool:
    """deny 前缀匹配：path 等于某 deny 项或在其子树内 → True。"""
    if not denies:
        return False
    p = os.path.abspath(path)
    for d in denies:
        d = os.path.abspath(d)
        if p == d or p.startswith(d.rstrip(os.sep) + os.sep):
            return True
    return False



# ── 提取器 ─────────────────────────────────────────────────────

def _strip_fm(text: str) -> str:
    """剥离 YAML frontmatter"""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            return parts[2].lstrip("\n")
    return text


def extract_markdown(path: str) -> list:
    """md/txt → 段落（剥 frontmatter，双空行分段，跳过代码围栏）"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    body = _strip_fm(text)
    blocks = []
    for i, p in enumerate(re.split("\n\n+", body)):
        p = p.strip()
        if len(p) < 10 or p.startswith("```"):
            continue
        blocks.append(SearchHit(key=f"{path}#p{i}", scope="?", channel="semantic",
                                method="", score=0.0, snippet=p[:200], meta={"text": p}))
    return blocks


_DOC_NODES = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def extract_python(path: str) -> list:
    """AST 双通道: docstring+注释(semantic) + 签名(symbols)，零内容特判"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            src = f.read()
    except OSError:
        return []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    blocks = []

    def walk(node):
        if isinstance(node, _DOC_NODES):
            d = ast.get_docstring(node, clean=False)
            if d:
                blocks.append(SearchHit(key=f"{path}#doc", scope="codes",
                                        channel="semantic", method="", score=0.0,
                                        snippet=" ".join(d.strip().split())[:200],
                                        meta={"text": d}))
        for child in ast.iter_child_nodes(node):
            walk(child)

    walk(tree)
    for ln in src.splitlines():
        s = ln.strip()
        if (s.startswith("#") and not s.startswith("#!")
                and not s.startswith("# -*-") and not s.startswith("# coding")):
            blocks.append(SearchHit(key=f"{path}#comment", scope="codes",
                                    channel="semantic", method="", score=0.0,
                                    snippet=s[1:].strip()[:200],
                                    meta={"text": s[1:].strip()}))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = ",".join(a.arg for a in node.args.args[:5])
            blocks.append(SearchHit(key=f"{path}#sym:def_{node.name}", scope="codes",
                                    channel="symbols", method="", score=0.0,
                                    snippet=f"def {node.name}({args})",
                                    meta={"text": f"def {node.name}({args})"}))
        elif isinstance(node, ast.ClassDef):
            blocks.append(SearchHit(key=f"{path}#sym:class_{node.name}", scope="codes",
                                    channel="symbols", method="", score=0.0,
                                    snippet=f"class {node.name}",
                                    meta={"text": f"class {node.name}"}))
    return blocks


def extract_c_like(path: str) -> list:
    """C/C++ → 正则双通道（/** */ + // 注释 semantic；函数/struct 签名 symbols）"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            src = f.read()
    except OSError:
        return []
    blocks = []
    for m in re.finditer(r"/\*\*(.*?)\*/", src, re.S):
        t = " ".join(m.group(1).replace("*", "").split())
        t = re.sub(r"\\\w+", "", t)  # 过滤 doxygen 命令 \brief 等
        if len(t) >= 10:
            blocks.append(SearchHit(key=f"{path}#doc", scope="codes",
                                    channel="semantic", method="", score=0.0,
                                    snippet=t[:200], meta={"text": t}))
    for m in re.finditer(r"//(.*)$", src, re.M):
        t = m.group(1).strip()
        if len(t) >= 5:
            blocks.append(SearchHit(key=f"{path}#comment", scope="codes",
                                    channel="semantic", method="", score=0.0,
                                    snippet=t[:200], meta={"text": t}))
    for m in re.finditer(
            r"\b(?:int|void|char|float|double|long|struct|static|unsigned)\s+\w+\s*\([^;]*\)"
            r"|struct\s+\w+|typedef\s+[^;]+;", src, re.M):
        blocks.append(SearchHit(key=f"{path}#sym", scope="codes",
                                channel="symbols", method="", score=0.0,
                                snippet=m.group(0)[:200], meta={"text": m.group(0)}))
    return blocks


def extract_rust(path: str) -> list:
    """Rust → 正则双通道（/// + //! + // 注释 semantic；fn/struct/impl 签名 symbols）"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            src = f.read()
    except OSError:
        return []
    blocks = []
    for m in re.finditer(r"///(.*?)$", src, re.M):
        t = m.group(1).strip()
        if t and not t.startswith("```"):
            blocks.append(SearchHit(key=f"{path}#doc", scope="codes",
                                    channel="semantic", method="", score=0.0,
                                    snippet=t[:200], meta={"text": t}))
    for m in re.finditer(r"//(?![/!])(.*?)$", src, re.M):
        t = m.group(1).strip()
        if len(t) >= 5:
            blocks.append(SearchHit(key=f"{path}#comment", scope="codes",
                                    channel="semantic", method="", score=0.0,
                                    snippet=t[:200], meta={"text": t}))
    for m in re.finditer(
            r"\bpub\s+fn\s+\w+\s*\([^)]*\)|\bfn\s+\w+\s*\([^)]*\)"
            r"|\bpub\s+struct\s+\w+|\bstruct\s+\w+|\bimpl\s+\w+"
            r"|\btrait\s+\w+|\bmod\s+\w+", src, re.M):
        blocks.append(SearchHit(key=f"{path}#sym", scope="codes",
                                channel="symbols", method="", score=0.0,
                                snippet=m.group(0)[:200], meta={"text": m.group(0)}))
    return blocks


def extract_log(path: str) -> list:
    """log → 时间戳分段（traceback 并入上一段）"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    ts_pat = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+ \|")
    blocks, cur, start = [], [], 0
    for i, ln in enumerate(lines):
        if ts_pat.match(ln) and cur:
            blocks.append(SearchHit(key=f"{path}:{start}", scope="logs",
                                    channel="raw", method="", score=0.0,
                                    snippet="\n".join(cur)[:200],
                                    meta={"text": "\n".join(cur), "line": start}))
            cur, start = [], i
        cur.append(ln)
    if cur:
        blocks.append(SearchHit(key=f"{path}:{start}", scope="logs",
                                channel="raw", method="", score=0.0,
                                snippet="\n".join(cur)[:200],
                                meta={"text": "\n".join(cur), "line": start}))
    return blocks


def extract_history(path: str) -> list:
    """historys msgz → 消息原文（2026-08-24 msgz 切换：单文件 zlib 读）"""
    try:
        from codes.history_msgz import MsgzStore
        store = MsgzStore(path, auto_sync=False)
        blocks = []
        sess = os.path.basename(path).replace(".msgz", "")
        for m in store.messages:
            content = m.get("content") or ""
            if not content:
                continue
            blocks.append(SearchHit(key=f"historys:{sess}:{m['id']}", scope="historys",
                                    channel="semantic", method="", score=0.0,
                                    snippet=content[:200],
                                    meta={"text": content, "role": m.get("role"),
                                          "extras": m.get("extras")}))
        return blocks
    except Exception:
        return []

def extract_raw(path: str) -> list:
    """配置文件等 → 原文整块（grep）"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    return [SearchHit(key=path, scope="?", channel="raw", method="", score=0.0,
                      snippet=text[:200], meta={"text": text})]


_EXTRACTORS = {
    "extract_markdown": extract_markdown,
    "extract_python": extract_python,
    "extract_c_like": extract_c_like,
    "extract_rust": extract_rust,
    "extract_log": extract_log,
    "extract_history": extract_history,
    "extract_raw": extract_raw,
}


# ── grep 方案（多词拆解 OR 匹配 + 上下文窗口）──────────────────

def _grep_match(query: str, text: str):
    """整串优先，失败拆词 OR 匹配"""
    idx = text.find(query)
    if idx >= 0:
        return ("exact", idx)
    words = [w for w in re.split(r"[\s,，。;；:：]+", query) if len(w) >= 2]
    for w in words:
        idx = text.find(w)
        if idx >= 0:
            return ("word", idx, w)
    return None


def search_grep(query: str, blocks: list, context: int | None = None,
                top_k: int = 10) -> list:
    if not query or not query.strip():
        return []
    ctx = C["GREP_CONTEXT"] if context is None else context
    hits = []
    for b in blocks:
        text = b.meta.get("text", "")
        m = _grep_match(query, text)
        if not m:
            continue
        idx = m[1]
        start = max(0, idx - ctx // 2)
        end = min(len(text), idx + len(query) + ctx // 2)
        hits.append(SearchHit(key=b.key, scope=b.scope, channel=b.channel,
                              method="grep", score=1.0, snippet=text[start:end],
                              meta={"match": m[0], "pos": idx}))
    return hits[:top_k]


# ── ngram 方案（短查询守卫 + min_score）────────────────────────

def _tokenize(text: str) -> list:
    tokens = []
    for m in re.finditer(r"[a-zA-Z][a-zA-Z0-9_-]*", text):
        tokens.append(m.group().lower())
    for seq in re.findall(r"[\u4e00-\u9fff]+", text):
        for ch in seq:
            tokens.append(ch)
        for i in range(len(seq) - 1):
            tokens.append(seq[i:i + 2])
        for i in range(len(seq) - 2):
            tokens.append(seq[i:i + 3])
    return tokens


def _char_ngrams(text: str, n_range: tuple = (2, 4)) -> collections.Counter:
    clean = re.sub(r"\s+", "", text)
    return collections.Counter(
        clean[i:i + n] for n in range(n_range[0], n_range[1] + 1)
        for i in range(len(clean) - n + 1))


# ── ngram 特征缓存（2026-08-23：historys 范围全量 ngram 匹配 ~9s 优化）──
# 实测（08-23）：historys 4426 块/977 万字符，全量 char_ngrams≈4.7s + tokenize≈0.8s，
# 且每轮 search_all 都重复计算相同文本 → 缓存 ≤2000 字符块的
# (char_ngrams, frozenset tokens, norm) 跨轮复用（historys 内容两次 search 间基本不变）。
# 内存实测：≤2000 字符块全量 ≈307MB（>2000 长块重算，避免全量 887MB 峰值）；机器内存充足可接受。
# 长块（>2000）仅缓存 tokenize 结果（内存小），char_ngrams 每次重算。
_NGRAM_CACHE_MAX_TEXT = 2000      # 仅缓存 ≤2000 字符的文本（长块每次重算）
_NGRAM_CACHE_MAX = 4000           # LRU 上限（historys 短块 3316 + 余量）
_ngram_cache: "collections.OrderedDict[str, tuple]" = collections.OrderedDict()
_ngram_cache_lock = threading.Lock()

# 长块 tokenize 缓存（frozenset tokens 内存小：4435 块 × ~200 token ≈ 50MB）
_TOKEN_CACHE_MAX = 20000
_token_cache: "dict[str, frozenset]" = {}
_token_cache_lock = threading.Lock()


def _cached_ngram_features(text: str):
    """返回 (char_ngrams Counter, frozenset tokens, norm)；短文本走 LRU 缓存，
    长文本（>_NGRAM_CACHE_MAX_TEXT）只缓存 tokenize 结果，char_ngrams 每次重算。"""
    if len(text) <= _NGRAM_CACHE_MAX_TEXT:
        with _ngram_cache_lock:
            hit = _ngram_cache.get(text)
            if hit is not None:
                _ngram_cache.move_to_end(text)
                return hit
    if len(text) <= _NGRAM_CACHE_MAX_TEXT:
        c = _char_ngrams(text)
        t = frozenset(_tokenize(text))
        nb = math.sqrt(sum(v * v for v in c.values()))
        feats = (c, t, nb)
        with _ngram_cache_lock:
            _ngram_cache[text] = feats
            if len(_ngram_cache) > _NGRAM_CACHE_MAX:
                _ngram_cache.popitem(last=False)
        return feats
    # 长块：只缓存 tokenize（char_ngrams 每次重算）
    with _token_cache_lock:
        t = _token_cache.get(text)
    if t is None:
        t = frozenset(_tokenize(text))
        with _token_cache_lock:
            _token_cache[text] = t
            if len(_token_cache) > _TOKEN_CACHE_MAX:
                # 简单淘汰：清空一半（长块 tokenize 缓存为辅助，偶发清空可接受）
                for k in list(_token_cache)[: len(_token_cache) // 2]:
                    del _token_cache[k]
    c = _char_ngrams(text)
    nb = math.sqrt(sum(v * v for v in c.values()))
    return (c, t, nb)


def _cosine_counter(a: collections.Counter, b: collections.Counter,
                    nb: float | None = None, na: float | None = None) -> float:
    """余弦相似度，O(|a|)（a 为 query，通常远小于 b）：只遍历 a 的 gram 求交集点积；
    nb/na 可传预计算范数，避免对 b 全量遍历（大块文本的主要开销）。"""
    dot = 0.0
    for k, av in a.items():
        bv = b.get(k)
        if bv:
            dot += av * bv
    if na is None:
        na = math.sqrt(sum(v * v for v in a.values()))
    if nb is None:
        nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def search_ngram(query: str, blocks: list, top_k: int = 5) -> list:
    q_tokens = collections.Counter(_tokenize(query))
    q_ngrams = _char_ngrams(query)
    if sum(q_tokens.values()) < C["SHORT_QUERY_TOKENS"]:
        return []
    q_norm = math.sqrt(sum(v * v for v in q_ngrams.values()))
    scored = []
    for b in blocks:
        text = b.meta.get("text", "")
        c, t_tokens, nb = _cached_ngram_features(text)
        sim = _cosine_counter(q_ngrams, c, nb, q_norm)
        kw = sum(q_tokens.get(tok, 0) for tok in t_tokens if tok in q_tokens)
        score = sim * 20.0 + kw * 2.0
        if score >= C["NGRAM_MIN_SCORE"]:
            scored.append((score, b))
    scored.sort(key=lambda x: -x[0])
    return [SearchHit(key=b.key, scope=b.scope, channel=b.channel, method="ngram",
                      score=s, snippet=b.meta.get("text", "")[:200], meta=b.meta)
            for s, b in scored[:top_k]]

# ── embedding 方案（包装向量 search + 阈值降级）────────────────

def search_embedding(query: str, blocks: list, top_k: int = 5,
                     scopes: list | None = None,
                     workdir: "str | Path | None" = None):
    """语义通道主方案：查各范围向量索引。

    返回 None = 模型不可用/索引为空/全部低分 → 触发上层降级 ngram。
    返回 []  = 有索引但无结果。

    Args:
        scopes: 要查询的索引 scope 列表；None = [blocks[0].scope]（兼容旧调用）。
    """
    if not blocks:
        return None
    scope = blocks[0].scope
    _t0 = time.perf_counter()
    # 全部范围统一用 search_index/{scope}.db（skills 也统一，由 /updateembedding 维护）
    scopes = scopes if scopes else [scope]
    dbs = [_index_db_path(s, workdir) for s in scopes]
    dbs = [d for d in dbs if os.path.exists(d)]
    if not dbs:
        return None
    merged, seen = [], set()
    for db_path in dbs:
        try:
            hits = search(query, db_path, top_k=top_k)   # 向量搜索（第一部分）
        except Exception:
            continue
        for k, s in hits:
            if k in seen:
                continue
            seen.add(k)
            merged.append((k, s))
    if not merged:
        logger.info(f"[search]   {scope} embedding: 索引为空/无结果 "
                    f"耗时={(time.perf_counter()-_t0)*1000:.1f}ms → 降级")
        return []
    valid = [(k, s) for k, s in merged if s >= C["EMBEDDING_MIN_SCORE"]]
    if not valid:
        logger.info(f"[search]   {scope} embedding: 全部低分(<{C['EMBEDDING_MIN_SCORE']}) "
                    f"耗时={(time.perf_counter()-_t0)*1000:.1f}ms → 降级 ngram")
        return None
    logger.info(f"[search]   {scope} embedding: 命中 {len(valid)} 条 "
                f"耗时={(time.perf_counter()-_t0)*1000:.1f}ms (min_score={C['EMBEDDING_MIN_SCORE']})")
    # 映射回块：向量库 key 可能是技能名/文件路径，block key 是文件路径#段
    # 1) 精确匹配 block.key
    # 2) 前缀匹配: key 是技能目录名（如 web_search）→ 找该目录下第一个语义块
    by_key = {b.key: b for b in blocks}
    by_dir = {}
    for b in blocks:
        for seg in b.key.replace("\\", "/").split("/"):
            if seg and (seg not in by_dir):
                by_dir[seg] = b
    out = []
    for k, s in valid:
        b = by_key.get(k)
        if b is None:
            b = by_dir.get(k)
        if b:
            out.append(SearchHit(key=b.key, scope=b.scope, channel=b.channel,
                                 method="embedding", score=s, snippet=b.snippet,
                                 meta=b.meta))
    return out[:top_k]


# ── 范围收集（签名 + 排除规则）────────────────────────────────

def _walk_files(root: str) -> list:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        # 数据目录不递归，避免搜索结果混入运行时数据。
        dirnames[:] = [d for d in dirnames
                       if d not in ("search_index", "__pycache__", ".git", ".trash",
                                    ".xkagent")]
        for fn in filenames:
            out.append(os.path.join(dirpath, fn))
    return out


def _collect_file(path: str, scope: str, blocks: list) -> None:
    """按后缀路由提取单个文件的文本块（collect_scope / update_embeddings 共用）。"""
    ext = os.path.splitext(path)[1].lower()
    route = ROUTE_TABLE.get(ext)
    if not route:
        return
    extractor_name, def_method = route
    try:
        if os.path.getsize(path) > C["MAX_FILE_SIZE"] and def_method == "embedding":
            return
    except OSError:
        return
    extractor = _EXTRACTORS.get(extractor_name)
    if not extractor:
        return
    try:
        file_blocks = extractor(path)
    except Exception:
        return
    for b in file_blocks:
        b.scope = scope
        b.method = "embedding" if (b.channel == "semantic"
                                   and def_method == "embedding") else "grep"
    blocks.extend(file_blocks)


def collect_scope(scope: str, session: str | None = None,
                  workdir: "str | Path | None" = None) -> list:
    """收集某范围全部文本块（按后缀路由到提取器）。

    roots = 内置 SCOPE_ROOTS + 配置 add（session=None 仅全局配置生效）；
    deny 前缀匹配过滤（文件/目录均可禁，可禁内置范围根）。
    """
    roots = _scope_roots(session, workdir).get(scope) or []
    denies = _effective_search_config(session, workdir)["denies"]
    if not roots:
        return []
    wd = str(_canonical_workdir(workdir))
    blocks = []
    for root in roots:
        abs_root = root if os.path.isabs(root) else os.path.join(wd, root)
        if os.path.isfile(abs_root):
            if not _is_denied(abs_root, denies):
                _collect_file(abs_root, scope, blocks)
            continue
        if not os.path.isdir(abs_root):
            logger.info(f"[search]   {scope} 范围目录不存在: {abs_root}")
            continue
        for path_ in _walk_files(abs_root):
            if _is_denied(path_, denies):
                continue
            _collect_file(path_, scope, blocks)
    if scope == "skills":
        blocks = _dedupe_skills_blocks(blocks, workdir)
    return blocks


# ── 统一入口 ─────────────────────────────────────────────────

def search_scope(query: str, scope: str, method: str | None = None,
                 top_k: int = 5, grep_context: int | None = None,
                 session: str | None = None,
                 workdir: "str | Path | None" = None) -> dict:
    """单范围搜索：语义通道(embedding→ngram→grep) + 精确通道(grep→ngram)"""
    _t0 = time.perf_counter()
    logger.info(f"[search] 范围开始: {scope} query={query!r:.60}")
    blocks = collect_scope(scope, session, workdir)
    _t_collect = (time.perf_counter() - _t0) * 1000
    logger.info(f"[search]   {scope} 收集完成: {len(blocks)} 块, 耗时={_t_collect:.1f}ms")
    semantic = [b for b in blocks if b.channel == "semantic"]
    symbols = [b for b in blocks if b.channel == "symbols"]
    raw = [b for b in blocks if b.channel == "raw"]

    # 语义通道: ngram 主通道 + embedding 附加合并（默认 hybrid）
    #   method="embedding" → 仅 embedding（显式强制）
    #   ngram 永远主跑；embedding 有索引则附加合并（score 归一化），无索引静默跳过
    sem_hits = []
    if method == "embedding":
        # 显式强制 embedding（用户要求）：有结果用之；无 → ngram 防御
        _t1 = time.perf_counter()
        sem_hits = search_embedding(query, semantic, top_k, workdir=workdir) or []
        if sem_hits:
            logger.info(f"[search]   {scope} 语义通道命中: embedding(强制) ×{len(sem_hits)} "
                        f"耗时={(time.perf_counter()-_t1)*1000:.1f}ms")
        else:
            logger.info(f"[search]   {scope} embedding(强制) 无结果 → 降级 ngram")
    else:
        # 默认: ngram 主通道（必跑）
        _t1 = time.perf_counter()
        sem_hits = search_ngram(query, semantic, top_k)
        if sem_hits:
            logger.info(f"[search]   {scope} 语义通道命中: ngram ×{len(sem_hits)} "
                        f"耗时={(time.perf_counter()-_t1)*1000:.1f}ms")
        else:
            logger.info(f"[search]   {scope} ngram 无结果")
        # embedding 附加: 默认关闭（EMBEDDING_ENABLED=False 时纯 ngram）；开启时索引存在才返回结果
        if C.get("EMBEDDING_ENABLED"):
            _t2 = time.perf_counter()
            emb_hits = search_embedding(query, semantic, top_k, workdir=workdir)
            if emb_hits:
                sem_hits = _merge_sem_hits(sem_hits, emb_hits, top_k)
                logger.info(f"[search]   {scope} embedding 附加合并 +{len(emb_hits)} → {len(sem_hits)} "
                            f"耗时={(time.perf_counter()-_t2)*1000:.1f}ms")
    if not sem_hits:
        # grep 防御（仅当 ngram 与 embedding 均无结果）
        _t1 = time.perf_counter()
        sem_hits = search_grep(query, semantic, grep_context, top_k)
        if sem_hits:
            logger.info(f"[search]   {scope} 语义通道命中: grep(防御) ×{len(sem_hits)} "
                        f"耗时={(time.perf_counter()-_t1)*1000:.1f}ms")

    # 精确通道（symbols + raw）
    _t1 = time.perf_counter()
    sym_hits = search_grep(query, symbols + raw, grep_context, top_k)
    if not sym_hits:
        sym_hits = search_ngram(query, symbols, top_k)
    if sym_hits:
        logger.info(f"[search]   {scope} 精确通道命中: ×{len(sym_hits)} "
                    f"耗时={(time.perf_counter()-_t1)*1000:.1f}ms")

    _el = (time.perf_counter() - _t0) * 1000
    logger.info(f"[search] 范围完成: {scope} 总耗时={_el:.1f}ms "
                f"semantic={len(sem_hits)} symbols={len(sym_hits)}")
    return {"semantic": sem_hits, "symbols": sym_hits}


def search_all(query: str, scope: str = "all", method: str | None = None,
               top_k: int = 5, grep_context: int | None = None,
               session: str | None = None,
               workdir: "str | Path | None" = None,
               scopes: list | None = None) -> dict:
    """统一入口：scope=all 时按范围分组返回；否则单范围"""
    _t0 = time.perf_counter()
    logger.info(f"[search] 入口: scope={scope} query={query!r:.60} method={method} top_k={top_k}")
    if not query or not query.strip():
        logger.info(f"[search] 空查询，直接返回")
        return {}
    if scope != "all":
        r = {scope: search_scope(query, scope, method, top_k, grep_context, session, workdir)}
        _el = (time.perf_counter() - _t0) * 1000
        logger.info(f"[search] 完成: scope={scope} 总耗时={_el:.1f}ms")
        return r
    results = {}
    scope_list = scopes if scopes is not None else list(_scope_roots(session, workdir))
    for sc in scope_list:
        results[sc] = search_scope(query, sc, method, top_k, grep_context, session, workdir)
    _el = (time.perf_counter() - _t0) * 1000
    _tot = sum(len(v["semantic"]) + len(v["symbols"]) for v in results.values())
    logger.info(f"[search] 完成: scope=all 总耗时={_el:.1f}ms 总命中={_tot}")
    return results


def _skill_name_from_key(key: str) -> str:
    """从 skills 范围 block key 提取技能名。

    key 形态: /abs/.../skills/<name>/skill.md#p0（内置级）
              或 /abs/.../.xkagent/skills/<name>/skill.md#p0（用户级）
    → 取 "skills" 段之后第一段目录名（两种形态均含独立的 skills 段）。
    """
    path_part = key.split("#", 1)[0].replace("\\", "/")
    segs = path_part.split("/")
    for i, seg in enumerate(segs):
        if seg == "skills" and i + 1 < len(segs):
            return segs[i + 1]
    return ""


def _skill_text_score(query: str, text: str) -> float:
    """对单一文本计算 ngram 加权分（与 search_ngram 同量纲: sim*20 + kw*2）"""
    q_tokens = collections.Counter(_tokenize(query))
    q_ngrams = _char_ngrams(query)
    sim = _cosine_counter(q_ngrams, _char_ngrams(text))
    t_tokens = set(_tokenize(text))
    kw = sum(q_tokens.get(tok, 0) for tok in t_tokens if tok in q_tokens)
    return sim * 20.0 + kw * 2.0


def _searchskill_ranked(query: str, top_k: int = 5) -> list[dict]:
    """搜索与 query 最匹配的技能，返回结构化明细列表（技能聚合打分）。

    双通道合并：
      ① 块级：search_scope("skills") hybrid 通道命中正文段落（ngram 主 + embedding 附加）
      ② frontmatter 级：description + triggers 直接 ngram 打分（正文提取剥离 frontmatter，
         此处补回 desc/triggers 匹配能力，如"定时执行任务"→ scheduled_task）
    每技能取最高分，幽灵技能过滤后按分数降序取 top_k。
    返回 [{"name", "description", "snippet", "path", "method", "score"}, ...]，
    snippet/path 为块级命中上下文（对齐 searchinfo 的 [method] path | snippet 展示风格）。
    """
    if not query or not query.strip():
        return []
    valid = set()
    try:
        from codes.skill import SkillLoader
        valid = set(SkillLoader.list_skills())
    except Exception:
        pass
    # 本地命中（ngram/frontmatter，中文可信）与 embedding-only（附加补充）分离：
    # ngram 是主通道，embedding 是附加——embedding-only 技能排在本地命中之后，
    # 避免英文模型中文区分度差导致的噪声（分数拥挤 0.87×20≈17）挤占真实命中
    local: dict = {}      # 本地命中技能 → (最高本地分, snippet, path, method)
    emb_only: dict = {}   # 仅 embedding 命中技能 → (最高 embedding 分, snippet, path, method)
    # ① 块级命中（取 top_k*4 给技能聚合留余量）
    try:
        r = search_scope(query, "skills", top_k=top_k * 4)
        for h in r.get("semantic") or []:
            name = _skill_name_from_key(h.key)
            if not name:
                continue
            path = _rel_path(h.key, None)
            if h.method == "ngram":
                if h.score < 2.0:
                    continue  # 与 frontmatter 阈值统一：<2.0 视为噪声（英文随机串 ngram 偶然重叠）
                if name not in local or h.score > local[name][0]:
                    local[name] = (h.score, h.snippet, path, h.method)
            else:  # embedding 附加
                if name not in emb_only or h.score > emb_only[name][0]:
                    emb_only[name] = (h.score, h.snippet, path, h.method)
    except Exception:
        pass
    # ② frontmatter 级（desc + triggers，本地通道）
    if valid:
        for name in valid:
            try:
                meta = SkillLoader.load_meta(name)
                desc = meta.get("description", "") or ""
                trig = meta.get("triggers", [])
                if not isinstance(trig, list):
                    trig = [str(trig)] if trig else []
                text = (desc + " " + "triggers: " + ", ".join(trig)).strip()
                if not text:
                    continue
                fs = _skill_text_score(query, text)
                # frontmatter 文本短（desc+triggers），1.0 阈值过松：
                # 随机英文串 ngram 偶然重叠可达 1.3（kw=0）；实测真实匹配 ≥2.0
                if fs < 2.0:
                    continue  # 低于阈值视为噪声（如无意义英文串）
                if name not in local or fs > local[name][0]:
                    local[name] = (fs, desc, f"skills/{name}/skill.md", "frontmatter")
            except Exception:
                continue
    # 纯 embedding 守卫：本地通道（ngram/frontmatter）全空时，
    # embedding 附加的命中是噪声（实测随机串 embedding 分 7-9），直接返回 []
    if not local:
        return []
    # 幽灵过滤 + 排序截断：本地命中按分降序在前，embedding-only 附加补充在后
    # （emb_only 中已在 local 的技能跳过，避免同技能重复输出）
    _seen_local = set(local)
    ranked = (sorted(local.items(), key=lambda x: -x[1][0])
              + [(n, v) for n, v in sorted(emb_only.items(), key=lambda x: -x[1][0])
                 if n not in _seen_local])
    out = []
    for name, (score, snippet, path, method) in ranked:
        if valid and name not in valid:
            continue
        snippet = " ".join(snippet.split())[:80]  # 2026-09-04: 单行压缩+截断（建议技能单行化配套）
        out.append({
            "name": name,
            "description": (SkillLoader.load_meta(name) or {}).get("description", "") if valid else "",
            "snippet": snippet,
            "path": path,
            "method": method,
            "score": score,
        })
        if len(out) >= top_k:
            break
    return out


def searchskill(query: str, top_k: int = 5) -> list[str]:
    """搜索与 query 最匹配的技能，返回技能名列表（兼容旧调用）。

    明细版（含 snippet/path，对齐 searchinfo 展示风格）见 searchskill_detail。
    """
    return [d["name"] for d in _searchskill_ranked(query, top_k)]


def searchskill_detail(query: str, top_k: int = 5) -> list[dict]:
    """搜索与 query 最匹配的技能，返回结构化明细（建议技能展示用）。

    返回 [{"name", "description", "snippet", "path", "method", "score"}, ...]，
    snippet/path 为搜索命中上下文（对齐 searchinfo 的 [method] path | snippet 风格）。
    """
    return _searchskill_ranked(query, top_k)



_NOISE_HISTORY_ROLES = {"thinking", "tool"}


def _dir_contains(file_dir: str, dirpath: str, dir_stat=None) -> bool:
    """file_dir 是否为 dirpath 自身或其子孙目录（inode 级判定）。

    build 受限沙箱下 os.walk/scandir 返回的路径视角可能与模块
    __file__/BUILTIN_SKILLS_DIR 的视角不一致（bind 别名），字符串
    前缀比较会误判；inode（st_dev+st_ino）在 bind mount 两侧一致，可跨视角判定。
    """
    if dir_stat is None:
        try:
            dir_stat = os.stat(dirpath)
        except OSError:
            return False
    cur = file_dir
    hops = 0
    while True:
        try:
            st = os.stat(cur)
        except OSError:
            pass
        else:
            if st.st_dev == dir_stat.st_dev and st.st_ino == dir_stat.st_ino:
                return True
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent
        hops += 1
        if hops > 64:  # 防环
            return False


def _dedupe_skills_blocks(blocks: list, workdir: "str | Path | None" = None) -> list:
    """skills 范围块过滤：与加载侧一致，同名技能用户级覆盖内置级。

    块 key 为绝对路径（extract_markdown）。对每个块按技能名在 _skill_dirs(workdir)
    中定位有效目录（用户级优先），块文件不在有效目录下（即被用户级覆盖的内置级
    同名技能文件）则丢弃，避免搜索命中被覆盖技能的旧内容。
    """
    _MISS = object()
    try:
        from codes.skill import _skill_dirs
    except Exception:
        return blocks
    try:
        skdirs = [os.path.realpath(p) for p in _skill_dirs(workdir)]
    except Exception:
        return blocks
    eff_cache: dict = {}
    out = []
    for b in blocks:
        name = _skill_name_from_key(b.key)
        if not name:
            out.append(b)
            continue
        eff = eff_cache.get(name, _MISS)
        if eff is _MISS:
            eff = None
            for d in skdirs:
                if os.path.isfile(os.path.join(d, name, "skill.md")):
                    eff = d
                    break
            eff_cache[name] = eff
        if eff is None:
            out.append(b)
            continue
        # 路径视角别名兼容：受限沙箱 scandir 返回的视角可能与模块
        # __file__ 不一致——realpath 前缀作快路径，跨视角
        # 场景（前缀不匹配）用 _dir_contains inode 级兜底判定。
        path_part = b.key.split("#", 1)[0].split(":", 1)[0]
        if path_part == eff or path_part.startswith(eff + os.sep) or _dir_contains(
                os.path.dirname(path_part), eff):
            out.append(b)
    return out

def _rel_path(key: str, session: str | None = None) -> str:
    """从 SearchHit.key 反解相对 workdir 的文件路径。

    key 形态:
      /abs/.../skill.md#p0   → /abs/.../skill.md（md/py/c/rs 提取器）
      historys:{sess}:{rid}  → .xkagent/historys/{sess}.db（extract_history）
      /abs/.../xxx.log:123   → /abs/.../xxx.log（extract_log 行号后缀）
    """
    from codes import config
    wd = str(config.get_session_context(str(session)).workdir) if session else str(config.get_workdir())
    if key.startswith("historys:"):
        sess = key.split(":", 2)[1]
        return os.path.join(".xkagent", "historys", f"{sess}.msgz")
    path_part = key.split("#", 1)[0].split(":", 1)[0]  # log 行号: 与路径分隔
    if not path_part:
        return ""
    try:
        rel = os.path.relpath(path_part, wd)
    except ValueError:
        return path_part
    # 越界路径（不在 workdir 下）原样返回绝对路径，避免 ../ 泄漏语义
    if rel.startswith(".."):
        return path_part
    return rel


def _is_noise_hit(hit) -> bool:
    """过滤不适合作为参考信息的命中：推理/工具结果/技能选择辅助消息。"""
    if hit.scope != "historys":
        return False
    role = (hit.meta.get("role") or "").strip()
    if role in _NOISE_HISTORY_ROLES:
        return True
    text = (hit.meta.get("text") or "").lstrip()
    if text.startswith("🎯 技能选择"):
        return True
    if text.startswith("[技能") and "定义 - 完整版]" in text:
        return True
    return False


def searchinfo(dirs_paths: list, query: str, top_k: int = 5,
               grep_context: int | None = None,
               session: str | None = None) -> list:
    """按指定目录列表 + 查询/关键词搜索文件内容（推荐信息/skill 不符时深挖用）。

    Args:
        dirs_paths: 要搜索的目录/文件路径列表（相对 workdir 或绝对路径）
        query: 查询文本或关键词
        top_k: 返回条数（默认 5）
        grep_context: grep 上下文窗口（默认用配置）
        session: 当前 session（合并 per-session deny 配置）

    Returns:
        [{"path", "snippet", "score", "method", "channel"}, ...]
        同文件最多 2 条；无结果/失败 → []。
    """
    if not query or not query.strip() or not dirs_paths:
        return []
    from codes import config
    from codes.path_guard import allowed_roots_for_session, path_in_roots
    wd = str(config.get_workdir())
    roots = allowed_roots_for_session(session)
    denies = _effective_search_config(session)["denies"]
    blocks: list = []
    seen_files: set = set()
    for d in dirs_paths:
        if not d or not str(d).strip():
            continue
        dstr = str(d)
        abs_root = dstr if os.path.isabs(dstr) else os.path.abspath(os.path.join(wd, dstr))
        if not path_in_roots(abs_root, roots):
            logger.info(f"[searchinfo] 路径越界，跳过: {abs_root}")
            continue
        if _is_denied(abs_root, denies):
            continue
        if os.path.isfile(abs_root):
            if abs_root not in seen_files:
                seen_files.add(abs_root)
                _collect_file(abs_root, "custom", blocks)
            continue
        if not os.path.isdir(abs_root):
            logger.info(f"[searchinfo] 目录不存在，跳过: {abs_root}")
            continue
        for path_ in _walk_files(abs_root):
            if path_ in seen_files:
                continue
            if not path_in_roots(path_, roots):
                continue
            seen_files.add(path_)
            if _is_denied(path_, denies):
                continue
            _collect_file(path_, "custom", blocks)
    if not blocks:
        return []
    semantic = [b for b in blocks if b.channel == "semantic"]
    symbols = [b for b in blocks if b.channel == "symbols"]
    raw = [b for b in blocks if b.channel == "raw"]
    # 语义通道: ngram 主 + embedding 附加（存在索引的 scope 全启用）+ grep 防御
    sem_hits = search_ngram(query, semantic, top_k * 4)
    idx_dir = _search_index_dir()
    if C.get("EMBEDDING_ENABLED") and os.path.isdir(idx_dir):
        scopes = [fn[:-3] for fn in os.listdir(idx_dir) if fn.endswith(".db")]
        if scopes:
            try:
                emb_hits = search_embedding(query, semantic, top_k * 4, scopes=scopes)
            except Exception:
                emb_hits = None
            if emb_hits:
                sem_hits = _merge_sem_hits(sem_hits, emb_hits, top_k * 4)
    if not sem_hits:
        sem_hits = search_grep(query, semantic, grep_context, top_k * 4)
    # 精确通道（symbols + raw）
    sym_hits = search_grep(query, symbols + raw, grep_context, top_k)
    if not sym_hits:
        sym_hits = search_ngram(query, symbols, top_k)
    hits = list(sem_hits) + list(sym_hits)
    if not hits:
        return []
    # 路径反解 + per-file cap 2（对齐 recommend_info）
    per_file: dict = {}
    for h in hits:
        rel = _rel_path(h.key, session)
        if not rel:
            continue
        per_file.setdefault(rel, []).append((h, rel))
    out = []
    for rel, items in per_file.items():
        items.sort(key=lambda x: -x[0].score)
        out.extend(items[:2])
    out.sort(key=lambda x: -x[0].score)
    items = []
    for h, rel in out[:top_k]:
        text = (h.meta.get("text") or h.snippet or "").strip()
        text = " ".join(text.split())
        items.append({
            "path": rel,
            "snippet": text[:80],
            "score": round(h.score, 2),
            "method": h.method,
            "channel": h.channel,
        })
    return items


def recommend_info(query: str, top_k: int = 5, exclude_session: str | None = None,
                   session: str | None = None) -> list:
    """推荐信息：跨全范围检索与 query 相关的片段 + 相对路径。

    Args:
        query: 用户需求文本
        top_k: 返回条数（默认 5）
        exclude_session: 排除的 session 名（其 historys 命中已在上下文中，
            避免重复注入浪费 token；None = 不排除）

    Returns:
        [{"scope", "path", "snippet"}, ...]，按 score 降序；
        同文件最多 2 条（per-file cap，避免单文件刷屏）；
        无匹配/失败 → []（上层静默降级）。
    """
    if not query or not query.strip():
        return []
    try:
        _scopes = [sc for sc in _scope_roots(session) if sc != "logs"]  # 2026-08-23: 推荐信息不搜 logs
        r = search_all(query, scope="all", method=None, top_k=2, session=session, scopes=_scopes)
    except Exception as e:
        logger.warning(f"[search] recommend_info 检索失败，静默降级: {e}")
        return []
    # 合并所有范围 semantic + symbols（skills 默认排除：需求 2026-08-07）
    hits = []
    for _sc, res in r.items():
        if _sc == "skills":
            continue
        if _sc == "logs":
            continue  # 2026-08-23: logs excluded from recommend_info
        hits.extend(res.get("semantic") or [])
        hits.extend(res.get("symbols") or [])
    if not hits:
        return []
    excl_path = None
    if exclude_session:
        excl_path = os.path.join(".xkagent", "historys", f"{exclude_session}.msgz")
    # 噪声过滤 + 路径反解 + 当前 session 排除
    filtered = []
    for h in hits:
        if _is_noise_hit(h):
            continue
        rel = _rel_path(h.key, session)
        if not rel:
            continue
        if excl_path and rel == excl_path:
            continue
        filtered.append((h, rel))
    if not filtered:
        return []
    # 同文件最多 2 条（按 score 降序后截前 2）
    per_file = {}
    for h, rel in filtered:
        per_file.setdefault(rel, []).append((h, rel))
    out = []
    for rel, items in per_file.items():
        items.sort(key=lambda x: -x[0].score)
        out.extend(items[:2])
    out.sort(key=lambda x: -x[0].score)
    # 组装输出（片段压缩为单行，截 80 字符）
    items = []
    for h, rel in out[:top_k]:
        text = (h.meta.get("text") or h.snippet or "").strip()
        text = " ".join(text.split())
        items.append({
            "scope": h.scope,
            "path": rel,
            "snippet": text[:80],
        })
    return items


# ═══════════════════════════════════════════════════════════════
#  docs 持久化（summary 工具 / compact 落盘共用）
# ═══════════════════════════════════════════════════════════════

_DOC_ROOT = ".xkagent/docs"


def write_doc(session, content, source="summary", title="", tags=None, model=""):
    """写入 docs 持久化文档，返回相对路径；失败抛 ValueError。

    时间系统：文件名时间戳 + frontmatter created_at 双保险。
    source ∈ {compact, summary}。
    直接 open() 写（agent 主进程调用，不经 pythonrt 沙箱；
    与 update_embeddings 写 search_index 同权限模型）。
    """
    from codes import config
    wd = str(config.get_workdir())
    sess_safe = re.sub(r'[^\w\-.]', '_', str(session or "default"))
    # 2026-09-10: 目录名仍用 session name（ASCII key，路径稳定）；frontmatter 附展示标题，
    # 仅供人读/检索辨识，未设置时与 sess_safe 一致（fallback name）。
    try:
        from codes import session_registry as _sreg
        _sctx = _sreg.get(str(session or ""))
        sess_title = (_sctx.title if _sctx else "") or sess_safe
    except Exception:
        sess_title = sess_safe
    ts = datetime.now()
    fname = f"{ts:%Y%m%d_%H%M%S}_{source}.md"
    rel_dir = os.path.join(_DOC_ROOT, sess_safe)
    abs_dir = os.path.join(wd, rel_dir)
    os.makedirs(abs_dir, exist_ok=True)
    body = (content or "").strip()
    if not body:
        raise ValueError("write_doc: content 为空")
    if not title:
        first_line = body.split("\n", 1)[0].strip()
        title = first_line[:60]
    tags_str = ""
    if tags:
        tags_str = "tags: [" + ", ".join(str(t) for t in tags) + "]\n"
    front = (
        "---\n"
        f"created_at: {ts:%Y-%m-%dT%H:%M:%S}\n"
        f"session: {sess_safe}\n"
        f"session_title: {sess_title}\n"
        f"source: {source}\n"
        f"title: {title}\n"
        f"model: {model or ''}\n"
        f"{tags_str}"
        "---\n\n"
    )
    full = front + body + "\n"
    abs_path = os.path.join(abs_dir, fname)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(full)
    # 读回校验关键锚点（created_at + 正文非空）
    back = open(abs_path, encoding="utf-8").read()
    if "created_at" not in back or len(back.strip()) < 30:
        raise ValueError(f"write_doc 读回校验失败: {abs_path}")
    return os.path.join(rel_dir, fname)


# ═══════════════════════════════════════════════════════════════
#  hybrid 合并 / 索引构建 / 异步预热（ngram 主 + embedding 附加）
# ═══════════════════════════════════════════════════════════════

def _merge_sem_hits(ngram_hits: list, emb_hits: list, top_k: int) -> list:
    """ngram 主通道结果 + embedding 附加结果合并（去重，score 归一化）。

    embedding score ∈ [0.2, 1.0]，映射到 ngram 量纲: emb*20（与 ngram sim*20 对齐）。
    同 key 取较大 score；按 score 降序截 top_k。
    """
    merged: dict = {}
    for h in ngram_hits:
        merged[h.key] = (h.score, h)
    for h in emb_hits:
        # 归一化到 ngram 量纲并附加折扣 ×0.7：embedding 是"附加"通道，
        # 英文模型中文区分度差（分数拥挤 0.55-0.87），不折扣会挤占 ngram 真实命中
        s = h.score * 20.0 * 0.7
        h.score = s  # 就地归一化，确保返回后全局排序正确
        if h.key in merged:
            if s > merged[h.key][0]:
                merged[h.key] = (s, h)
        else:
            merged[h.key] = (s, h)
    out = [h for _, h in sorted(merged.values(), key=lambda x: -x[0])]
    return out[:top_k]


def _atomic_write_json(path: str, data: dict) -> None:
    """在目标目录内写临时文件后原子替换 JSON。"""
    fd, tmp_path = tempfile.mkstemp(prefix=f".{Path(path).name}.", suffix=".tmp",
                                    dir=str(Path(path).parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass


def _build_scope_index(scope: str, sentences: list, manifest: dict,
                       workdir: "str | Path | None" = None) -> None:
    """完整构建临时 SQLite；仅全部成功后替换 scope 的现有索引。"""
    idx_dir = _search_index_dir(workdir)
    os.makedirs(idx_dir, exist_ok=True)
    db_path = _index_db_path(scope, workdir)
    fd, tmp_db = tempfile.mkstemp(prefix=f".{scope}.", suffix=".db.tmp", dir=idx_dir)
    os.close(fd)
    try:
        # 即使没有 semantic 内容也生成有效空库，以原子方式清除旧向量。
        conn = _get_db(tmp_db)
        conn.execute("CREATE TABLE IF NOT EXISTS index_manifest (id INTEGER PRIMARY KEY CHECK (id=1), data TEXT NOT NULL)")
        conn.execute("INSERT OR REPLACE INTO index_manifest (id, data) VALUES (1, ?)",
                     (json.dumps(manifest, ensure_ascii=False, sort_keys=True),))
        conn.commit()
        conn.close()
        groups: dict = {}
        for text, key in sentences:
            groups.setdefault(key, []).append(text)
        for key, texts in groups.items():
            if not store_batch(texts, tmp_db, key):
                raise RuntimeError(f"写入索引失败: scope={scope}, key={key}")
        with open(tmp_db, "rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp_db, db_path)
        _mark_dirty(db_path)
        try:
            _atomic_write_json(os.path.join(idx_dir, f"{scope}.manifest.json"), manifest)
        except OSError as exc:
            # DB 内 manifest 与索引原子提交；旁路 JSON 失败不回滚已完成的 DB 替换。
            logger.warning(f"[search] {scope} manifest JSON 写入失败: {exc}")
    finally:
        with _FAISS_CACHE_LOCK:
            _FAISS_CACHE.pop(_db_cache_key(tmp_db), None)
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(tmp_db + suffix)
            except FileNotFoundError:
                pass


def update_embeddings(verbose: bool = True, workdir: "str | Path | None" = None,
                      session: str | None = None, scopes=None) -> dict:
    """为生效范围（skills/docs/historys/logs，codes 默认关闭）重建向量索引。

    复用 collect_scope 提取器产出 semantic 文本块，统一写入
    .xkagent/search_index/{scope}.db（skills 也统一，废除技能目录独立 db）。
    skills 额外写入 frontmatter（description+triggers）句子，提升技能级召回。
    返回 {scope: 写入 key 数}。
    """
    canonical_workdir = _canonical_workdir(workdir)
    idx_dir = _search_index_dir(canonical_workdir)
    os.makedirs(idx_dir, exist_ok=True)
    result: dict = {}
    effective_roots = _scope_roots(session, canonical_workdir)
    selected_scopes = list(effective_roots) if scopes is None else (
        [scopes] if isinstance(scopes, str) else list(scopes))
    for scope in selected_scopes:
        blocks = collect_scope(scope, session, canonical_workdir)
        semantic = [b for b in blocks if b.channel == "semantic"]
        # (text, key) 列表
        sentences = []
        # skills 额外: frontmatter description + triggers（按技能名 key）
        if scope == "skills":
            try:
                from codes import config
                from codes.skill import SkillLoader
                same_workdir = workdir is None or canonical_workdir == Path(config.get_workdir()).resolve()
                for name in SkillLoader.list_skills() if same_workdir else []:
                    meta = SkillLoader.load_meta(name)
                    if not meta:
                        continue
                    desc = meta.get("description", "") or ""
                    trig = meta.get("triggers", [])
                    if not isinstance(trig, list):
                        trig = [str(trig)] if trig else []
                    t = "triggers: " + ", ".join(trig) if trig else ""
                    for s in [desc, t]:
                        if s:
                            sentences.append((s, f"skills:{name}#meta"))
            except Exception:
                pass
        for b in semantic:
            text = (b.meta.get("text") or b.snippet or "").strip()
            if text:
                sentences.append((text, b.key))
        groups = {key for _, key in sentences}
        manifest = {
            "schema_version": 1,
            "canonical_workdir": str(canonical_workdir),
            "scope": scope,
            "built_at": datetime.now(timezone.utc).isoformat(),
            "build": {"strategy": "atomic-full-rebuild", "format": "sqlite-json-vectors"},
            "config": {
                "session": session,
                "roots": effective_roots.get(scope, []),
                "effective": _effective_search_config(session, canonical_workdir),
                "index_window": C["INDEX_WINDOW"],
                "index_overlap": C["INDEX_OVERLAP"],
            },
            "model": {"name": _ONNX_MODEL_NAME, "directory": str(Path(_ONNX_MODEL_DIR).resolve())},
            "counts": {"blocks": len(blocks), "semantic_blocks": len(semantic),
                       "sentences": len(sentences), "keys": len(groups)},
        }
        try:
            _build_scope_index(scope, sentences, manifest, canonical_workdir)
        except Exception as exc:
            logger.warning(f"[search] {scope} 索引重建失败，保留旧索引: {exc}")
            result[scope] = 0
            if verbose:
                print(f"  ❌ {scope}: 重建失败，旧索引保持不变")
            continue
        result[scope] = len(groups)
        if verbose:
            print(f"  📂 {scope}: {len(semantic)} 块 / {len(groups)} key -> {_index_db_path(scope, canonical_workdir)}")
    return result


_EMB_READY = False


def _load_models_sync():
    """后台线程：加载 ONNX Runtime 模型并预热 FAISS 索引（已建索引范围）。"""
    global _EMB_READY
    if not C.get("EMBEDDING_ENABLED"):
        # embedding 默认禁止（2026-08-10 用户决策）：不加载模型，保持 _EMB_READY=False
        return
    if faiss is None or np is None:
        # faiss/numpy 缺失：embedding 不可用 → 保持 _EMB_READY=False，纯 ngram 运行
        return
    try:
        _get_model()  # 加载 ONNX Runtime 模型（单例）
        from codes import config
        from codes.session_registry import list_contexts
        workdirs = {config.get_default_workdir().resolve()}
        workdirs.update(context.workdir.resolve() for context in list_contexts())
        for workdir in workdirs:
            idx_dir = _search_index_dir(workdir)
            if not os.path.isdir(idx_dir):
                continue
            for fn in sorted(os.listdir(idx_dir)):
                if not fn.endswith(".db"):
                    continue
                manifest_path = os.path.join(idx_dir, f"{fn[:-3]}.manifest.json")
                try:
                    if os.path.isfile(manifest_path):
                        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
                        if manifest.get("canonical_workdir") != str(workdir):
                            continue
                    search("warmup", os.path.join(idx_dir, fn), top_k=1)
                except Exception:
                    continue
        _EMB_READY = True
    except Exception:
        pass  # 加载失败保持 False，embedding 附加自动跳过（纯 ngram）


def init_async():
    """启动时调用：后台线程异步加载 embedding 模型，不阻塞主流程。

    在不支持 threading 的环境（WASM 沙箱）中静默降级，不影响主流程。
    """
    try:
        thread = threading.Thread(target=_load_models_sync, daemon=True)
        thread.start()
    except RuntimeError:
        pass  # 环境不支持 threading，静默降级
