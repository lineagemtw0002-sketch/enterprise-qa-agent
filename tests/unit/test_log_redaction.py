"""日志脱敏测试 —— 可观测性方案 §2.4 / T-1 / T-12。

被测的是 `src/observability/redact.py` 的纯函数 `redact()`，以及它挂在
logging handler 上之后的端到端效果。

判别力说明（`CLAUDE.md` §7.2 要求每条测试都回答"它在修复前会失败吗"）：

- `redact()` 是新功能，**旧实现下没有这个函数**，所有 T-1 用例天然会失败。
- 但"新功能所以必然通过"是个陷阱：这组测试真正要拦的 bug 是**漏字段**——
  分级表里忘了登记某个内容字段，它就原样进了日志。所以每个用例除了断言
  `_len` / `_sha256` 出现，还必须断言**整串 JSON 里查不到原文**
  （`test_*_not_anywhere_in_output`），逐字段断言抓不到这类漏。
- `TestLtmStoreLeakFixed` 直接对着 `ltm_store.py` 修复前的形态断言，
  那条在修复前 100% 会失败。
"""

from __future__ import annotations

import json
import logging

import pytest

from src.observability.redact import (
    S0,
    S1,
    S2,
    S2_PLUS,
    classify_field,
    hash_value,
    redact,
    sensitive_digest,
)

SECRET_QUERY = "张伟的年薪是多少，他在哪个部门"
SECRET_CHUNK = "公司 2026 年度调薪方案：技术序列 P7 基准年薪 78 万元。"
SECRET_PROMPT = "你是企业知识助手。以下是检索到的资料：" + SECRET_CHUNK


# ── T-1：分级判定 ───────────────────────────────────────────────────


class TestClassify:
    def test_ids_and_counters_are_s0(self) -> None:
        for name in ("request_id", "task_id", "result_count", "duration_ms", "top_k"):
            assert classify_field(name, 1) == S0, name

    def test_quasi_identifiers_are_s1(self) -> None:
        for name in ("org_id", "user_id", "collection", "chunk_ids", "source"):
            assert classify_field(name, "x") == S1, name

    def test_content_fields_are_s2(self) -> None:
        for name in ("query", "answer", "text", "summary", "thought", "raw_content"):
            assert classify_field(name, "x") == S2, name

    def test_prompt_is_s2_plus(self) -> None:
        for name in ("prompt", "final_prompt", "system_prompt", "buffer_preview"):
            assert classify_field(name, "x") == S2_PLUS, name

    def test_unknown_string_field_defaults_to_s2(self) -> None:
        """风险 R3 的直接对策：**没登记过的字符串字段一律当内容处理**。

        这条拦的是"三个月后有人加了 `extra={"user_note": ...}` 但忘了分级"。
        宁可把一个无害字段哈希掉，也不要漏一个内容字段。
        """
        assert classify_field("some_brand_new_field", "任意文本") == S2

    def test_unknown_numeric_field_defaults_to_s0(self) -> None:
        """数值/布尔不承载内容，默认放行——否则每个新计数器都要先登记才可用。"""
        assert classify_field("some_brand_new_field", 42) == S0
        assert classify_field("some_brand_new_flag", True) == S0

    def test_unknown_list_of_strings_is_s2(self) -> None:
        """容器默认递归，但**列表里直接躺着字符串**必须当内容处理。

        递归对标量是无操作的——放行 `notes: ["用户说他要离职"]` 就等于漏。
        这条拦的是一个很容易写出来的实现：'是容器就递归'。
        """
        assert classify_field("notes", ["用户说他要离职"]) == S2
        assert classify_field("retry_delays", [1, 2, 3]) == S0

    def test_unknown_name_suffix_is_s2(self) -> None:
        """`file_name` 这类是用户上传的文件名——是内容，不是标识符。"""
        assert classify_field("upload_file_name", "2026年薪酬方案.xlsx") == S2

    def test_suffix_rule_covers_derived_fields(self) -> None:
        """`_len` / `_sha256` 自身必须是 S0，否则 redact() 就不幂等了。"""
        assert classify_field("query_len", 12) == S0
        assert classify_field("query_sha256", "abc") == S0


# ── T-1：redact() 本体 ──────────────────────────────────────────────


class TestRedactDefaults:
    def test_s2_becomes_len_and_hash(self) -> None:
        out = redact({"query": SECRET_QUERY})
        assert out == {
            "query_len": len(SECRET_QUERY),
            "query_sha256": hash_value(SECRET_QUERY),
        }
        assert len(out["query_sha256"]) == 12

    def test_secret_not_anywhere_in_output(self) -> None:
        """兜底断言：整串查，不逐字段查。逐字段查抓不到"漏了某个字段"。"""
        out = redact({
            "query": SECRET_QUERY,
            "answer": SECRET_CHUNK,
            "chunks": [{"text": SECRET_CHUNK, "chunk_id": "c-1"}],
            "thought": "我需要先查薪酬库",
        })
        blob = json.dumps(out, ensure_ascii=False)
        assert SECRET_QUERY not in blob
        assert SECRET_CHUNK not in blob
        assert "薪酬库" not in blob

    def test_s0_and_s1_pass_through(self) -> None:
        out = redact({
            "request_id": "abc123",
            "org_id": "org_acme",
            "collection": "acme_hr",
            "chunk_ids": ["c-1", "c-2"],
            "result_count": 3,
            "duration_ms": 42.5,
        })
        assert out["request_id"] == "abc123"
        assert out["org_id"] == "org_acme"
        assert out["collection"] == "acme_hr"
        assert out["chunk_ids"] == ["c-1", "c-2"]
        assert out["result_count"] == 3

    def test_nested_payload_is_redacted(self) -> None:
        """内容常常藏在一层结构里（`extra={"payload": {...}}`），必须递归。"""
        out = redact({"payload": {"query": SECRET_QUERY, "top_k": 5}})
        assert out["payload"]["top_k"] == 5
        assert SECRET_QUERY not in json.dumps(out, ensure_ascii=False)

    def test_list_of_dicts_is_redacted(self) -> None:
        out = redact({
            "stages": [
                {"stage": "rerank", "elapsed_ms": 12, "text": SECRET_CHUNK},
            ]
        })
        assert out["stages"][0]["stage"] == "rerank"
        assert SECRET_CHUNK not in json.dumps(out, ensure_ascii=False)

    def test_arg_dict_keeps_only_keys(self) -> None:
        """`subgraph.py` 记 `{"args": {...}}` 全值——对 query_knowledge_hub
        而言那个值就是用户的问题。只留键名。"""
        out = redact({"args": {"query": SECRET_QUERY, "collection_name": "acme_hr"}})
        assert out == {"args_keys": ["collection_name", "query"]}
        assert SECRET_QUERY not in json.dumps(out, ensure_ascii=False)

    def test_none_content_is_not_hashed_as_string_none(self) -> None:
        """`None` 与字符串 "None" 必须能区分开。"""
        out = redact({"raw_content": None})
        assert out == {"raw_content_len": 0, "raw_content_sha256": None}

    def test_is_idempotent(self) -> None:
        """脱敏挂在多个 handler 上会被重复执行，重复执行必须无副作用。"""
        once = redact({"query": SECRET_QUERY, "org_id": "org_acme"})
        twice = redact(once)
        assert once == twice

    def test_does_not_mutate_input(self) -> None:
        event = {"query": SECRET_QUERY}
        redact(event)
        assert event == {"query": SECRET_QUERY}  # 纯函数

    def test_same_content_same_hash(self) -> None:
        """hash 的用处正在于此：用户把原文给你，你 hash 一遍就能 grep 到请求。"""
        a = redact({"query": SECRET_QUERY})["query_sha256"]
        b = redact({"question": SECRET_QUERY})["question_sha256"]
        assert a == b == hash_value(SECRET_QUERY)


class TestLogContentSwitch:
    def test_switch_on_reveals_s2(self) -> None:
        out = redact({"query": SECRET_QUERY}, log_content=True)
        assert out == {"query": SECRET_QUERY}

    def test_switch_never_reveals_s2_plus(self) -> None:
        """**本组最重要的一条。**

        最终 prompt = 系统提示词 + chunk 原文 + 历史对话 + LTM 事实，是系统里
        敏感度最高的单个字符串；`docs/security_prompt_injection_test_report.md`
        已确认系统提示词泄露是真实问题。所以它**没有任何开关能打开**。
        """
        out = redact(
            {"prompt": SECRET_PROMPT, "buffer_preview": SECRET_PROMPT},
            log_content=True,
        )
        blob = json.dumps(out, ensure_ascii=False)
        assert SECRET_PROMPT not in blob
        assert SECRET_CHUNK not in blob
        assert out["prompt_sha256"] == hash_value(SECRET_PROMPT)
        assert out["prompt_len"] == len(SECRET_PROMPT)

    def test_switch_on_still_allows_reconstruction_fields(self) -> None:
        """不记原文 ≠ 不能重建：模板在 git、chunk 在库、历史在 messages 表。"""
        out = redact({
            "prompt": SECRET_PROMPT,
            "prompt_template_version": "v3",
            "chunk_ids": ["c-1", "c-2"],
        })
        assert out["prompt_template_version"] == "v3"
        assert out["chunk_ids"] == ["c-1", "c-2"]
        assert out["prompt_sha256"]


class TestStrictMode:
    def test_strict_downgrades_s1(self) -> None:
        """无 org 归属的日志（`_unassigned`）按最严处理——"不知道属于谁"的
        日志最危险，没有 org 边界可以约束谁能读它。"""
        out = redact({"user_id": "u-42", "collection": "acme_hr"}, strict=True)
        assert "user_id" not in out
        assert out["user_id_sha256"] == hash_value("u-42")
        assert "acme_hr" not in json.dumps(out, ensure_ascii=False)

    def test_strict_keeps_s0(self) -> None:
        out = redact({"request_id": "abc", "duration_ms": 12}, strict=True)
        assert out["request_id"] == "abc"


# ── T-12：端到端兜底 ────────────────────────────────────────────────


class TestEndToEndThroughLoggingPipeline:
    """走完整的 filter → formatter 链路，断言**渲染后的字符串**里没有原文。

    这是 §2.4 全部策略的兜底断言，比逐字段测更抗"未来新增字段"。
    """

    def test_no_plaintext_in_rendered_json(self, capture_json_logs) -> None:
        logs = capture_json_logs()
        logger = logging.getLogger("test.redaction.e2e")
        logger.info(
            "retrieve done",
            extra={
                "event": "retrieve.knowledge_retrieval.success",
                "query": SECRET_QUERY,
                "chunks": [{"chunk_id": "c-1", "text": SECRET_CHUNK, "score": 0.87}],
                "result_count": 1,
            },
        )
        blob = logs.blob
        assert SECRET_QUERY not in blob
        assert SECRET_CHUNK not in blob

        record = logs.records[-1]
        assert record["result_count"] == 1
        assert record["query_len"] == len(SECRET_QUERY)
        assert record["chunks"][0]["chunk_id"] == "c-1"  # S1 保留，能顺着 id 去库里查

    def test_content_switch_visible_end_to_end(self, capture_json_logs) -> None:
        logs = capture_json_logs(log_content=True)
        logging.getLogger("test.redaction.e2e2").info(
            "retrieve done", extra={"query": SECRET_QUERY, "prompt": SECRET_PROMPT}
        )
        blob = logs.blob
        assert SECRET_QUERY in blob        # S2 被开关放行
        assert SECRET_PROMPT not in blob   # S2+ 任何开关下都不放行


# ── D 类敏感泄漏的定点回归 ─────────────────────────────────────────


class TestLtmStoreLeakFixed:
    """`ltm_store.py` 的模型原始输出泄漏（设计文档 §2.6 D 类第 1 处）。

    修复前那行是：
        print(f"[LTM] extract_facts failed: {e!r}; raw content={content!r}")
    `content` 是模型抽取出的**长期记忆事实原文**（姓名、职位、请假原因），
    直接进 stdout 就是落盘一份用户画像。**这条测试在修复前必然失败。**
    """

    def test_extract_facts_failure_logs_no_content(self, capture_json_logs) -> None:
        import asyncio

        from src.ragent_backend.ltm_store import LTMStore

        leaked = "用户张伟是财务部经理，2026 年 3 月因家人住院请过长假"

        class _BadLLM:
            async def ainvoke(self, messages):  # noqa: ANN001
                class _R:
                    content = leaked
                return _R()

        logs = capture_json_logs()
        store = LTMStore()
        # 模型返回的不是 JSON → 走 except 分支，也就是原来会打印 content 的那条路
        facts = asyncio.run(store.extract_facts("张伟是谁", "他是财务部经理", _BadLLM()))

        assert facts == []
        blob = logs.blob
        assert leaked not in blob
        assert "张伟" not in blob
        assert "财务部" not in blob

        record = logs.records[-1]
        assert record["event"] == "ltm.extract_facts.failed"
        assert record["raw_content_len"] == len(leaked)
        assert len(record["raw_content_sha256"]) == 12

    def test_digest_helper_shape(self) -> None:
        out = sensitive_digest("raw_content", "abc")
        assert out == {"raw_content_len": 3, "raw_content_sha256": hash_value("abc")}
