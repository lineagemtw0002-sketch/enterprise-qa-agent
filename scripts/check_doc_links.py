#!/usr/bin/env python3
"""文档链接体检 —— 扫全仓所有对 `docs/*.md` 的引用，逐个确认目标文件存在。

**为什么需要它**：`CLAUDE.md` §7.4 要求"一份设计被取代时尽快删除"，§7.5 要求
"脚本落仓不许丢"。文档一旦改名或删除，引用它的地方会**静默断链**——
`docs/` 路径不只出现在 markdown 里，还散在 40 多处 Python 注释/docstring 中
（`workflow.py`、`intent.py`、`query_knowledge_hub.py` …），
改文档时最容易漏掉的就是这批代码内引用。

**它检查什么**：
1. 全仓（排除 `.venv` / `node_modules` / `.git` / `.claude/worktrees`）里所有形如
   `docs/xxx.md` 或 `docs/dir/xxx.md` 的字符串；
2. 每个引用的目标文件是否真实存在；
3. 额外报告"孤儿文档"——`docs/` 下没有被任何地方引用的 `.md`
   （只是提示，不算失败：`CLAUDE.md` §8 的索引表本身就是入口）。

**它不检查什么**（见文末 SCOPE）。

用法：
    .venv/bin/python scripts/check_doc_links.py            # 体检，有断链则退出码 1
    .venv/bin/python scripts/check_doc_links.py --orphans  # 额外列出孤儿文档
    .venv/bin/python scripts/check_doc_links.py --json     # 机器可读输出

建立于 2026-08-26（`docs/doc_reorg_plan.md` 的回归保护手段）。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# 形如 docs/a.md 、docs/sub_dir/b.md 。刻意不匹配裸文件名（`role.md`），
# 因为那种写法在正文里大量作为"提一下这份文档"出现，不带路径语义。
DOC_REF_RE = re.compile(r"docs/(?:[A-Za-z0-9_一-鿿.-]+/)*[A-Za-z0-9_一-鿿.-]+\.md")

# 扫这些后缀就够了：markdown 正文 + 代码注释/docstring + 工程配置。
SCAN_SUFFIXES = {".md", ".py", ".js", ".jsx", ".ts", ".tsx", ".toml", ".yml", ".yaml", ".cfg", ".ini"}

EXCLUDE_DIR_PARTS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "worktrees",  # .claude/worktrees —— 其它会话的独立副本，不属于本仓当前树
}

# 这些目录下的文件是**运行产物**（基准/安全脚本落的 JSON），里面记录的
# `baseline_source` 是历史快照，指向当时的路径，不该因为文档改名而被改写。
# 它们不参与检查——但改名后这些历史引用确实会失效，属于"已知代价"。
FROZEN_RESULT_DIRS = {"benchmark_results", "security_results"}

# 本脚本自己的 docstring/注释里写了示例路径，不是真引用。
SELF = "scripts/check_doc_links.py"

# 文件级豁免标记。写在文件任意位置（通常是头部注释）即整份文件不参与检查。
#
# **只给一种文件用：内容本身就是"路径提案"的方案文档**——比如改名映射表里
# 列的是**还不存在的**目标路径，它们是待确认的提案，不是引用。
# 对这种文件，断链检查除了刷屏没有任何意义。
#
# ⚠️ **代价要说清楚**：豁免是整份文件的，所以这份文件里**真正的**断链
# （比如它引用了一份已被删除的现存文档）也会被一并漏掉。
# 因此**不要给普通文档加这个标记**——普通文档里的路径都该是真引用。
IGNORE_FILE_MARKER = "doc-link-check: ignore-file"

# 已知的"看起来像 docs 引用、实际不是"的白名单。
# key = (引用者相对路径, 被引用路径)，value = 为什么它不算断链。
# **加白名单必须写理由**，否则等于把断链检查关掉。
KNOWN_NON_REFS: dict[tuple[str, str], str] = {
    ("tests/unit/test_response_builder.py", "docs/gpt4-usage.md"):
        "单测里伪造的 source_path 字段值，不是仓库文件",
    ("tests/unit/test_multimodal_assembler.py", "docs/readme.md"):
        "单测里伪造的 source_path 字段值，不是仓库文件",
    ("docs/review_2026-08-25/doc_and_collab_practices.md", "docs/current-sprint.md"):
        "调研报告里举的业界命名例子（`docs/current-sprint.md` 之类），不是本仓文件",
    ("docs/orchestration_design.md", "docs/parallel_reasoning_design.md"):
        "已被 orchestration_design.md 合并取代并删除；此处是溯源说明，刻意保留",
    ("docs/orchestration_design.md", "docs/memory_manage_async_decouple_design.md"):
        "已被 orchestration_design.md 合并取代并删除；此处是溯源说明，刻意保留",
    # ↓ api.md 于 2026-08-26 删除（覆盖 7/65 端点、认证一节与实现相反）。
    #   review_codebase_findings.md 是**时点快照**，记录 08-24 当天的事实，
    #   按 CLAUDE.md §7.4「报告类保留但标死日期」刻意不改写——
    #   改写冻结报告等于伪造历史记录。去向说明在 docs/README.md「已删除的文档」。
    ("docs/review_2026-08-24/review_codebase_findings.md", "docs/api.md"):
        "api.md 已于 2026-08-26 删除；该文件是 08-24 的时点快照，刻意不改写。"
        "去向见 docs/README.md「已删除的文档」",
    # ↓ 下面两条是**故意写出已删除/已移动的旧路径**的"去向说明"。
    #   这类路径指向的是 git 历史里的对象或文件的旧位置，不是对当前工作区文件的引用。
    #   删掉它们会让"东西去哪了"这个问题无处可查，所以刻意保留。
    ("docs/README.md", "docs/api.md"):
        "「已删除的文档」表里的 git 取回命令 `git show 7eaff77:docs/api.md`，"
        "指向历史版本而非当前文件，必须保持可复制粘贴",
    ("tests/fixtures/prompt_injection/README.md", "docs/test_upload_doc/后勤资料文档.md"):
        "夹具 README 里说明「原位置在哪」，指向 2026-08-26 移动前的旧路径",
}


def iter_scan_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in SCAN_SUFFIXES:
            continue
        parts = set(path.relative_to(REPO_ROOT).parts)
        if parts & EXCLUDE_DIR_PARTS:
            continue
        if parts & FROZEN_RESULT_DIRS:
            continue
        files.append(path)
    return sorted(files)


def collect_refs() -> tuple[dict[str, list[tuple[str, int]]], list[str]]:
    """返回 ({被引用的 docs 路径: [(引用者相对路径, 行号), ...]}, [被豁免的文件])。

    豁免列表要一起返回并**始终打印出来**——被静默跳过的检查等于没有检查，
    这正是 `CLAUDE.md` §7.4 说的"标着已实现的废弃文档比没有文档更危险"的同款陷阱。
    """
    refs: dict[str, list[tuple[str, int]]] = {}
    exempted: list[str] = []
    for path in iter_scan_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "docs/" not in text:
            continue
        rel = str(path.relative_to(REPO_ROOT))
        if rel == SELF:
            continue
        if IGNORE_FILE_MARKER in text:
            exempted.append(rel)
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in DOC_REF_RE.findall(line):
                if (rel, match) in KNOWN_NON_REFS:
                    continue
                refs.setdefault(match, []).append((rel, lineno))
    return refs, sorted(exempted)


def find_orphans(refs: dict[str, list[tuple[str, int]]]) -> list[str]:
    """docs/ 下没有被任何带路径引用命中的 .md。"""
    docs_dir = REPO_ROOT / "docs"
    if not docs_dir.is_dir():
        return []
    referenced = set(refs)
    orphans = []
    for md in sorted(docs_dir.rglob("*.md")):
        rel = str(md.relative_to(REPO_ROOT))
        if rel not in referenced:
            orphans.append(rel)
    return orphans


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--orphans", action="store_true", help="额外列出没有被引用的文档")
    parser.add_argument("--json", dest="as_json", action="store_true", help="机器可读输出")
    args = parser.parse_args()

    refs, exempted = collect_refs()
    broken: dict[str, list[tuple[str, int]]] = {}
    for target, sites in sorted(refs.items()):
        if not (REPO_ROOT / target).exists():
            broken[target] = sites

    orphans = find_orphans(refs) if args.orphans else []

    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=False,
        ).stdout.strip()
    except OSError:
        head = "unknown"

    if args.as_json:
        print(json.dumps(
            {
                "commit": head,
                "scanned_files": len(iter_scan_files()),
                "distinct_targets": len(refs),
                "total_reference_sites": sum(len(v) for v in refs.values()),
                "broken": {k: [f"{f}:{n}" for f, n in v] for k, v in broken.items()},
                "orphans": orphans,
                "exempted_files": exempted,
            },
            ensure_ascii=False, indent=2,
        ))
        return 1 if broken else 0

    print(f"文档链接体检 @ {head}")
    print(f"  扫描文件      : {len(iter_scan_files())}")
    print(f"  被引用的文档  : {len(refs)} 份")
    print(f"  引用点总数    : {sum(len(v) for v in refs.values())}")
    if exempted:
        print(f"  ⚠️ 整份豁免    : {len(exempted)} 份（含 `{IGNORE_FILE_MARKER}` 标记，"
              f"其中真断链也查不出来）")
        for e in exempted:
            print(f"      {e}")
    print()

    if broken:
        print(f"❌ 断链 {len(broken)} 处：")
        for target, sites in broken.items():
            print(f"  {target}  ← 被 {len(sites)} 处引用")
            for f, n in sites:
                print(f"      {f}:{n}")
    else:
        print("✅ 无断链：所有带路径的 docs 引用都能找到目标文件。")

    if args.orphans:
        print()
        print(f"孤儿文档（没有任何带路径引用指向它）{len(orphans)} 份：")
        for o in orphans:
            print(f"  {o}")
        print("  注：孤儿不等于该删 —— CLAUDE.md §8 的索引表是它们的正式入口。")

    print()
    print("本次未覆盖：① 文档内的锚点（`#小节`）是否存在；② 裸文件名引用"
          "（正文里写 `role.md` 而不写路径）；③ 历史 commit message 里的路径；"
          "④ scripts/{benchmark,security}_results/ 里的运行产物（刻意冻结）。")

    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
