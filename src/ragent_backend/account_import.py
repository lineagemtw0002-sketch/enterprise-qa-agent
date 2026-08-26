"""
员工账号批量导入 — 纯函数校验层

设计见 `docs/account_lifecycle_design.md` §4.1 / §4.4。

## 为什么校验逻辑单独成一个纯函数模块

万人企业不接受逐个建号，批量导入是账号体系里最先要有的东西。但它同时是
**一个能一次创建上万账号的新越权面**——`admin_create_user` 上已有的两条防护
（企业归属强制覆盖、跨企业角色校验）必须逐条复制过来，且要有测试钉住
（`CLAUDE.md` §7.2：凡涉及权限的改动必须同时提交测试）。

而本仓库 `tests/` 下**从没有测试碰过 Postgres**，`conftest.py` 里没有 DB
fixture。如果把这些判定写进 async 的端点函数，它们就永远没有测试。
所以这里的所有函数都**只接收已经从库里读出来的数据**，不做任何 IO，
判定结果是纯粹的数据。参照 `auth.py::resolve_jwt_secret` 那个已验证有效的模式
（11 条单测 2 秒跑完、零 fixture）。

⚠️ **这不能替代集成测试。** 纯函数能证明"规则写对了"，不能证明"端点真的调了
这些规则"。接线那一层要么有集成测试，要么在交付时明确写出这块覆盖度打折。

## 席位为什么也在这个文件里

席位（`docs/account_lifecycle_design.md` §4.4）的计量单位就是账号，而账号的
创建路径只有两条：管理端建号、批量导入。`check_seat_capacity` 放在这里，
是为了让这两条路径**用同一个函数**，不要把同一个计数写两遍。
第三个调用点是"重新启用一个已停用的用户"——它不创建账号却让占用数 +1，
是三处校验里最容易漏的一处。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Mapping, Optional, Sequence, Set

# CSV 必须有的列。刻意只要这两列必填——让企业 IT 从 AD 导出的表格
# 尽量少改就能用。
REQUIRED_COLUMNS = ("username", "role_name")
OPTIONAL_COLUMNS = ("display_name",)

# ⚠️ 出现任何一个就**整份文件拒收**，不是忽略该列。
# 设计（§4.1b）已定：初始凭证走一次性激活码，CSV 里不应该有密码。
# 如果只是静默忽略这一列，管理员会以为密码设进去了，而员工拿不到任何凭证；
# 更糟的是那份含明文密码的文件已经在他们的邮箱/群里流转过了。
# 拒收并报错，才能让"这个系统不通过 CSV 传密码"这件事被人知道。
FORBIDDEN_COLUMNS = frozenset({"password", "passwd", "pwd", "密码", "初始密码"})

# users.username 是 VARCHAR(64) UNIQUE，见 user_store.py:72
MAX_USERNAME_LEN = 64


class RowAction(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    ERROR = "error"


@dataclass(frozen=True)
class ImportContext:
    """一次导入所需的全部库内事实，由调用方一次性查好传进来。

    `actor_org_id` 是**调用者自己企业的 id**，不是请求体里的 org_id——
    企业归属强制覆盖这条防护就落在"这个字段只能这么来"上。
    `admin_create_user` 里已经是这个做法，批量导入是同一个越权面，
    不能因为走了新端点就漏掉。
    """

    actor_org_id: str
    # role_name -> role_id，**调用方必须只放本企业可分配的角色**。
    # 跨企业角色校验因此退化成一次字典查找：不在表里就是不能分配。
    assignable_roles: Mapping[str, str]
    # username -> org_id，全平台范围。username 在库里是全局 UNIQUE
    # （user_store.py:72），所以这里必须是全平台而不是本企业，理由见
    # `_classify_existing`。
    existing_users: Mapping[str, str] = field(default_factory=dict)
    seat_limit: Optional[int] = None  # None = 不限
    seats_used: int = 0


@dataclass(frozen=True)
class RowOutcome:
    line_no: int  # CSV 里的行号（含表头，从 1 数），报错时给人看的
    username: str
    action: RowAction
    role_id: Optional[str] = None
    display_name: Optional[str] = None
    reason: Optional[str] = None  # 仅 ERROR 时有值


@dataclass(frozen=True)
class SeatCheck:
    ok: bool
    seats_used: int
    seat_limit: Optional[int]
    delta: int
    detail: Optional[str] = None


@dataclass(frozen=True)
class ImportPlan:
    outcomes: List[RowOutcome]
    seat_check: SeatCheck
    fatal_error: Optional[str] = None  # 整份文件级别的问题（表头不合法等）

    @property
    def to_create(self) -> List[RowOutcome]:
        return [o for o in self.outcomes if o.action is RowAction.CREATE]

    @property
    def to_update(self) -> List[RowOutcome]:
        return [o for o in self.outcomes if o.action is RowAction.UPDATE]

    @property
    def errors(self) -> List[RowOutcome]:
        return [o for o in self.outcomes if o.action is RowAction.ERROR]

    @property
    def applicable(self) -> bool:
        """能不能真的执行。整份文件出错、或席位不够，都不执行。"""
        return self.fatal_error is None and self.seat_check.ok


def check_seat_capacity(
    seats_used: int, seat_limit: Optional[int], delta: int
) -> SeatCheck:
    """席位校验。三个调用点共用：建号、批量导入、**重新启用已停用用户**。

    `seat_limit is None` 表示不限（存量企业迁移过来时就是这个状态，
    不能因为没配上限就把所有人挡在外面）。

    ⚠️ **占用口径：只数 `disabled_at IS NULL` 的用户，停用的人不占席位。**
    这个口径不是随手定的，它把删除权限（§4.2）和席位（§4.4）绑在了一起：
    如果停用仍然占席位，客户为了腾名额就会去**删除**离职员工，
    而"改停用不改删除"的全部理由正是"删除会带走会话归属、破坏审计追溯"。
    收费规则会直接决定客户选停用还是删除，让停用免费，客户才会选对的那个。
    这条口径由调用方保证（传进来的 `seats_used` 怎么算的），本函数只做算术。

    失败文案里带上当前用量与上限：企业管理员看到一句泛泛的"创建失败"会
    以为是系统故障来报障，看到"12/12，请联系平台方"才知道该找谁。
    """
    if seat_limit is None:
        return SeatCheck(True, seats_used, None, delta)
    if seats_used + delta <= seat_limit:
        return SeatCheck(True, seats_used, seat_limit, delta)
    over = seats_used + delta - seat_limit
    return SeatCheck(
        False, seats_used, seat_limit, delta,
        detail=(
            f"席位不足：当前已用 {seats_used}，本次将新增 {delta}，"
            f"上限 {seat_limit}，超出 {over} 个。请联系平台方调整席位上限。"
        ),
    )


def _normalize_header(name: str) -> str:
    # Excel 导出常带 BOM 和前后空格；大小写不敏感能省掉一轮来回。
    return name.strip().lstrip("﻿").lower()


def parse_csv(text: str) -> tuple[List[Dict[str, str]], Optional[str]]:
    """解析 CSV，返回 `(行, 整份文件级别的错误)`。

    整份文件级别的错误只有两类：缺必填列、出现被禁列。
    单行的问题不在这里判——那是 `validate_row` 的事，且必须逐行隔离
    （1000 行里第 800 行错了，前 799 行不该受影响）。
    """
    try:
        reader = csv.DictReader(io.StringIO(text))
        raw_fields = reader.fieldnames or []
    except (csv.Error, UnicodeDecodeError) as exc:  # pragma: no cover - csv 很少抛
        return [], f"CSV 无法解析：{exc}"

    headers = [_normalize_header(f) for f in raw_fields if f is not None]

    forbidden = [h for h in headers if h in FORBIDDEN_COLUMNS]
    if forbidden:
        return [], (
            f"CSV 含密码列 {forbidden}，已拒绝导入。"
            "本系统不通过 CSV 传递密码——导入后会为每个新账号生成一次性激活码，"
            "由管理员分发，员工凭码自行设置密码。请删除该列后重新上传。"
        )

    missing = [c for c in REQUIRED_COLUMNS if c not in headers]
    if missing:
        return [], f"CSV 缺少必填列：{missing}（必填列为 {list(REQUIRED_COLUMNS)}）"

    rows: List[Dict[str, str]] = []
    for raw in reader:
        rows.append({
            _normalize_header(k): (v or "").strip()
            for k, v in raw.items()
            if k is not None
        })
    return rows, None


def _classify_existing(username: str, ctx: ImportContext) -> tuple[RowAction, Optional[str]]:
    """这个 username 该建、该更新，还是该拒？

    ⚠️ **这里是这个模块最重要的一条判定。** `users.username` 是
    **全局 UNIQUE**（`user_store.py:72`），不是企业内唯一。所以"username 已存在
    就更新"这条幂等规则如果不看归属，Acme 的管理员只要在 CSV 里写一行
    `zhangsan`，就能改掉 Globex 那个 `zhangsan` 的角色——**跨企业账号接管**。

    正确的分法是三档：本企业已有 → 更新；别的企业已有 → **拒绝**；没有 → 新建。

    ⚠️ 拒绝时的文案刻意只说"已被占用"，**不说被哪家企业占用**。
    这仍然泄露了"这个用户名存在"这一位信息（用户名全局唯一的系统绕不开），
    但至少不泄露它属于谁。这个端点在管理员鉴权之后，跟无鉴权的
    `/activate`（那里必须连存在性都不泄露，见 `activation.py`）风险等级不同。
    """
    owner_org = ctx.existing_users.get(username)
    if owner_org is None:
        return RowAction.CREATE, None
    if owner_org == ctx.actor_org_id:
        return RowAction.UPDATE, None
    return RowAction.ERROR, "该用户名已被占用，请换一个"


def validate_row(
    row: Mapping[str, str], line_no: int, ctx: ImportContext, seen: Set[str]
) -> RowOutcome:
    """校验单行。`seen` 是本次文件里已经出现过的 username（调用方维护）。

    ⚠️ **任何情况下都返回一个 `RowOutcome`，不抛异常。** 逐行隔离是硬要求：
    企业 IT 的预期是"告诉我哪几行有问题，我改完再传一次"，
    而不是"第 800 行错了所以什么都没导进去"。
    """
    username = (row.get("username") or "").strip()
    role_name = (row.get("role_name") or "").strip()
    display_name = (row.get("display_name") or "").strip() or None

    def err(reason: str) -> RowOutcome:
        return RowOutcome(line_no, username, RowAction.ERROR, reason=reason)

    if not username:
        return err("username 为空")
    if len(username) > MAX_USERNAME_LEN:
        return err(f"username 超过 {MAX_USERNAME_LEN} 字符")
    if username in seen:
        # 同一份文件里重复。不能当成"更新"处理——两行的角色可能不同，
        # 谁后写谁生效是隐式的，管理员不会知道最终生效的是哪一行。
        return err("同一份文件里出现了重复的 username")
    if not role_name:
        return err("role_name 为空")

    role_id = ctx.assignable_roles.get(role_name)
    if role_id is None:
        # 跨企业角色校验落在这里：`assignable_roles` 由调用方按 actor 的企业
        # 过滤后传入，所以"别家企业的角色"和"根本不存在的角色"在这里
        # 都表现为查不到。文案统一，同样不泄露别家企业有哪些角色。
        return err(f"角色 {role_name!r} 不存在或不可分配")

    action, reason = _classify_existing(username, ctx)
    if action is RowAction.ERROR:
        return err(reason or "用户名不可用")

    return RowOutcome(line_no, username, action, role_id=role_id, display_name=display_name)


def plan_import(text: str, ctx: ImportContext) -> ImportPlan:
    """从 CSV 原文算出一份完整的执行计划。

    **dry-run 和真跑用的是同一个函数** —— dry-run 就是"算出计划但不执行"。
    如果预演走一条代码路径、真跑走另一条，预演就失去了意义（它保证不了
    真跑会发生同样的事）。§4.1 要求"必须先 dry-run"，靠的就是这一点。
    """
    rows, fatal = parse_csv(text)
    if fatal is not None:
        return ImportPlan([], check_seat_capacity(ctx.seats_used, ctx.seat_limit, 0), fatal)

    outcomes: List[RowOutcome] = []
    seen: Set[str] = set()
    for idx, row in enumerate(rows):
        # +2：CSV 第 1 行是表头，且行号从 1 开始数——报错说"第 5 行"时，
        # 管理员在 Excel 里看到的就该是第 5 行。
        outcome = validate_row(row, idx + 2, ctx, seen)
        if outcome.username:
            seen.add(outcome.username)
        outcomes.append(outcome)

    # 只有新建才占席位；更新已有用户不增加占用。
    delta = sum(1 for o in outcomes if o.action is RowAction.CREATE)
    return ImportPlan(outcomes, check_seat_capacity(ctx.seats_used, ctx.seat_limit, delta))


def format_dry_run_summary(plan: ImportPlan) -> str:
    """给管理员看的预演摘要。

    席位那一行**总是**出现，即使没超——让管理员在传 1000 行之前就知道会不会
    撞上限，而不是导入到第 800 行才失败（§4.4 的要求）。
    """
    if plan.fatal_error:
        return f"整份文件未通过校验：{plan.fatal_error}"
    sc = plan.seat_check
    limit_text = "不限" if sc.seat_limit is None else str(sc.seat_limit)
    lines = [
        f"将新建 {len(plan.to_create)} 个账号，更新 {len(plan.to_update)} 个，"
        f"{len(plan.errors)} 行有问题。",
        f"席位：当前已用 {sc.seats_used}，本次新增 {sc.delta}，上限 {limit_text}。",
    ]
    if not sc.ok and sc.detail:
        lines.append(sc.detail)
    for e in plan.errors:
        lines.append(f"  第 {e.line_no} 行（{e.username or '空'}）：{e.reason}")
    return "\n".join(lines)
