"""pre-commit 钩子：校验 `router_lora_data/*.jsonl` 训练集与
`tests/fixtures/router_eval.jsonl` 评估集互不重叠（精确匹配 + 归一化匹配）。

对应 `docs/chitchat_intent_design.md` §4.5 ①/§5-⑦：根因②教训的核心是
"测的和训的是同一个分布"从来没有机制拦住——这次改成有机制：训练集与
评估集一旦出现重叠句子，提交直接失败，不靠人肉记得去查。

归一化口径与 `scripts/gen_router_training_data.py::_normalize_for_dedup`
（以及 `intent.py::_normalize_chitchat_token`）保持一致——三处都写的是
同一段逻辑（去空白/去标点/剥语气词/转小写），这是刻意的重复而不是共享
一个 import：`gen_router_training_data.py` 是离线生成脚本，这里是提交时
的把关脚本，两者的生命周期和失败处理方式不同（一个是"生成失败就不写盘"，
一个是"提交失败就拒绝 commit"），保持各自独立不容易在改一处时忘了改
另一处对提交流程的影响。

用法（本地手动跑）：
    .venv/bin/python scripts/check_router_lora_dedup.py

接入 pre-commit：见仓库根目录 `.pre-commit-config.yaml`（`language: system`，
直接调用本脚本，不需要额外安装 hook 依赖；但 pre-commit 框架本身
**需要先 `pip install pre-commit && pre-commit install`**——本仓库此前没有
引入过 pre-commit，这一步是新增的，见交付报告"什么没做"）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List, Set

REPO_ROOT = Path(__file__).resolve().parent.parent
TRAIN_DIR = REPO_ROOT / "router_lora_data"
EVAL_PATH = REPO_ROOT / "tests" / "fixtures" / "router_eval.jsonl"


def _normalize(text: str) -> str:
    strip_chars = " \t\r\n，,。.！!？?～~、；;：:…—-_\"'“”‘’（）()【】[]"
    tail_particles = ("呀", "啊", "哈", "嘞", "咯", "啦", "喔", "噢", "嘛", "唷", "耶")
    token = "".join((text or "").split()).strip(strip_chars).lower()
    while len(token) > 1 and token.endswith(tail_particles):
        token = token[:-1].strip(strip_chars)
    return token


def _load_queries(path: Path) -> Set[str]:
    if not path.exists():
        return set()
    out = set()
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            out.add(row["query"])
    return out


def main() -> int:
    if not EVAL_PATH.exists():
        print(f"[skip] {EVAL_PATH} 不存在，无需检查")
        return 0
    if not TRAIN_DIR.exists():
        print(f"[skip] {TRAIN_DIR} 不存在，无需检查")
        return 0

    eval_queries = _load_queries(EVAL_PATH)
    train_files = sorted(TRAIN_DIR.glob("*.jsonl"))
    if not train_files:
        print(f"[skip] {TRAIN_DIR} 下没有 .jsonl 文件")
        return 0

    train_queries: Dict[str, str] = {}  # query -> 所在文件
    problems: List[str] = []
    for fp in train_files:
        for q in _load_queries(fp):
            if q in train_queries and train_queries[q] != fp.name:
                problems.append(f"训练集内部跨文件重复: {q!r}（{train_queries[q]} 与 {fp.name}）")
            train_queries[q] = fp.name

    exact_overlap = set(train_queries) & eval_queries
    for q in sorted(exact_overlap):
        problems.append(f"精确重叠: {q!r}（训练集 {train_queries[q]} 与评估集同时出现）")

    train_norm = {_normalize(q): (q, fname) for q, fname in train_queries.items()}
    eval_norm = {_normalize(q): q for q in eval_queries}
    for norm_key, eval_q in eval_norm.items():
        if norm_key in train_norm:
            train_q, fname = train_norm[norm_key]
            if train_q != eval_q:  # 精确重叠已经报过了，这里只报"看起来不同、归一化后相同"的
                problems.append(
                    f"归一化后重叠: 训练集 {fname} 的 {train_q!r} 与评估集 {eval_q!r}"
                )

    if problems:
        print(f"[FAIL] 训练集/评估集去重校验未通过（{len(problems)} 处）：")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(f"[OK] 训练集（{len(train_queries)} 条，{len(train_files)} 个文件）"
          f"与评估集（{len(eval_queries)} 条）互不重叠")
    return 0


if __name__ == "__main__":
    sys.exit(main())
