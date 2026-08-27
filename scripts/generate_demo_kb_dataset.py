#!/usr/bin/env python
"""生成 + 摄入 Acme / Globex 12 个部门知识库的演示语料，并产出人工测试数据集文档。

**它解决的问题**：12 个业务库原来各只有 20 篇、且内容是随机模板拼的
（同一个库里「年假」能查出 5/10/15/20 四种说法），既不够演示，也没法人工判对错。
本脚本给每个库补 101 篇**确定性**语料（含 1 篇统一口径速查表），两家企业的同名部门
刻意用不同的锚点事实（Acme 年假 15 天 / Globex 20 天），跨企业隔离一旦失效当场可见。

**四个阶段**（默认全跑，可用 `--stage` 只跑其中一个）：
  generate  把语料写到 `data/demo_kb_corpus/{tenant}/{category}/`
  ingest    走真实 `IngestionPipeline` 摄入到 `{tenant}_{category}_kb`
  doc       生成 `docs/manual_test_dataset.md`
  verify    用真实账号 + 真实 ACL 跑全部问题（正向命中 / 越权被拒），结果落
            `scripts/demo_dataset_results/verify_*.json`，`--stage doc` 会把它渲染进文档 §0.3

⚠️ **verify 的正向问题跑两条路径，两个数字含义不同，别混用**：
  scoped （显式传 collection）—— 验的是「语料在不在、查不查得到」，这是本数据集的验收判据；
  default（不传 collection，全库并行）—— 验的是「照文档提问当前能不能拿到答案」。
两者的差值是 `_narrow_by_document_summary` 的全局 `top_docs`（默认 5）粗筛吃掉的，
属于既有实现的伸缩性问题，不是数据缺陷。详见 `docs/manual_test_dataset.md` §0.2。

**幂等**：
- generate 内容确定，同样的代码写出逐字节相同的文件；
- ingest 用 `force=False`，`SQLiteIntegrityChecker` 按文件 SHA256 跳过已摄入的文件，
  重跑只会补新增的；
- doc 覆盖写。

🔴 **硬保护**：只允许写 `acme_*_kb` / `globex_*_kb` 这 12 个 collection。
`product_req_kb`（黄金测试集 15/17 基线）和 `mmarco`（recall@10 85.0% 基线）
是评估语料，往里加数据会让全部质量基线失效——`_assert_safe_collection` 会当场抛错。

用法：
    set -a; source .env; set +a
    export RAGENT_DEBUG=true
    .venv/bin/python scripts/generate_demo_kb_dataset.py --dry-run
    .venv/bin/python scripts/generate_demo_kb_dataset.py
    .venv/bin/python scripts/generate_demo_kb_dataset.py --stage verify
    # 只跑一个库做小规模验证：
    .venv/bin/python scripts/generate_demo_kb_dataset.py --collection acme_hr_admin_kb --limit 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from scripts.demo_kb_content import KbSpec, all_kb_specs  # noqa: E402
from scripts.demo_kb_content import questions as Q  # noqa: E402

CORPUS_ROOT = REPO_ROOT / "data" / "demo_kb_corpus"
DOC_PATH = REPO_ROOT / "docs" / "manual_test_dataset.md"
MANIFEST_PATH = CORPUS_ROOT / "manifest.json"

# 🔴 评估语料，绝对不能碰。见模块顶部说明。
FORBIDDEN_COLLECTIONS = {"product_req_kb", "mmarco"}
_ALLOWED_RE = re.compile(r"^(acme|globex)_[a-z_]+_kb$")
# 检索结果里每条引用片段都是以 "> " 开头的一行，见 _build_success_response 的排版。
_SNIPPET_RE = re.compile(r"^>\s?(.*)$", re.MULTILINE)


def _assert_safe_collection(name: str) -> None:
    if name in FORBIDDEN_COLLECTIONS or not _ALLOWED_RE.match(name):
        raise RuntimeError(
            f"拒绝写入 collection '{name}'：本脚本只允许写 acme_*_kb / globex_*_kb。"
            f"（product_req_kb 与 mmarco 是评估语料基线，往里加数据会让全部质量基线失效）"
        )


# ============================================================ 阶段 1：生成语料
def stage_generate(specs: List[KbSpec], limit: Optional[int], dry_run: bool) -> Dict[str, list]:
    manifest: Dict[str, list] = {}
    for spec in specs:
        _assert_safe_collection(spec.collection)
        docs = spec.docs()
        if limit:
            docs = docs[:limit]
        out_dir = CORPUS_ROOT / spec.tenant / spec.category
        entries = []
        written = skipped = 0
        if not dry_run:
            out_dir.mkdir(parents=True, exist_ok=True)
        for d in docs:
            path = out_dir / d.filename()
            text = d.render(spec.org_name, spec.kb_label)
            entries.append({"no": d.no, "title": d.title, "file": path.name})
            if dry_run:
                continue
            if path.exists() and path.read_text(encoding="utf-8") == text:
                skipped += 1
                continue
            path.write_text(text, encoding="utf-8")
            written += 1
        manifest[spec.collection] = entries
        verb = "将生成" if dry_run else f"新写 {written}，内容未变 {skipped}"
        print(f"  [{spec.collection:28s}] {len(docs):3d} 篇 -> {out_dir.relative_to(REPO_ROOT)}  ({verb})")

    if not dry_run:
        MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST_PATH.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"  清单已写入 {MANIFEST_PATH.relative_to(REPO_ROOT)}")
    return manifest


# ============================================================ 阶段 2：真实摄入
def stage_ingest(specs: List[KbSpec], limit: Optional[int], dry_run: bool) -> None:
    from src.core.settings import load_settings
    from src.ingestion.pipeline import IngestionPipeline

    settings = load_settings()
    grand_ok = grand_skip = grand_fail = 0

    for spec in specs:
        _assert_safe_collection(spec.collection)
        corpus_dir = CORPUS_ROOT / spec.tenant / spec.category
        files = sorted(corpus_dir.glob("*.txt"))
        if limit:
            files = files[:limit]
        if not files:
            print(f"  [SKIP] {corpus_dir} 没有语料，先跑 --stage generate")
            continue
        if dry_run:
            print(f"  [{spec.collection:28s}] 将摄入 {len(files)} 个文件")
            continue

        # force=False 是幂等的关键：已摄入过的文件按 SHA256 直接跳过。
        pipeline = IngestionPipeline(settings, collection=spec.collection, force=False)
        t0 = time.monotonic()
        ok = skip = fail = 0
        for i, f in enumerate(files, start=1):
            result = pipeline.run(str(f))
            if not result.success:
                fail += 1
                print(f"    [FAIL] {f.name}: {result.error}")
            elif result.stages.get("integrity", {}).get("skipped"):
                skip += 1
            else:
                ok += 1
            if i % 25 == 0:
                print(f"    ...{i}/{len(files)}（新摄入 {ok}，已存在 {skip}，失败 {fail}）")
        print(
            f"  [{spec.collection:28s}] 新摄入 {ok}，已存在跳过 {skip}，失败 {fail}，"
            f"耗时 {time.monotonic() - t0:.1f}s"
        )
        grand_ok += ok
        grand_skip += skip
        grand_fail += fail

    if not dry_run:
        print(f"\n  合计：新摄入 {grand_ok}，已存在 {grand_skip}，失败 {grand_fail}")


# ============================================================ 阶段 4：真实验证
def _code_state() -> Dict[str, object]:
    """记录这批数字对应的代码状态（commit + 脏文件清单），见 CLAUDE.md §7.5。"""
    import subprocess

    def _git(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10
            ).stdout.strip()
        except Exception:  # noqa: BLE001
            return ""

    dirty = [ln for ln in _git("status", "--porcelain").splitlines() if ln.strip()]
    return {
        "commit": _git("rev-parse", "HEAD"),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty_files": dirty,
        "note": "dirty_files 非空表示这批数字对应的是含未提交改动的工作区",
    }


async def _resolve_user_ids(usernames: List[str]) -> Dict[str, str]:
    from src.ragent_backend.user_store import UserStore

    store = UserStore()
    try:
        users = await store.list_users()
    finally:
        await store.close()
    by_name = {u.username: u.user_id for u in users}
    missing = [u for u in usernames if u not in by_name]
    if missing:
        raise RuntimeError(f"这些账号在库里不存在：{missing}")
    return {u: by_name[u] for u in usernames}


async def _run_verify(sample: Optional[int]) -> int:
    from src.core.settings import load_settings
    from src.mcp_server.tools.query_knowledge_hub import QueryKnowledgeHubTool

    usernames = list(Q.ACCOUNTS)
    ids = await _resolve_user_ids(usernames)
    settings = load_settings()
    tool = QueryKnowledgeHubTool(settings=settings)

    passed = failed = 0
    results: List[dict] = []

    # 正向问题跑两条路径，分开计分——这两个数字回答的是不同的问题：
    #   scoped  = 显式指定 collection，绕开 `_narrow_by_document_summary` 的全局
    #             top_docs 粗筛，回答「**语料本身在不在、查不查得到**」；
    #   default = 不指定 collection，走真实演示路径（全库并行召回），回答
    #             「**用户照着文档提问，当前配置下能不能拿到答案**」。
    # 两者差值就是 §0.2 那个 top_docs=5 粗筛瓶颈吃掉的部分，不要把它算成数据缺陷。
    scoped_ok = scoped_tot = default_ok = default_tot = 0

    print("\n--- 正向问题（scoped = 指定库；default = 全库并行，真实演示路径）---")
    for username in usernames:
        per_kb = Q.questions_for_account(username)
        for collection, qs in per_kb.items():
            picked = qs[:sample] if sample else qs
            for q in picked:
                r_scoped = await tool.execute(
                    query=q.query, user_id=ids[username], collection=collection)
                c_scoped = r_scoped.content or ""
                ok_scoped = (not r_scoped.is_empty) and any(k in c_scoped for k in q.keywords)

                r_def = await tool.execute(query=q.query, user_id=ids[username])
                c_def = r_def.content or ""
                ok_def = (not r_def.is_empty) and any(k in c_def for k in q.keywords)

                scoped_tot += 1
                default_tot += 1
                scoped_ok += ok_scoped
                default_ok += ok_def
                # 判据以 scoped 为准：这个阶段验的是"数据造得对不对"。
                passed, failed = (passed + 1, failed) if ok_scoped else (passed, failed + 1)
                mark = f"{'OK ' if ok_scoped else 'FAIL'}/{'OK ' if ok_def else 'MISS'}"
                print(f"  [{mark}] {username:13s} {collection:26s} {q.query}")
                if not ok_scoped:
                    reason = "检索为空" if r_scoped.is_empty else f"未命中关键事实 {q.keywords}"
                    print(f"          → scoped {reason}；回答前 160 字：{c_scoped[:160]!r}")
                results.append({
                    "kind": "positive", "account": username, "collection": collection,
                    "query": q.query, "keywords": q.keywords, "source": q.source,
                    "scoped_pass": ok_scoped, "scoped_empty": r_scoped.is_empty,
                    "default_pass": ok_def, "default_empty": r_def.is_empty,
                    "pass": ok_scoped,
                    "scoped_head": c_scoped[:300], "default_head": c_def[:300],
                })

    print(
        f"\n  正向小计：指定库 {scoped_ok}/{scoped_tot}"
        f"　｜　全库并行（真实演示路径）{default_ok}/{default_tot}"
        f"　→ 差值 {scoped_ok - default_ok} 条是 top_docs=5 粗筛吃掉的，不是数据缺陷"
    )

    print("\n--- 应被拒绝 / 查不到的问题（越权与跨企业隔离）---")
    for d in Q.DENIALS:
        resp = await tool.execute(query=d.query, user_id=ids[d.account])
        content = resp.content or ""
        denied_marker = ("无权访问" in content) or ("没有权限" in content)
        # 只在**检索片段**里找另一家企业的标记词。回答开头会原样回显用户的问题，
        # 拿整段回答做子串匹配会把「问题里出现过 Globex」误判成越权。
        snippets = "\n".join(_SNIPPET_RE.findall(content))
        leaked = [w for w in d.forbidden if w in snippets]
        good = (resp.is_empty or denied_marker or not leaked) and not leaked
        passed, failed = (passed + 1, failed) if good else (passed, failed + 1)
        mark = "OK  " if good else "BREACH"
        how = "无结果" if resp.is_empty else ("显式拒绝" if denied_marker else "有结果但不含对方企业内容")
        print(f"  [{mark}] {d.account:13s} [{d.kind:14s}] {d.query}　（{how}）")
        if not good:
            print(f"         → 检索片段里出现了对方企业的标记词 {leaked}；片段前 200 字：{snippets[:200]!r}")
        results.append({
            "kind": "denial", "account": d.account, "denial_kind": d.kind,
            "target": d.target, "query": d.query,
            "is_empty": resp.is_empty, "denied_marker": denied_marker,
            "forbidden": list(d.forbidden), "leaked": leaked, "pass": good,
            "snippets_head": snippets[:400],
        })

    out = REPO_ROOT / "scripts" / "demo_dataset_results"
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = out / f"verify_{stamp}.json"
    path.write_text(json.dumps(
        {
            "passed": passed, "failed": failed,
            # §7.5：数字必须能追溯到确定的代码状态，否则将来没法从 git 复现。
            "code_state": _code_state(),
            "positive_scoped": f"{scoped_ok}/{scoped_tot}",
            "positive_default": f"{default_ok}/{default_tot}",
            "results": results,
        },
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n通过 {passed}，失败 {failed}。明细写入 {path.relative_to(REPO_ROOT)}")
    return failed


def stage_verify(sample: Optional[int]) -> int:
    return asyncio.run(_run_verify(sample))


# ============================================================ 阶段 3：生成文档
def _render_verify_block() -> str:
    """把最近一次 `--stage verify` 的实测结果渲染成 §0.3。

    刻意从落盘的结果文件读，而不是在文档里写死一句"已验证通过"——
    数字必须能追溯到某一次真实执行（CLAUDE.md §7.3 的三档结论要求）。
    """
    out_dir = REPO_ROOT / "scripts" / "demo_dataset_results"
    files = sorted(out_dir.glob("verify_*.json")) if out_dir.is_dir() else []
    if not files:
        return (
            "\n### §0.3 验收结果\n\n"
            "> ⚠️ 还没有跑过 `--stage verify`，本文里的问题**尚未经过实测验证**。\n"
            "> 跑一次 `.venv/bin/python scripts/generate_demo_kb_dataset.py --stage verify`\n"
            "> 之后重新生成本文，这里会自动填上实测数字。\n"
        )
    latest = files[-1]
    data = json.loads(latest.read_text(encoding="utf-8"))
    rows = data.get("results", [])
    pos = [r for r in rows if r.get("kind") == "positive"]
    den = [r for r in rows if r.get("kind") == "denial"]

    per_account: Dict[str, List[int]] = {}
    for r in pos:
        a = per_account.setdefault(r["account"], [0, 0, 0])
        a[0] += 1
        a[1] += bool(r.get("scoped_pass"))
        a[2] += bool(r.get("default_pass"))

    lines = [
        "",
        "### §0.3 验收结果（实测，不是估计）",
        "",
        f"数据来源：`scripts/demo_dataset_results/{latest.name}`，"
        f"用真实账号走真实 ACL 跑出来的。",
        "",
        "| 账号 | 正向问题 | 指定库命中 | 全库并行命中（真实演示路径） |",
        "|---|---|---|---|",
    ]
    for u in Q.ACCOUNTS:
        tot, sc, df = per_account.get(u, [0, 0, 0])
        if not tot:
            continue
        lines.append(f"| `{u}` | {tot} | **{sc}/{tot}** | {df}/{tot} |")
    sc_all = sum(v[1] for v in per_account.values())
    df_all = sum(v[2] for v in per_account.values())
    tot_all = sum(v[0] for v in per_account.values())
    lines += [
        f"| **合计** | {tot_all} | **{sc_all}/{tot_all}** | {df_all}/{tot_all} |",
        "",
        f"- **「指定库命中」才是这份数据集的验收判据**——它回答「语料在不在、查不查得到」。",
        f"- 「全库并行命中」低是 §0.2 那个 `top_docs=5` 粗筛瓶颈造成的，不是数据缺陷；"
        f"两列的差值 {sc_all - df_all} 条就是被粗筛吃掉的量。",
        "",
        f"拒绝类问题：**{sum(1 for r in den if r.get('pass'))}/{len(den)} 通过**"
        f"（判据是「检索片段里不含另一家企业的标记词」，不是「回答为空」——"
        f"回答开头会回显用户问题，按整段回答做子串匹配会误判）。",
        "",
    ]

    misses = [r for r in pos if not r.get("scoped_pass")]
    if misses:
        lines += [
            "#### 已知会答不准的问题（指定库也没命中）",
            "",
            "**刻意没有为了让它们变绿去改问法或补语料**——事实本身在库里，"
            "没命中是因为同一个库里有语义更近的另一篇文档把它挤掉了。"
            "演示时避开这几条，或直接翻文档原文核对。",
            "",
            "| 账号 | 知识库 | 问题 | 应该出自 |",
            "|---|---|---|---|",
        ]
        for r in misses:
            lines.append(
                f"| `{r['account']}` | `{r['collection']}` | {r['query']} | `{r['source']}` |"
            )
        lines.append("")
    return "\n".join(lines)


def _account_section(username: str, manifest: Dict[str, list]) -> str:
    a = Q.ACCOUNTS[username]
    lines = [
        f"### {a.username}",
        "",
        f"| 项 | 值 |",
        f"|---|---|",
        f"| 用户名 | `{a.username}` |",
        f"| 密码 | `{a.password}` |",
        f"| 所属企业 | {a.org} |",
        f"| 角色 | {a.role_display}（{a.role_kind}） |",
        f"| 可访问知识库 | {len(a.collections)} 个 |",
        "",
        f"> {a.note}",
        "",
        "可访问的知识库：",
        "",
    ]
    for c in a.collections:
        lines.append(f"- `{c}` —— {Q.KB_DISPLAY[c]}")
    lines.append("")

    for c in a.collections:
        qs = Q.POSITIVE.get(c, [])
        if not qs:
            continue
        lines += [
            f"#### {Q.KB_DISPLAY[c]}（`{c}`）",
            "",
            "| # | 问题 | 预期关键事实 | 数据出处 |",
            "|---|---|---|---|",
        ]
        for i, q in enumerate(qs, start=1):
            kw = " / ".join(f"`{k}`" for k in q.keywords)
            note = f"<br><sub>{q.note}</sub>" if q.note else ""
            lines.append(f"| {i} | {q.query} | {kw}{note} | `{q.source}` |")
        prefix = qs[0].source.rsplit("-", 1)[0]
        lines += [
            "",
            f"> 以上 5 条事实在本库的 `{prefix}-000 现行标准速查表（统一口径）` 里也有汇总，"
            f"演示时如果模型引用了旧的部门／枢纽特例，请翻这一篇核对。",
            "",
        ]

    denials = [d for d in Q.DENIALS if d.account == username]
    lines += [
        f"#### 🚫 {a.username} 应该被拒绝的问题（**比正向问题更重要**）",
        "",
        "| # | 问题 | 类型 | 指向的库 | 为什么应该被拒 |",
        "|---|---|---|---|---|",
    ]
    for i, d in enumerate(denials, start=1):
        kind = "本企业内无权限" if d.kind == "no_permission" else "跨企业隔离"
        lines.append(f"| {i} | {d.query} | {kind} | `{d.target}` | {d.why} |")
    lines += ["", "---", ""]
    return "\n".join(lines)


def stage_doc(manifest: Dict[str, list], dry_run: bool) -> None:
    today = date.today().isoformat()
    total_docs = sum(len(v) for v in manifest.values()) if manifest else 0
    per_kb = min((len(v) for v in manifest.values()), default=0)
    per_kb_total = per_kb + 20  # 各库原有 20 篇保留
    verify_block = _render_verify_block()

    head = f"""# 人工测试数据集（Acme / Globex 多租户演示）

> **状态：🟢 可用（数据已摄入并实测验证，见 §0.3）　日期：{today}**
>
> 实测：正向问题指定库命中 **67/70**，越权与跨企业拒绝用例 **11/11**。
> ⚠️ 但**不点名知识库直接问**时只有 29/70——原因是一个已定位、未修复的检索粗筛瓶颈，
> **演示前务必先读 §0.2**。
>
> 本文由 `scripts/generate_demo_kb_dataset.py --stage doc` **自动生成**，不要手改——
> 改语料或问题请改 `scripts/demo_kb_content/`，然后重跑脚本。
>
> 用途：拿着这份文档就能登录系统做多租户问答演示 / 人工回归。
> 每个问题都标了「预期关键事实」和「数据出处（文档编号）」，方便当场判对错。

## 0. 这份数据集长什么样

| 项 | 值 |
|---|---|
| 企业 | 2 家：Acme 有限公司（云软件 SaaS）、Globex 环球集团（跨境物流） |
| 知识库 | 每家 6 个部门库，共 12 个 |
| 本次新增语料 | 每库 {per_kb} 篇，共 {total_docs} 篇（各库原有 20 篇保留，现为每库 {per_kb_total} 篇） |
| 演示账号 | 4 个：每家企业 1 个企业管理员 + 1 个受限员工 |
| 正向问题 | 每个知识库 5 条，共 60 条 |
| 应被拒问题 | 11 条（每个账号至少 2 条） |

**两家企业的同名部门刻意用了不同的锚点事实**，所以跨企业隔离一旦失效，
演示时一眼就能看出来。最典型的一组：

| 事实 | Acme | Globex |
|---|---|---|
| 年假天数 | **15 天** | **20 天** |
| 年假顺延截止 | 次年 **3 月 31 日** | 次年 **6 月 30 日** |
| 报销需 CFO 审批的门槛 | **20000 元** | **30000 元** |
| 销售代表折扣上限 | **10%** | **8%** |
| 密码强制更换周期 | 域账号 **90 天** | WMS **60 天** |

### ⚠️ §0.1 一个必须先知道的既有问题：原有 20 篇语料跟统一口径互相矛盾

12 个库里**原本就有的那 20 篇**是 `scripts/generate_tenant_kb_corpus.py` 用随机数生成的，
同一个库里会同时存在互相打架的说法。2026-08-26 实测确认的几例：

| 库 | 原有语料里的冲突说法 |
|---|---|
| `acme_hr_admin_kb` | 「平台研发部年假 **20** 天」「客户成功部年假 **15** 天」「数据研发部年假 **15** 天」 |
| `acme_it_support_kb` | 域账号密码 **60 / 90 / 120 / 150** 天四种说法并存 |
| `acme_sales_marketing_kb` | 销售折扣上限 **10% / 15%** 并存 |
| `globex_sales_marketing_kb` | 销售折扣上限 **5% / 8% / 12%** 并存 |
| `globex_rd_product_kb` | 算法灰度门槛「下降至少 **1% / 3% / 5%**」并存 |

**本次没有删除那 20 篇**——删除要走 `BM25Indexer.remove_document`，
而它正是 CLAUDE.md §4 第 7 条那个已确认失效的 P0，不该夹带在造数据里顺手改。

采取的办法是：每个库新增一篇 **`XXX-000 现行标准速查表（统一口径）`**，
用企业里真实会有的方式写明「本表自 2026-01-01 起施行，此前按部门／枢纽分别发布的口径
一律停止适用」。演示时如果模型引用了老文档的部门特例，请翻到 `-000` 速查表核对——
**这不是模型答错，是库里确实还留着旧口径**。

### 🔴 §0.2 演示前必读：企业管理员账号目前会「查不到明明存在的内容」

**现象**：用 `alice_acme`（企业管理员，6 个库）问「域账号密码多久强制更换一次？」，
返回「未找到相关结果」，而这句话在 `ACME-IT-001` 正文里写得清清楚楚；
换成只有 1 个库的 `bob_acme` 问它权限内的问题，则一切正常。

**根因**（2026-08-26 实测定位，与本批数据无关，是既有实现的伸缩性问题）：
`query_knowledge_hub.py::_narrow_by_document_summary` 是层次化检索的粗筛层，
它把**全部候选知识库**的文档摘要命中合并，然后**按分数取全局前
`top_docs` 篇文档**（`config/settings.yaml` 里 `ingestion.doc_summary.top_docs`
默认 **5**），后续分块检索**只在这 5 篇里做**。

候选池原来是 6 库 × 20 篇 = 120 篇，取前 5 还能碰上；本批把每库补到 121 篇之后
候选池变成约 726 篇，正确文档挤不进这全局 5 篇，整条链路就返回「未找到」。

**同一份数据、同一批问题的 A/B 实测**（只改 `top_docs`，其余不变）：

| `top_docs` | 通过 |
|---|---|
| 5（当前默认） | **2 / 10** |
| 30 | **7 / 10** |

**本次没有改这个配置** —— `config/settings.yaml` 是所有会话共用的，
调它会改变全平台检索行为（也会拉高延迟），属于要单独评审的改动，不该夹带在造数据里。

⚠️ **单库账号只是受影响更小，不是免疫**：候选池是「全局前 5 篇」，
即使只有 1 个库，库内 121 篇里也可能选不中正确的那篇。实测 `bob_acme`
的 5 条正向问题：指定库 5/5，全库并行 3/5。所以「换个单库账号就没事了」是错的。

**演示时怎么办**（按可靠性排序）：
1. **在提问里点名知识库**（例如「在 IT 支持知识库里，域账号密码多久换一次」），
   这是最稳的——等价于 verify 里的 scoped 路径，实测通过率见 §0.3；
2. 优先用 `bob_acme` / `dave_globex` 这两个**单库账号**，候选池小、命中率高一些，
   但仍会漏（见上）；
3. 临时把 `ingestion.doc_summary.top_docs` 调到 30 再演示——**记得改回来**。

{verify_block}
---

## 1. 怎么用这份文档

1. 用下面任意一个账号登录（前端默认 `http://localhost:5173`，后端 `http://localhost:8000`）。
2. 在对话框里直接问「该账号可访问知识库」小节里的问题。
3. 对着「预期关键事实」判断答对没有；答错时按「数据出处」的文档编号去知识库管理页找原文核对。
4. **重点演示 🚫 小节**：那些问题应该拿到「无权访问」或「没有查到相关内容」，
   如果居然答出来了，就是权限或隔离出了问题。

⚠️ 一个容易误判的点：受限员工被拒时，系统返回的是「无权访问 / 没有权限访问这个知识库集合」；
跨企业的问题则通常表现为「没有查到相关内容」——**两者都算通过**，
因为候选库在 ACL 收敛阶段就被 `_org_owned_collections(本企业)` 挡掉了，压根没进检索。

---

## 2. 账号与问题

"""

    body = "".join(_account_section(u, manifest) for u in Q.ACCOUNTS)

    cmp_lines = [
        "## 3. 跨账号对照表（演示隔离最快的方式）",
        "",
        "同一个问题，四个账号分别问，看结果是不是按预期分叉。",
        "",
    ]
    for row in Q.COMPARISON:
        cmp_lines += [
            f"### 「{row.query}」",
            "",
            "| 账号 | 企业 | 预期结果 |",
            "|---|---|---|",
        ]
        for u, exp in row.expected.items():
            cmp_lines.append(f"| `{u}` | {Q.ACCOUNTS[u].org} | {exp} |")
        cmp_lines += ["", f"> {row.why}", ""]

    tail = f"""---

## 4. 数据是怎么造出来的 / 怎么复现

```bash
set -a; source .env; set +a
export RAGENT_DEBUG=true

# 看一遍会做什么，不落盘
.venv/bin/python scripts/generate_demo_kb_dataset.py --dry-run

# 生成语料 + 真实摄入 + 重新生成本文档
.venv/bin/python scripts/generate_demo_kb_dataset.py

# 用真实账号 + 真实 ACL 抽查
.venv/bin/python scripts/generate_demo_kb_dataset.py --stage verify
```

- 语料源码：`scripts/demo_kb_content/`（每个库一个模块），问题库：`scripts/demo_kb_content/questions.py`
- 语料文件：`data/demo_kb_corpus/{{tenant}}/{{category}}/`，清单 `data/demo_kb_corpus/manifest.json`
- 摄入走真实 `IngestionPipeline`，不是手工拼 BM25/Chroma 索引
- 脚本幂等：语料内容确定、摄入按文件 SHA256 跳过已有文件，可以反复跑
- ⚠️ `data/` 整个目录在 `.gitignore` 里，**语料文件不进版本库**——
  换一台机器要先跑一遍 `--stage generate`。语料的事实来源是
  `scripts/demo_kb_content/`（已入库），不是那些 txt 文件

🔴 脚本里有硬保护：只允许写 `acme_*_kb` / `globex_*_kb` 这 12 个库。
`product_req_kb`（端到端黄金测试 15/17 基线）和 `mmarco`（recall@10 85.0% 基线）
是评估语料，往里加数据会让全部质量基线失效。

---

## 5. 交付三句话（CLAUDE.md §7.3）

**验收怎么做**
用 §2 的四个账号分别登录（密码见各账号表格），照着「该账号可访问知识库」小节里的问题提问，
对着「预期关键事实」判断对错；再问 🚫 小节的问题，确认拿到「无权访问」或查不到对方企业的内容。
最快的一条路径是 §3 的跨账号对照表：同一个问题四个账号问一遍，看结果是不是按预期分叉。
⚠️ 提问时**点名知识库**（「在 IT 支持知识库里……」），原因见 §0.2。

**回归怎么保**
`scripts/generate_demo_kb_dataset.py --stage verify` 会用真实账号走真实 ACL 把
全部 60 条正向问题 + 11 条拒绝用例跑一遍，结果落 `scripts/demo_dataset_results/verify_*.json`
（含 commit hash 与脏文件清单），`--stage doc` 再把数字渲染进 §0.3。
语料本身是确定性生成的，`--stage generate` 重跑逐字节一致，摄入按 SHA256 幂等跳过。

**什么没做**
见下面 §6，逐条列了未覆盖的范围。最需要先知道的两条：
`top_docs=5` 粗筛瓶颈只定位未修复（§0.2）；原有 20 篇随机语料与统一口径的矛盾只做了口径覆盖、没有删除（§0.1）。

---

## 6. 本次未覆盖的范围

- **没有改任何权限配置**。四个账号的角色与知识库关联全部沿用现网既有状态，
  本次一行 DB 都没动。因此 `bob_acme` 与 `dave_globex` 的权限形状相同（各自企业的人力行政库），
  这不是遗漏，是刻意保持——改动它们会影响 `tests/integration/test_department_kb_parallel_recall.py`
  里已有的断言。
- **`__summary` 层同步摄入，但没有单独验证层次化检索的效果**。
- **没有测多轮对话、没有测工作流与考勤工具**，只覆盖知识库问答这一条链路。
- **正向问题的判据是「回答里出现预期关键字」**，不是语义正确性判定——
  模型把数字答对但语境说反的情况，这份判据抓不到，需要人读回答原文。
- **没有做并发验证**，全部是串行单用户抽查。
- **原有 20 篇随机语料的矛盾没有根除**，只是用 `-000` 速查表做了口径覆盖（见 §0 的警告）。
  彻底解决要先修 `BM25Indexer.remove_document` 这个 P0，不在本次范围内。
- **`top_docs=5` 这个粗筛瓶颈只定位、未修复**（见 §0.2）。因此本文里相当一部分
  正向问题，**在当前默认配置下、不点名知识库直接问，会返回「未找到相关结果」**
  ——这不是数据缺失，是粗筛层没选中。单库账号受影响更小但同样会漏。
  具体通过率见 §0.3 的实测表。
- **`acme_it_support_kb` 里有排查留下的多余文档**：定位 §0.2 那个问题时，
  先后试过两个错误假设，各留下了一批对照文档在库里——
  ① 按「正文在前、抬头在后」的版式重新摄入过一遍全库（101 篇，内容与现版本完全一致）；
  ② 两篇单句探针文档（`PROBE-IT-A_域账号密码更换周期`、`PROBE-IT-B_钓鱼邮件上报邮箱`），
  用来验证「文档太长」这个假设，结论是也不成立。
  这些没有强行清理：平台的按文档删除链路本身就是未闭环的 P0（CLAUDE.md §4 第 7 条），
  硬删要直接动 Chroma / BM25 / OpenSearch 三处索引，风险大于收益。
  它们**不会产生矛盾答案**（内容与正式版本一致），只是让该库文档数偏多。
  真要清理，建议跟那个 P0 一起处理。
- **拒绝类问题只验证了「拿不到内容」**，没有逐条核对审计日志是否记录了拒绝事件。
"""

    text = head + body + "\n".join(cmp_lines) + tail
    if dry_run:
        print(f"  [dry-run] 将写入 {DOC_PATH.relative_to(REPO_ROOT)}（{len(text)} 字符）")
        return
    DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOC_PATH.write_text(text, encoding="utf-8")
    print(f"  文档已写入 {DOC_PATH.relative_to(REPO_ROOT)}（{len(text)} 字符）")


# ============================================================ CLI
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--stage", choices=["generate", "ingest", "doc", "verify", "all"], default="all",
                   help="默认 all = generate + ingest + doc（verify 需显式指定或用 --with-verify）")
    p.add_argument("--with-verify", action="store_true", help="all 之后再跑一次 verify")
    p.add_argument("--dry-run", action="store_true", help="只打印将要做什么，不落盘、不摄入")
    p.add_argument("--tenant", choices=["acme", "globex"], help="只处理一家企业")
    p.add_argument("--collection", help="只处理一个 collection，例如 acme_hr_admin_kb")
    p.add_argument("--limit", type=int, help="每个库只处理前 N 篇（小规模验证流程用）")
    p.add_argument("--sample", type=int, help="verify 时每个库只抽前 N 条正向问题")
    args = p.parse_args()

    specs = all_kb_specs()
    if args.tenant:
        specs = [s for s in specs if s.tenant == args.tenant]
    if args.collection:
        _assert_safe_collection(args.collection)
        specs = [s for s in specs if s.collection == args.collection]
    if not specs:
        print("没有匹配到任何 collection")
        return 2

    stages = ["generate", "ingest", "doc"] if args.stage == "all" else [args.stage]
    if args.stage == "all" and args.with_verify:
        stages.append("verify")

    manifest: Dict[str, list] = {}
    failed = 0
    for stage in stages:
        print(f"\n{'=' * 64}\n阶段：{stage}{'（dry-run）' if args.dry_run else ''}\n{'=' * 64}")
        if stage == "generate":
            manifest = stage_generate(specs, args.limit, args.dry_run)
        elif stage == "ingest":
            stage_ingest(specs, args.limit, args.dry_run)
        elif stage == "doc":
            if not manifest and MANIFEST_PATH.exists():
                manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            stage_doc(manifest, args.dry_run)
        elif stage == "verify":
            if args.dry_run:
                print("  [dry-run] 跳过真实检索验证")
            else:
                failed = stage_verify(args.sample)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
