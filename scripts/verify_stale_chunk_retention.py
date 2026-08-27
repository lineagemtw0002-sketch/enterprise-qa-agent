"""验证：文档更新后，旧版本的片段是否还留在知识库里。

背景见 docs/scale_slo_and_priorities.md §1.5。读代码得出的推断是：
- doc_id 就是文件 SHA256（pipeline.py:288），内容一变即被当作全新文档
- 片段级去重是"跳过"语义而非"替换"语义
- 全仓不存在文档版本替换逻辑
=> 更新一份文档后，旧版本的片段永远不会被删除，新旧两版会同时被检索到

本脚本做真实摄入来确认或推翻这个推断：
  1. 建一个一次性测试 collection
  2. 摄入 v1（含一句「年假上限 10 天」）
  3. 把那句话改成「年假上限 15 天」，摄入 v2
  4. 把 collection 里所有片段 dump 出来，看两个版本是否同时存在
  5. 清理测试 collection

用法：
    python scripts/verify_stale_chunk_retention.py
    python scripts/verify_stale_chunk_retention.py --keep   # 保留测试库供人工检查
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.settings import load_settings, resolve_path
from src.ingestion.pipeline import IngestionPipeline

# 每次运行都掺入时间戳：文件级完整性检查按内容哈希跳过已处理文件，
# 内容固定的话第二次运行会被整体跳过，测不出东西。
RUN_TAG = time.strftime("%Y%m%d_%H%M%S")
COLLECTION = f"verify_stale_{RUN_TAG}"

DOC_TEMPLATE = """公司假期管理制度（测试文档 {tag}）

第一章 总则
本制度适用于全体员工，由人力资源部负责解释。

第二章 年假
{leave_sentence}
年假需提前三个工作日申请，经直属主管批准后生效。

第三章 其他
本制度自发布之日起施行。
"""

V1_SENTENCE = "员工年假上限为 10 天。"
V2_SENTENCE = "员工年假上限为 15 天。"


def write_doc(path: Path, sentence: str) -> None:
    path.write_text(
        DOC_TEMPLATE.format(tag=RUN_TAG, leave_sentence=sentence),
        encoding="utf-8",
    )


def dump_chunks(pipeline: IngestionPipeline) -> list[str]:
    """把 collection 里所有片段的正文取出来（直接读底层 Chroma collection，
    绕开检索阈值与重排，看到的就是库里真实存了什么）。

    复用 pipeline 自己的 vector store —— Chroma 不允许同一进程内用不同 settings
    对同一路径建第二个 PersistentClient，另起客户端会直接抛
    "An instance of Chroma already exists ... with different settings"。
    """
    store = pipeline.vector_upserter.vector_store
    got = store.collection.get(include=["documents"])
    return got.get("documents") or []


def cleanup(pipeline: IngestionPipeline, collection: str) -> None:
    client = pipeline.vector_upserter.vector_store.client
    for name in (collection, f"{collection}__summary"):
        try:
            client.delete_collection(name)
        except Exception:
            pass
    for d in (resolve_path(f"data/db/bm25/{collection}"), resolve_path(f"data/images/{collection}")):
        shutil.rmtree(d, ignore_errors=True)
    # 摄入历史里的记录也一并清掉，避免污染后续运行
    db = resolve_path("data/db/ingestion_history.db")
    if Path(db).exists():
        conn = sqlite3.connect(db)
        try:
            for tbl in ("chunk_dedup", "file_integrity", "ingestion_history"):
                try:
                    conn.execute(f"DELETE FROM {tbl} WHERE collection = ?", (collection,))
                except sqlite3.OperationalError:
                    pass  # 表不存在或无 collection 列
            conn.commit()
        finally:
            conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="跑完不清理测试 collection")
    args = ap.parse_args()

    print(f"测试 collection: {COLLECTION}\n")
    settings = load_settings()
    pipeline = IngestionPipeline(settings=settings, collection=COLLECTION)

    workdir = Path(tempfile.mkdtemp(prefix="verify_stale_"))
    doc = workdir / "假期管理制度.txt"

    try:
        # ---- v1 ----
        write_doc(doc, V1_SENTENCE)
        r1 = pipeline.run(str(doc))
        print(f"[v1] success={r1.success} doc_id={str(r1.doc_id)[:16]}... "
              f"入库片段={r1.chunk_count} 去重跳过={r1.duplicate_chunk_count}")

        # ---- v2：只改那一句 ----
        write_doc(doc, V2_SENTENCE)
        r2 = pipeline.run(str(doc))
        print(f"[v2] success={r2.success} doc_id={str(r2.doc_id)[:16]}... "
              f"入库片段={r2.chunk_count} 去重跳过={r2.duplicate_chunk_count}")
        print(f"     doc_id 是否变化: {r1.doc_id != r2.doc_id}\n")

        # ---- 取出库里所有片段 ----
        chunks = dump_chunks(pipeline)
        has_v1 = [c for c in chunks if "10 天" in c]
        has_v2 = [c for c in chunks if "15 天" in c]

        print(f"库中片段总数: {len(chunks)}")
        print(f"  含旧版本「10 天」的片段: {len(has_v1)}")
        print(f"  含新版本「15 天」的片段: {len(has_v2)}\n")

        if has_v1 and has_v2:
            print("❌ 推断成立：新旧两个版本同时留在库里")
            print("   → 检索时模型会同时拿到「上限 10 天」和「上限 15 天」，")
            print("     且片段上没有版本/更新时间信息可供判断哪个是当前版本。")
            verdict = 1
        elif has_v2 and not has_v1:
            print("✅ 推断被推翻：旧版本已被清除，只剩新版本")
            verdict = 0
        elif has_v1 and not has_v2:
            print("⚠️ 异常：只有旧版本，新版本没进去（v2 可能被整体跳过）")
            verdict = 2
        else:
            print("⚠️ 异常：两个版本都没找到，脚本或摄入有问题")
            verdict = 2

        if has_v1:
            print(f"\n旧版本片段原文（节选）:\n  {has_v1[0][:120]}...")
        return verdict
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        if args.keep:
            print(f"\n(--keep) 保留测试 collection: {COLLECTION}")
        else:
            cleanup(pipeline, COLLECTION)
            print(f"\n已清理测试 collection: {COLLECTION}")


if __name__ == "__main__":
    raise SystemExit(main())
