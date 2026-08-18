"""摄入 data/kb_seed/ 下的部门知识库示例文档到各自的 collection。

collection 命名用 ASCII slug（ChromaDB 不接受中文 collection 名），中文只作为
文档内容和展示名使用：
    it_kb          IT 知识库
    attendance_kb  考勤知识库
    logistics_kb   后勤知识库
    legal_kb       法务知识库（高权限）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from src.core.settings import load_settings
from src.ingestion.pipeline import IngestionPipeline

SEED_DIR = Path("data/kb_seed")

COLLECTIONS = {
    "it_kb.txt": "it_kb",
    "attendance_kb.txt": "attendance_kb",
    "logistics_kb.txt": "logistics_kb",
    "legal_kb.txt": "legal_kb",
}


def main():
    settings = load_settings()

    for filename, collection in COLLECTIONS.items():
        file_path = SEED_DIR / filename
        if not file_path.exists():
            print(f"  [SKIP] {file_path} 不存在")
            continue

        print(f"\n{'='*60}\nIngesting {filename} -> collection={collection}\n{'='*60}")
        pipeline = IngestionPipeline(settings, collection=collection, force=True)
        result = pipeline.run(str(file_path))
        if result.success:
            print(f"  [OK] {result.chunk_count} chunks, {len(result.vector_ids)} vectors")
        else:
            print(f"  [FAIL] {result.error}")


if __name__ == "__main__":
    main()
