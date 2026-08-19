"""把 `scripts/generate_tenant_kb_corpus.py` 生成的语料批量摄入到 Acme / Globex
各自独立的 Chroma 持久化目录 + 租户专属 collection（`tenant_{name}_kb`）。

跟 `scripts/seed_tenant_kb_demo.py::_ingest_tenant_kb` 用的是同一套 settings
override 方式（覆盖 `vector_store.persist_directory`），复用同一个 `IngestionPipeline`
实例摄入某个租户下的全部文件——不是每个文件都重新构建一次 pipeline（避免重复
初始化 embedding client / chunk refiner 等组件的开销，230+ 个文件很快）。

用法（先跑 generate_tenant_kb_corpus.py 生成语料，再跑这个脚本摄入）：
    python scripts/generate_tenant_kb_corpus.py
    python scripts/ingest_tenant_kb_corpus.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from src.core.settings import load_settings
from src.ingestion.pipeline import IngestionPipeline

TENANTS = ["acme", "globex"]


def ingest_tenant(tenant_name: str) -> None:
    corpus_dir = Path("data/tenant_demo") / tenant_name / "kb_corpus"
    files = sorted(corpus_dir.rglob("*.txt"))
    if not files:
        print(f"[SKIP] {corpus_dir} 没有找到语料文件，先跑 generate_tenant_kb_corpus.py")
        return

    settings = load_settings()
    tenant_settings = settings.model_copy(update={
        "vector_store": settings.vector_store.model_copy(update={
            "persist_directory": f"data/tenant_demo/{tenant_name}/chroma",
        }),
    })
    collection = f"tenant_{tenant_name}_kb"
    pipeline = IngestionPipeline(tenant_settings, collection=collection, force=True)

    print(f"\n{'='*60}\n摄入 {len(files)} 个文件 -> collection={collection}\n{'='*60}")
    t0 = time.monotonic()
    ok, failed = 0, 0
    for i, f in enumerate(files, start=1):
        result = pipeline.run(str(f))
        if result.success:
            ok += 1
        else:
            failed += 1
            print(f"  [FAIL] {f}: {result.error}")
        if i % 50 == 0:
            print(f"  ...已处理 {i}/{len(files)}")
    elapsed = time.monotonic() - t0
    print(f"[OK] {tenant_name}: 成功 {ok}，失败 {failed}，耗时 {elapsed:.1f}s")


def main() -> None:
    for tenant_name in TENANTS:
        ingest_tenant(tenant_name)


if __name__ == "__main__":
    main()
