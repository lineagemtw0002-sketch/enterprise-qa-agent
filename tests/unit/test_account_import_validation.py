"""批量导入校验层的回归保护。设计见 `docs/account_lifecycle_design.md` §4.1 §4.4。

对应设计里的 T-1/T-2/T-3/T-4/T-6/T-9 与 T-15~T-18 的**逻辑部分**。

⚠️ **这组测试证明的是"规则写对了"，不是"端点真的调了这些规则"。**
接线那一层（`app.py` 的导入端点）现在还没写，写完之后如果没有集成测试覆盖，
交付时必须按 `CLAUDE.md` §7.3 明确写出这块覆盖度打折。
本仓库 `tests/` 下从没有测试碰过 Postgres，这也是这些判定被刻意做成纯函数的原因。

**判别力**：每条测试针对性地杀掉一条规则——把 `account_import.py` 里对应的
那一个判断删掉，只有这一条会红，其余保持绿。写的时候逐条验过。
"""

from __future__ import annotations

import pytest

from src.ragent_backend.account_import import (
    FORBIDDEN_COLUMNS,
    ImportContext,
    RowAction,
    check_seat_capacity,
    format_dry_run_summary,
    parse_csv,
    plan_import,
)

ACME = "org-acme"
GLOBEX = "org-globex"


def ctx(**kw) -> ImportContext:
    base = dict(
        actor_org_id=ACME,
        assignable_roles={"HR专员": "role-hr", "IT运维": "role-it"},
        existing_users={},
        seat_limit=None,
        seats_used=0,
    )
    base.update(kw)
    return ImportContext(**base)


HEADER = "username,role_name,display_name\n"


class TestOrgScoping:
    """T-1 / T-2：企业归属与跨企业角色。这两条是从 `admin_create_user`
    复制过来的防护，批量导入是同一个越权面。"""

    def test_role_not_in_assignable_is_rejected(self):
        """T-2：不在 `assignable_roles` 里的角色整行失败。

        调用方按 actor 的企业过滤后才传进来，所以"别家企业的角色"和
        "根本不存在的角色"在这里都表现为查不到——这正是想要的。
        """
        plan = plan_import(HEADER + "alice,财务主管,\n", ctx())
        assert len(plan.errors) == 1
        assert "不存在或不可分配" in plan.errors[0].reason

    def test_rejected_role_does_not_create_the_account(self):
        """T-2 的后半句：**整行失败且不建号**。

        只报错但仍然建一个没有角色的账号，比不建更糟——`users.role` 与
        `allowed_collections` 都是 NOT NULL，会得到一个登进来什么都看不到的
        僵尸账号，而管理员以为导入成功了。
        """
        plan = plan_import(HEADER + "alice,财务主管,\n", ctx())
        assert plan.to_create == []
        assert plan.to_update == []

    def test_username_owned_by_another_org_is_rejected_not_updated(self):
        """**这条是整组里最重要的一条。**

        `users.username` 是全局 UNIQUE（`user_store.py:72`），不是企业内唯一。
        所以"已存在就更新"这条幂等规则如果不看归属，Acme 的管理员只要在 CSV
        里写一行 `zhangsan`，就能改掉 Globex 那个 `zhangsan` 的角色 ——
        跨企业账号接管。

        把 `_classify_existing` 里 `owner_org == ctx.actor_org_id` 这个判断
        去掉（退化成"存在就更新"），只有这条会红。
        """
        plan = plan_import(
            HEADER + "zhangsan,HR专员,\n",
            ctx(existing_users={"zhangsan": GLOBEX}),
        )
        assert plan.to_update == [], "别家企业的用户绝不能被当成'更新'"
        assert plan.to_create == []
        assert len(plan.errors) == 1

    def test_conflict_message_does_not_reveal_the_owning_org(self):
        """拒绝文案不能说被哪家企业占用。

        用户名全局唯一的系统绕不开"这个名字存在"这一位信息的泄露，
        但"它属于谁"是可以不说的，而且必须不说 —— 否则企业管理员可以拿
        一份姓名表探测出竞争对手企业的员工花名册。
        """
        plan = plan_import(
            HEADER + "zhangsan,HR专员,\n",
            ctx(existing_users={"zhangsan": GLOBEX}),
        )
        reason = plan.errors[0].reason
        assert GLOBEX not in reason and "globex" not in reason.lower()

    def test_same_org_existing_user_is_an_update(self):
        """对照组：本企业已有的用户走更新，幂等规则本身没被上一条测坏。"""
        plan = plan_import(
            HEADER + "zhangsan,HR专员,\n",
            ctx(existing_users={"zhangsan": ACME}),
        )
        assert len(plan.to_update) == 1
        assert plan.to_create == []


class TestRowIsolation:
    """T-3：逐行隔离。"""

    def test_one_bad_row_does_not_sink_the_others(self):
        """999 行成功、1 行报错 —— 企业 IT 的预期是"告诉我哪几行有问题"，
        而不是"第 800 行错了所以什么都没导进去"。"""
        rows = "".join(f"user{i},HR专员,\n" for i in range(999))
        rows += "bad,不存在的角色,\n"
        plan = plan_import(HEADER + rows, ctx())
        assert len(plan.to_create) == 999
        assert len(plan.errors) == 1

    def test_line_numbers_match_what_the_admin_sees_in_excel(self):
        """行号要对得上 Excel 里的行号，否则"第 5 行有问题"帮不上忙。
        表头占第 1 行，所以第一条数据是第 2 行。"""
        plan = plan_import(HEADER + "ok,HR专员,\nbad,无此角色,\n", ctx())
        assert plan.errors[0].line_no == 3

    @pytest.mark.parametrize("bad,expect", [
        (",HR专员,\n", "username 为空"),
        ("alice,,\n", "role_name 为空"),
        ("x" * 65 + ",HR专员,\n", "超过 64 字符"),
    ])
    def test_malformed_rows_are_reported_not_raised(self, bad, expect):
        """任何情况下返回 RowOutcome，不抛异常 —— 抛了就破坏逐行隔离。"""
        plan = plan_import(HEADER + bad, ctx())
        assert expect in plan.errors[0].reason


class TestIdempotency:
    """T-4：同一份文件传两次是安全的。"""

    def test_second_run_is_all_updates(self):
        """第一次全新建，把结果喂回 context 再跑一次，应该全变更新，
        且**不再占用新席位**。"""
        csv_text = HEADER + "alice,HR专员,\nbob,IT运维,\n"
        first = plan_import(csv_text, ctx(seat_limit=10, seats_used=0))
        assert len(first.to_create) == 2

        after = {o.username: ACME for o in first.to_create}
        second = plan_import(csv_text, ctx(existing_users=after, seat_limit=10, seats_used=2))
        assert len(second.to_update) == 2
        assert second.to_create == []
        assert second.seat_check.delta == 0, "重复导入不该再占席位"

    def test_duplicate_username_within_one_file_is_an_error(self):
        """同一份文件里重复不能当成"更新"。

        两行的角色可能不同，谁后写谁生效是**隐式的** —— 管理员不会知道最终
        生效的是哪一行。显式报错让他自己决定。
        """
        plan = plan_import(HEADER + "alice,HR专员,\nalice,IT运维,\n", ctx())
        assert len(plan.to_create) == 1
        assert len(plan.errors) == 1
        assert "重复" in plan.errors[0].reason


class TestNoPasswordsInCsv:
    """T-9：CSV 里不能有密码列。"""

    @pytest.mark.parametrize("col", sorted(FORBIDDEN_COLUMNS))
    def test_password_column_rejects_the_whole_file(self, col):
        plan = plan_import(f"username,role_name,{col}\nalice,HR专员,hunter2\n", ctx())
        assert plan.fatal_error is not None
        assert plan.outcomes == [], "拒收就是一行都不处理"
        assert not plan.applicable

    def test_rejection_explains_what_to_do_instead(self):
        """光说"不许有密码列"没用，得告诉管理员凭证怎么来 ——
        否则他会以为这个系统建完号没法登录。"""
        plan = plan_import("username,role_name,password\na,HR专员,x\n", ctx())
        assert "激活码" in plan.fatal_error

    def test_forbidden_column_check_is_case_insensitive(self):
        """Excel 导出的表头大小写不受控。"""
        plan = plan_import("username,role_name,PASSWORD\na,HR专员,x\n", ctx())
        assert plan.fatal_error is not None

    def test_missing_required_column_is_fatal(self):
        plan = plan_import("username\nalice\n", ctx())
        assert "缺少必填列" in plan.fatal_error


class TestSeats:
    """T-15 ~ T-18：席位。"""

    def test_limit_blocks_creation(self):
        """T-15：满员时新建被拒。"""
        plan = plan_import(HEADER + "alice,HR专员,\n", ctx(seat_limit=3, seats_used=3))
        assert not plan.seat_check.ok
        assert not plan.applicable

    def test_disabled_users_do_not_consume_seats(self):
        """T-16：停用的人不占席位。

        口径由调用方保证（传进来的 `seats_used` 只数 `disabled_at IS NULL`），
        这里测的是这个口径成立时结果对不对：停用 1 人后同样的请求就能过。

        ⚠️ 这条钉住的是一个**商业前提**而不只是算术：如果停用仍然占席位，
        客户为了腾名额就会去删除离职员工，而"改停用不改删除"的全部理由
        正是"删除会破坏审计追溯"。
        """
        blocked = plan_import(HEADER + "alice,HR专员,\n", ctx(seat_limit=3, seats_used=3))
        assert not blocked.seat_check.ok
        freed = plan_import(HEADER + "alice,HR专员,\n", ctx(seat_limit=3, seats_used=2))
        assert freed.seat_check.ok

    def test_reenable_path_shares_the_same_check(self):
        """T-17：重新启用也要过席位校验。

        它不创建账号却让占用数 +1，是三处校验里最容易漏的一处。
        这里直接调 `check_seat_capacity`，证明它是能被那条路径复用的独立函数，
        而不是埋在导入流程里的私有逻辑。
        """
        assert not check_seat_capacity(seats_used=5, seat_limit=5, delta=1).ok
        assert check_seat_capacity(seats_used=4, seat_limit=5, delta=1).ok

    def test_none_limit_means_unlimited(self):
        """存量企业迁移过来时 seat_limit 是 NULL，
        不能因为没配上限就把所有人挡在外面。"""
        assert check_seat_capacity(seats_used=10_000, seat_limit=None, delta=500).ok

    def test_failure_detail_carries_usage_and_limit(self):
        """一句泛泛的"创建失败"会让企业管理员以为是系统故障来报障。"""
        sc = check_seat_capacity(seats_used=12, seat_limit=12, delta=1)
        assert "12" in sc.detail and "联系平台方" in sc.detail

    def test_updates_do_not_count_toward_seats(self):
        """只有新建占席位。满员的企业仍然应该能改现有员工的角色。"""
        plan = plan_import(
            HEADER + "alice,HR专员,\n",
            ctx(existing_users={"alice": ACME}, seat_limit=1, seats_used=1),
        )
        assert plan.seat_check.delta == 0
        assert plan.applicable

    def test_seat_line_is_always_in_the_dry_run_summary(self):
        """T-18：席位那一行**总是**出现，即使没超。

        §4.4 的要求是让管理员在传 1000 行之前就知道会不会撞上限，
        而不是导入到第 800 行才失败。只在超限时才提，就做不到这件事。
        """
        plan = plan_import(HEADER + "alice,HR专员,\n", ctx(seat_limit=100, seats_used=3))
        assert plan.seat_check.ok
        summary = format_dry_run_summary(plan)
        assert "席位" in summary and "上限 100" in summary


class TestDryRunUsesTheSameCodePath:
    """T-6：预演不落库。

    纯函数层测不了"没落库"（它本来就不碰库），能测的是更根本的一条：
    **预演和真跑用的是同一个函数**。如果预演走一条路径、真跑走另一条，
    预演就失去了意义——它保证不了真跑会发生同样的事。
    """

    def test_plan_is_deterministic_and_pure(self):
        csv_text = HEADER + "alice,HR专员,\nbob,无此角色,\n"
        c = ctx(seat_limit=5, seats_used=1)
        a, b = plan_import(csv_text, c), plan_import(csv_text, c)
        assert a.outcomes == b.outcomes and a.seat_check == b.seat_check

    def test_context_is_frozen(self):
        """ImportContext 不可变 —— 预演不能顺手改掉调用方的状态。"""
        with pytest.raises(Exception):
            ctx().seats_used = 99  # type: ignore[misc]


class TestCsvErgonomics:
    """企业 IT 从 AD/Excel 导出的表格千奇百怪，尽量少让他们改。"""

    def test_bom_and_whitespace_in_headers(self):
        rows, fatal = parse_csv("﻿Username , Role_Name \nalice,HR专员\n")
        assert fatal is None
        assert rows[0]["username"] == "alice"

    def test_values_are_stripped(self):
        plan = plan_import(HEADER + "  alice  , HR专员 , 张三 \n", ctx())
        assert plan.to_create[0].username == "alice"
        assert plan.to_create[0].display_name == "张三"

    def test_display_name_is_optional(self):
        rows, fatal = parse_csv("username,role_name\nalice,HR专员\n")
        assert fatal is None and len(rows) == 1
