"""训练集/评估集数据本身的回归测试（`docs/chitchat_intent_design.md` §5-⑥⑦）。

跟 `scripts/check_router_lora_dedup.py`（pre-commit 用）不是同一件事：
那个脚本只查重叠，这里额外守 schema 完整性和五类配比的硬性下限——防止
有人后续手改 `.jsonl` 文件（比如直接删掉几条 chitchat 样本"腾地方"）时，
悄悄破坏了 §5-⑥ 拍板的"任何一类不超过 25%"这条约束却没人发现。

在旧状态下会不会失败：**会**——`router_lora_data/train_batch1.jsonl` 和
`tests/fixtures/router_eval.jsonl` 都是本次新增文件，旧代码库里这两个
路径不存在，本文件全部用例都会因为 `FileNotFoundError`/空数据集失败。

⚠️ 落仓路径是 `router_lora_data/`（仓库根目录），不是设计文档 §5-⑦ 拍板
文字里写的 `data/router_lora/`——本机 `.gitignore` 把 `data/` 整体忽略
（`.git/info/exclude` 里还有一层 worktree 共享的重复忽略），塞进去的文件
会被 git 悄悄当成不存在，重蹈"脚本/数据丢失"的覆辙。改用的 `router_lora_data/`
正是 CLAUDE.md 里原本就提到过的历史路径名，见 `scripts/gen_router_training_data.py`
顶部的完整说明。
"""

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TRAIN_PATH = REPO_ROOT / "router_lora_data" / "train_batch1.jsonl"
EVAL_PATH = REPO_ROOT / "tests" / "fixtures" / "router_eval.jsonl"

VALID_INTENT_TYPES = {"clarify", "rag", "tool", "workflow", "chitchat"}
REQUIRED_FIELDS = {
    "query", "rewritten_query", "sub_queries", "intent_type",
    "target_tool", "workflow_type", "need_clarify", "confidence", "reasoning",
}


def _load_jsonl(path: Path):
    assert path.exists(), f"{path} 不存在——先跑一遍 scripts/gen_router_training_data.py"
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


@pytest.fixture(scope="module")
def train_records():
    return _load_jsonl(TRAIN_PATH)


@pytest.fixture(scope="module")
def eval_records():
    return _load_jsonl(EVAL_PATH)


class TestSchema:
    def test_train_records_have_required_fields(self, train_records):
        for r in train_records:
            missing = REQUIRED_FIELDS - set(r.keys())
            assert not missing, f"{r.get('query')!r} 缺字段: {missing}"

    def test_eval_records_have_required_fields(self, eval_records):
        for r in eval_records:
            missing = REQUIRED_FIELDS - set(r.keys())
            assert not missing, f"{r.get('query')!r} 缺字段: {missing}"

    @pytest.mark.parametrize("which", ["train", "eval"])
    def test_intent_type_is_valid(self, which, train_records, eval_records):
        records = train_records if which == "train" else eval_records
        for r in records:
            assert r["intent_type"] in VALID_INTENT_TYPES, (
                f"{r['query']!r} 的 intent_type={r['intent_type']!r} 不在五分类范围内"
            )

    def test_no_empty_queries(self, train_records, eval_records):
        for r in train_records + eval_records:
            assert r["query"].strip(), "训练/评估数据里不许有空 query"


class TestClassBalance:
    """拍板点 §5-⑥：五类各 15%~25%，任何一类不超过 25%（针对训练集，
    评估集刻意不做这条约束——评估集的 chitchat 占比故意偏高，用来集中
    暴露开放闲聊的缺口，不是拿来训练用的，不受同一条配比约束）。"""

    def test_each_class_within_15_to_25_percent(self, train_records):
        total = len(train_records)
        counts = {}
        for r in train_records:
            counts[r["intent_type"]] = counts.get(r["intent_type"], 0) + 1
        assert set(counts.keys()) == VALID_INTENT_TYPES, "训练集必须五类都覆盖到，一类都不能少"
        for intent_type, n in counts.items():
            pct = n / total
            assert 0.15 <= pct <= 0.25, (
                f"{intent_type} 占比 {pct:.1%}，超出 15%~25% 的拍板范围"
            )

    def test_chitchat_open_subcategory_at_least_half(self, train_records):
        """开放闲聊 >= chitchat 总数的 50%（设计文档 §2.3-3c ③ 表格）。
        本测试重新按 reasoning 文本模式识别子类，不依赖生成脚本内部的
        `_subcategory` 字段（那个字段已被生成脚本在落盘前剥离，落盘文件
        里查不到）——改用 reasoning 里的关键词做等价判断。"""
        chitchat = [r for r in train_records if r["intent_type"] == "chitchat"]
        assert chitchat, "训练集里没有任何 chitchat 样本"
        open_like = [r for r in chitchat if "开放闲聊" in r.get("reasoning", "")]
        assert len(open_like) / len(chitchat) >= 0.5, (
            f"开放闲聊只占 chitchat 的 {len(open_like) / len(chitchat):.1%}，"
            "低于拍板要求的 50%"
        )

    def test_hard_negatives_at_least_20_percent_of_chitchat(self, train_records):
        chitchat_count = sum(1 for r in train_records if r["intent_type"] == "chitchat")
        hard_negative_count = sum(
            1 for r in train_records if "寒暄壳子" in r.get("reasoning", "")
        )
        assert chitchat_count, "训练集里没有任何 chitchat 样本"
        assert hard_negative_count / chitchat_count >= 0.2, (
            f"hard negative 只占 chitchat 的 {hard_negative_count / chitchat_count:.1%}，"
            "低于拍板要求的 20%（会重演根因②的镜像偏斜）"
        )


class TestTrainEvalDisjoint:
    """训练集与评估集不得有重叠句子——精确匹配 + 归一化匹配都要查
    （设计文档 §4.5 ①）。复用 `scripts/check_router_lora_dedup.py` 里
    同一份校验逻辑，不是重写一份可能悄悄跑偏的平行实现。"""

    def test_no_exact_overlap(self, train_records, eval_records):
        train_q = {r["query"] for r in train_records}
        eval_q = {r["query"] for r in eval_records}
        overlap = train_q & eval_q
        assert not overlap, f"训练集与评估集精确重叠: {overlap}"

    def test_no_normalized_overlap(self, train_records, eval_records):
        from scripts.check_router_lora_dedup import _normalize

        train_norm = {_normalize(r["query"]) for r in train_records}
        eval_norm = {_normalize(r["query"]) for r in eval_records}
        overlap = train_norm & eval_norm
        assert not overlap, f"训练集与评估集归一化后重叠: {overlap}"

    def test_no_duplicate_within_train(self, train_records):
        queries = [r["query"] for r in train_records]
        assert len(queries) == len(set(queries)), "训练集内部有精确重复的 query"


class TestEvalHoldoutCoversKnownGaps:
    """评估集必须覆盖训练集没有强调的措辞形态（设计文档 §4.5 ②）——
    不是"不同句子"，是"不同类型"。这条测试钉住"评估集不会退化成训练集的
    简单改写"这件事本身。"""

    def test_eval_covers_english_chitchat(self, eval_records):
        assert any(
            r["intent_type"] == "chitchat" and r["query"].isascii()
            for r in eval_records
        ), "评估集里没有英文闲聊样本"

    def test_eval_covers_punctuation_only(self, eval_records):
        import re

        assert any(
            r["intent_type"] == "chitchat" and not re.search(r"[\w一-鿿]", r["query"])
            for r in eval_records
        ), "评估集里没有纯表情/标点闲聊样本"

    def test_eval_covers_multiple_intent_types_not_just_chitchat(self, eval_records):
        """评估集不能只测 chitchat——五类回归都要覆盖，否则测不出"重训后
        模型把带业务词的句子判成 chitchat"这类跨类污染（R2 风险）。"""
        types_covered = {r["intent_type"] for r in eval_records}
        assert types_covered == VALID_INTENT_TYPES
