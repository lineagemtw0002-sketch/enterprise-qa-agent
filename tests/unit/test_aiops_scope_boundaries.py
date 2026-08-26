"""智能运维模块目标越界判定 —— 边界与越界侧补充覆盖。

配套 `tests/unit/test_aiops_scope.py`（正常路径 + 每类动作一条越界用例）。
本文件只补那份没覆盖的部分，不重复它已经测过的用例：

1. **边界值两侧成对**（等于上界/上界+1、等于下界/下界-1），而不是只测"明显越界"；
2. **管理员配置本身畸形**（缺字段、类型不对、空列表）时的 fail-open / fail-closed 方向；
3. **`docs/aiops_module_design.md` §10.3 点名的"排除规则优先"**，从"能不能被绕过"
   这个角度再压一遍，而不是只测"正常写法下排除确实优先"。

## 判别力自查（`CLAUDE.md` §7.2：写完测试问"它在旧实现下会失败吗"）

这是新代码，没有"旧实现"可比，所以判别力按"**把被测那行删掉/写反，这条测试会不会
变红**"来自查。逐条标注在每个类的 docstring 里，分两档：

- **判别式**：能钉死一个具体的实现选择（`is None` vs `not`、`>` vs `>=`、
  排除/允许两个循环的先后顺序、isinstance 校验在不在）。改掉那行就变红。
- **回归保护**：当前行为的快照，改实现不一定变红（比如"缺字段返回 allowed=False
  而不是抛异常"这种契约），价值在于防止将来无意改变契约。

## 三条已确认的实现与设计不符（以 `xfail(strict=True)` 记录，未改生产代码）

`TestExclusionRuleCannotBeBypassed` / `TestMalformedScopeConfigFailsClosed` /
`TestNumericScopeConfigTypeContract` 里带 `xfail(strict=True)` 的用例断言的是
**设计要求的行为**，当前实现做不到，所以现在是 xfail（不是绿灯，是"已知红"）。
一旦生产代码修好，strict xfail 会以 XPASS 报错，强制那次修复顺手把标记摘掉——
不会出现"修了但测试还挂着 xfail"的沉默状态。详见交付报告里的 bug 清单。
"""

from __future__ import annotations

import pytest

from src.ragent_backend.aiops_scope import (
    MAX_APPROVAL_TIMEOUT_MINUTES,
    MIN_APPROVAL_TIMEOUT_MINUTES,
    InvalidApprovalTimeout,
    InvalidScopeConfig,
    check_target_in_scope,
    validate_approval_timeout_minutes,
)


# ==================== clean_disk：排除规则优先（§10.3） ====================


class TestExclusionRuleCannotBeBypassed:
    """§10.3 那条"排除规则优先生效，不是先匹配到哪条算哪条"。

    判别力：`test_exclusion_wins_when_exclusion_listed_after_allow` 和
    `test_exclusion_wins_regardless_of_pattern_order` 是**判别式**——把
    `_check_clean_disk` 里两个 for 循环的顺序对调（这正是 §10.3 警告的
    "凭直觉搞错"的写法），这两条立刻变红。

    `test_path_traversal_defeats_exclusion_rule` 是**已确认的 bug**，见类内注释。
    """

    SCOPE = {
        "allowed_path_patterns": ["/var/lib/*"],
        "excluded_path_patterns": ["/var/lib/postgresql/*"],
    }

    def test_exclusion_wins_when_exclusion_listed_after_allow(self):
        # 允许模式 `/var/lib/*` 先命中，排除模式 `/var/lib/postgresql/*` 后命中，
        # "先匹配到哪条算哪条"的实现会放行 —— 必须拒绝。
        result = check_target_in_scope(
            "clean_disk", self.SCOPE, {"path": "/var/lib/postgresql/data/base.dat"}
        )
        assert result.allowed is False
        assert "排除" in result.reason

    def test_exclusion_wins_regardless_of_pattern_order(self):
        """把排除模式写在允许模式"前面"还是"后面"都不该影响结论——
        优先级来自代码里两个循环的顺序，不来自管理员配置的书写顺序。"""
        scope_a = {
            "allowed_path_patterns": ["/data/*", "/data/prod/*"],
            "excluded_path_patterns": ["/data/prod/*"],
        }
        scope_b = {
            "allowed_path_patterns": ["/data/prod/*", "/data/*"],
            "excluded_path_patterns": ["/data/prod/*"],
        }
        for scope in (scope_a, scope_b):
            result = check_target_in_scope("clean_disk", scope, {"path": "/data/prod/x.dat"})
            assert result.allowed is False, f"排除规则未生效：{scope}"

    def test_exclusion_wins_even_when_allow_pattern_is_wildcard_all(self):
        # 管理员把允许模式配成 `*`（全放开）时，排除规则是唯一的闸门。
        scope = {"allowed_path_patterns": ["*"], "excluded_path_patterns": ["/etc/*"]}
        result = check_target_in_scope("clean_disk", scope, {"path": "/etc/shadow"})
        assert result.allowed is False

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "已确认 bug：_check_clean_disk 用 fnmatch 做字面串匹配，不做路径规范化，"
            "`..` 可以绕过排除规则。修复方向需要 aiops_scope.py 的归属会话定夺"
            "（os.path.normpath + 拒绝含 .. 的路径，或改用 PurePath.match），"
            "本次只记录不改生产代码。"
        ),
    )
    def test_path_traversal_defeats_exclusion_rule(self):
        """`/var/log/app/../../lib/postgresql/data/base.dat` 实际指向被排除的
        postgres 数据目录，但字面串既命中允许模式 `/var/log/app/*`、又**不**命中
        排除模式 `/var/lib/postgresql/*`，于是被放行。

        这条直接击穿 §3.3.1 "AI 提议的目标必须落在管理员预先配置的白名单/边界内"
        —— 排除规则可以被一个 `..` 绕过，等于 §10.3 想守的东西没守住。
        """
        scope = {
            "allowed_path_patterns": ["/var/log/app/*"],
            "excluded_path_patterns": ["/var/lib/postgresql/*"],
        }
        result = check_target_in_scope(
            "clean_disk", scope, {"path": "/var/log/app/../../lib/postgresql/data/base.dat"}
        )
        assert result.allowed is False

    def test_star_currently_crosses_directory_separator(self):
        """回归保护（不是判别式）：`fnmatch` 的 `*` 会跨越 `/`，所以
        `/var/log/app/*.log` 也匹配任意深度的子目录。

        这是上面那条穿越 bug 的同一个根因（字面串 glob，没有路径语义）。
        单独钉住当前行为：如果将来改成路径语义的匹配（`*` 不跨 `/`），
        这条会变红，提醒那次改动**同时改变了允许侧的宽窄**，不只是修穿越。
        """
        scope = {"allowed_path_patterns": ["/var/log/app/*.log"], "excluded_path_patterns": []}
        result = check_target_in_scope(
            "clean_disk", scope, {"path": "/var/log/app/a/b/c/deep.log"}
        )
        assert result.allowed is True


class TestCleanDiskPathBoundaries:
    """判别力：`test_missing_exclusion_key_defaults_to_no_exclusion` 是**判别式**
    （`scope_config.get("excluded_path_patterns", [])` 的默认值一旦改成 `None`
    就会抛 TypeError）；其余是**回归保护**，钉住"缺字段/空列表时往哪边倒"。
    """

    def test_empty_allowlist_denies_everything(self):
        # fail-closed：空白名单 = 什么都不许清，不是"没配就是不限制"。
        result = check_target_in_scope(
            "clean_disk",
            {"allowed_path_patterns": [], "excluded_path_patterns": []},
            {"path": "/var/log/app/x.log"},
        )
        assert result.allowed is False

    def test_missing_exclusion_key_defaults_to_no_exclusion(self):
        # excluded_path_patterns 是可选字段，缺失时不应炸，也不应变成"全排除"。
        result = check_target_in_scope(
            "clean_disk", {"allowed_path_patterns": ["/var/log/*"]}, {"path": "/var/log/x.log"}
        )
        assert result.allowed is True

    @pytest.mark.parametrize("path", ["", None])
    def test_missing_or_empty_path_is_denied_not_raised(self, path):
        # 提议缺 path 是"提议不合法"（allowed=False），不是"管理员配置错"（抛异常），
        # 这两种失败必须能被调用方区分 —— 见 check_target_in_scope 的 docstring。
        result = check_target_in_scope(
            "clean_disk", {"allowed_path_patterns": ["*"]}, {"path": path}
        )
        assert result.allowed is False

    def test_matching_is_case_sensitive_on_posix(self):
        # 回归保护：大小写不同的路径不该被当成同一个路径放行。
        result = check_target_in_scope(
            "clean_disk",
            {"allowed_path_patterns": ["/var/log/app/*.log"], "excluded_path_patterns": []},
            {"path": "/VAR/LOG/APP/x.LOG"},
        )
        assert result.allowed is False


class TestMalformedScopeConfigFailsClosed:
    """管理员把 scope_config 配错时，往哪边倒。

    判别力：`test_allowlist_as_bare_string_is_rejected` 是**判别式**——去掉
    `isinstance(allowed_patterns, list)` 这行，字符串会被逐字符迭代，测试变红。

    `test_exclusion_as_bare_string_silently_disables_exclusion` 是**已确认 bug**：
    允许侧有 isinstance 校验、排除侧没有，两侧不对称，而排除侧才是安全侧。
    """

    def test_allowlist_as_bare_string_is_rejected(self):
        with pytest.raises(InvalidScopeConfig):
            check_target_in_scope(
                "clean_disk",
                {"allowed_path_patterns": "/var/log/app/*.log"},
                {"path": "/var/log/app/x.log"},
            )

    def test_restart_allowlist_as_bare_string_is_rejected(self):
        # 如果没有 isinstance 校验，`"order" in "order-service"` 这个子串判断
        # 会让一个根本不在清单里的服务名通过。
        with pytest.raises(InvalidScopeConfig):
            check_target_in_scope(
                "restart_service", {"allowed_targets": "order-service"}, {"target": "order"}
            )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "已确认 bug：excluded_path_patterns 没有 isinstance 校验（allowed 侧有），"
            "误配成字符串时被逐字符迭代，排除规则静默失效 —— fail-open。"
            "修复方向：与 allowed 侧对称地加一条 isinstance 校验，抛 InvalidScopeConfig。"
        ),
    )
    def test_exclusion_as_bare_string_silently_disables_exclusion(self):
        """管理员漏了方括号，把排除列表写成一个字符串。

        逐字符迭代后，每个字符（`/`、`v`、`a`…）都不匹配整条路径，
        排除规则等于不存在，本该被保护的 postgres 数据目录被放行。
        """
        with pytest.raises(InvalidScopeConfig):
            check_target_in_scope(
                "clean_disk",
                {
                    "allowed_path_patterns": ["/var/lib/*"],
                    "excluded_path_patterns": "/var/lib/postgresql",
                },
                {"path": "/var/lib/postgresql/data/base.dat"},
            )

    @pytest.mark.xfail(
        strict=True,
        reason="同上：excluded_path_patterns=None 时抛裸 TypeError，不是 InvalidScopeConfig。",
    )
    def test_exclusion_as_none_is_rejected_as_config_error(self):
        with pytest.raises(InvalidScopeConfig):
            check_target_in_scope(
                "clean_disk",
                {"allowed_path_patterns": ["/var/lib/*"], "excluded_path_patterns": None},
                {"path": "/var/lib/postgresql/data/base.dat"},
            )


# ==================== restart_service：allowed_targets ====================


class TestRestartServiceTargetMatching:
    """判别力：`test_substring_of_allowed_target_is_denied` 和
    `test_empty_allowlist_denies_everything` 是**判别式**——把精确匹配换成
    前缀/子串匹配、或者给空清单加"没配就放行"的捷径，都会变红。
    """

    SCOPE = {"allowed_targets": ["order-service", "payment-gateway"]}

    def test_substring_of_allowed_target_is_denied(self):
        # order-service-canary 是另一个服务，不能因为前缀相同就被放行。
        result = check_target_in_scope(
            "restart_service", self.SCOPE, {"target": "order-service-canary"}
        )
        assert result.allowed is False

    def test_prefix_of_allowed_target_is_denied(self):
        result = check_target_in_scope("restart_service", self.SCOPE, {"target": "order"})
        assert result.allowed is False

    def test_case_variant_is_denied(self):
        result = check_target_in_scope("restart_service", self.SCOPE, {"target": "Order-Service"})
        assert result.allowed is False

    def test_empty_allowlist_denies_everything(self):
        result = check_target_in_scope(
            "restart_service", {"allowed_targets": []}, {"target": "order-service"}
        )
        assert result.allowed is False

    def test_missing_target_key_is_denied_not_raised(self):
        result = check_target_in_scope("restart_service", self.SCOPE, {})
        assert result.allowed is False

    def test_every_listed_target_is_allowed(self):
        for target in self.SCOPE["allowed_targets"]:
            assert check_target_in_scope("restart_service", self.SCOPE, {"target": target}).allowed


# ==================== scale_instances：上下界 ====================


class TestScaleInstancesBoundaries:
    """判别力：`test_zero_min_instances_is_honoured_not_treated_as_missing` 是
    **判别式**——把 `min_instances is None` 写成 `not min_instances`（一个很常见的
    手滑），这条立刻变红，而现有 test_aiops_scope.py 里的用例全都发现不了。

    `test_at_lower_bound_allowed` / `test_one_below_lower_bound_denied` 成对，钉死
    `<` vs `<=`；`test_fractional_upper_bound_*` 成对，钉死非整数上界的取整行为
    （当前是不取整，4.5 就是 4.5）。
    """

    SCOPE = {"min_instances": 2, "max_multiplier_of_baseline": 2.0}

    def test_at_lower_bound_allowed(self):
        result = check_target_in_scope(
            "scale_instances", self.SCOPE, {"target_instances": 2, "baseline_instances": 4}
        )
        assert result.allowed is True

    def test_one_below_lower_bound_denied(self):
        result = check_target_in_scope(
            "scale_instances", self.SCOPE, {"target_instances": 1, "baseline_instances": 4}
        )
        assert result.allowed is False

    def test_one_above_upper_bound_denied(self):
        # 基线 4 × 2.0 = 8，9 越界。
        result = check_target_in_scope(
            "scale_instances", self.SCOPE, {"target_instances": 9, "baseline_instances": 4}
        )
        assert result.allowed is False

    def test_fractional_upper_bound_at_floor_allowed(self):
        # 基线 3 × 1.5 = 4.5，4 <= 4.5 放行。
        scope = {"min_instances": 1, "max_multiplier_of_baseline": 1.5}
        result = check_target_in_scope(
            "scale_instances", scope, {"target_instances": 4, "baseline_instances": 3}
        )
        assert result.allowed is True

    def test_fractional_upper_bound_above_ceiling_denied(self):
        scope = {"min_instances": 1, "max_multiplier_of_baseline": 1.5}
        result = check_target_in_scope(
            "scale_instances", scope, {"target_instances": 5, "baseline_instances": 3}
        )
        assert result.allowed is False

    def test_zero_min_instances_is_honoured_not_treated_as_missing(self):
        """`min_instances: 0`（允许缩容到 0，即完全下线）是一个合法配置，
        0 是 falsy 但不是"没配"——实现必须用 `is None` 判断存在性。"""
        scope = {"min_instances": 0, "max_multiplier_of_baseline": 2.0}
        result = check_target_in_scope(
            "scale_instances", scope, {"target_instances": 0, "baseline_instances": 4}
        )
        assert result.allowed is True

    def test_zero_multiplier_is_honoured_not_treated_as_missing(self):
        # max_multiplier_of_baseline: 0 = "只许缩不许扩"，同样是 falsy 但合法。
        scope = {"min_instances": 0, "max_multiplier_of_baseline": 0}
        assert (
            check_target_in_scope(
                "scale_instances", scope, {"target_instances": 1, "baseline_instances": 4}
            ).allowed
            is False
        )

    @pytest.mark.parametrize(
        "proposed",
        [
            {"target_instances": 3},
            {"baseline_instances": 4},
            {},
        ],
    )
    def test_missing_proposal_fields_denied_not_raised(self, proposed):
        # 回归保护：提议缺字段是"提议不合法"，不是"管理员配置错"。
        result = check_target_in_scope("scale_instances", self.SCOPE, proposed)
        assert result.allowed is False

    @pytest.mark.parametrize(
        "scope",
        [
            {"min_instances": 1},
            {"max_multiplier_of_baseline": 2.0},
            {},
        ],
    )
    def test_missing_config_fields_raise_config_error(self, scope):
        with pytest.raises(InvalidScopeConfig):
            check_target_in_scope(
                "scale_instances", scope, {"target_instances": 3, "baseline_instances": 4}
            )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "已确认设计缺口：上界 = baseline × multiplier，而 baseline_instances 跟 "
            "target_instances 一样来自同一份 AI 提议，边界因此是自指的 —— 提议方自报一个"
            "虚高基线就能把天花板抬到任意高度。§3.3.1 要求边界由管理员预先登记，"
            "baseline 应取连接器上报的实测值，不是提议里的自报值。"
            "这是设计层缺口（scope schema 没规定 baseline 从哪来），不是单纯的实现手滑。"
        ),
    )
    def test_self_reported_baseline_cannot_inflate_the_ceiling(self):
        """AI 提议"基线 5000，扩容到 10000"，倍数刚好 2.0，通过白名单校验。

        §3.3.1 举的反例正是"拒绝明显异常值（如'扩容到 10000'）"，
        当前实现拦不住这个例子本身。
        """
        result = check_target_in_scope(
            "scale_instances",
            {"min_instances": 1, "max_multiplier_of_baseline": 2.0},
            {"target_instances": 10000, "baseline_instances": 5000},
        )
        assert result.allowed is False


# ==================== rollback_deployment：max_versions_back ====================


class TestRollbackDeploymentBoundaries:
    """判别力：`test_zero_max_versions_back_denies_everything` 是**判别式**
    （同样钉 `is None` vs `not`）；上下界成对的两条钉 `>` vs `>=`。
    其余为回归保护。
    """

    SCOPE = {"max_versions_back": 5}

    def test_one_above_bound_denied(self):
        assert (
            check_target_in_scope(
                "rollback_deployment", self.SCOPE, {"target_version_offset": 6}
            ).allowed
            is False
        )

    def test_minimum_valid_offset_allowed(self):
        # 回滚 1 个版本是最小的合法回滚。
        assert (
            check_target_in_scope(
                "rollback_deployment", self.SCOPE, {"target_version_offset": 1}
            ).allowed
            is True
        )

    def test_negative_offset_denied(self):
        # 负 offset = "回滚到未来的版本"，无意义，必须拒绝而不是当成 0 处理。
        assert (
            check_target_in_scope(
                "rollback_deployment", self.SCOPE, {"target_version_offset": -3}
            ).allowed
            is False
        )

    def test_zero_max_versions_back_denies_everything(self):
        """`max_versions_back: 0` = "这个连接器不允许回滚"，是合法配置，
        不能因为 0 是 falsy 就被当成"没配置"抛 InvalidScopeConfig。"""
        result = check_target_in_scope(
            "rollback_deployment", {"max_versions_back": 0}, {"target_version_offset": 1}
        )
        assert result.allowed is False
        assert "上限 0" in result.reason

    def test_missing_offset_denied_not_raised(self):
        assert (
            check_target_in_scope("rollback_deployment", self.SCOPE, {}).allowed is False
        )


# ==================== 数值型配置的类型契约 ====================


class TestNumericScopeConfigTypeContract:
    """`check_target_in_scope` 的 docstring 承诺调用方能区分两种失败：
    `InvalidScopeConfig`（管理员配错）和 `allowed=False`（AI 提议越界）。
    数值字段被配成字符串时，当前实现两种都不是——漏出裸 `TypeError`。

    这三条都是**已确认 bug**（契约被打破），以 xfail(strict=True) 记录。
    """

    @pytest.mark.xfail(
        strict=True,
        reason="已确认 bug：max_multiplier_of_baseline 是字符串时漏出裸 TypeError，"
        "而不是 InvalidScopeConfig（管理员从 JSON/表单填错类型是很现实的场景）。",
    )
    def test_string_multiplier_raises_config_error(self):
        with pytest.raises(InvalidScopeConfig):
            check_target_in_scope(
                "scale_instances",
                {"min_instances": 1, "max_multiplier_of_baseline": "2.0"},
                {"target_instances": 10, "baseline_instances": 4},
            )

    @pytest.mark.xfail(
        strict=True,
        reason="已确认 bug：max_versions_back 是字符串时漏出裸 TypeError。",
    )
    def test_string_max_versions_back_raises_config_error(self):
        with pytest.raises(InvalidScopeConfig):
            check_target_in_scope(
                "rollback_deployment", {"max_versions_back": "5"}, {"target_version_offset": 6}
            )


# ==================== approval_timeout_minutes（§10.4） ====================


class TestApprovalTimeoutBoundaryEdges:
    """现有 test_aiops_scope.py 已覆盖 5 / 1440 / 4 / 1441 / 0 / 负数。
    这里补的是**类型侧**和**常量本身**，不重复数值边界。

    判别力：`test_bounds_match_design_document` 是判别式（改常量即变红）；
    `test_returns_the_same_value_it_validated` 是回归保护（钉住"校验通过就原值返回，
    不静默夹紧"这条 docstring 里写明的契约 —— 一旦有人改成 clamp 就变红）。
    """

    def test_bounds_match_design_document(self):
        # §10.4：默认 30 分钟，可配置范围 5 分钟 – 24 小时。
        assert MIN_APPROVAL_TIMEOUT_MINUTES == 5
        assert MAX_APPROVAL_TIMEOUT_MINUTES == 24 * 60

    @pytest.mark.parametrize("minutes", [5, 30, 60, 1439, 1440])
    def test_returns_the_same_value_it_validated(self, minutes):
        assert validate_approval_timeout_minutes(minutes) == minutes

    @pytest.mark.parametrize("minutes", [4, 1441, 10**9, -1])
    def test_out_of_range_rejected_not_clamped(self, minutes):
        # 静默夹紧会让管理员以为自己配的值生效了 —— 必须抛。
        with pytest.raises(InvalidApprovalTimeout):
            validate_approval_timeout_minutes(minutes)

    def test_boolean_is_rejected(self):
        # True/False 在 Python 里是 int 子类；恰好都落在范围外所以会被拒，
        # 回归保护：万一将来下界改到 0 或 1，这条会提醒 bool 不该被当成合法分钟数。
        with pytest.raises(InvalidApprovalTimeout):
            validate_approval_timeout_minutes(True)
        with pytest.raises(InvalidApprovalTimeout):
            validate_approval_timeout_minutes(False)

    @pytest.mark.xfail(
        strict=True,
        reason="已确认 bug：字符串分钟数漏出裸 TypeError，而不是 InvalidApprovalTimeout。"
        "这个值来自管理员在连接器配置里填的表单，字符串是最常见的输入形态。",
    )
    def test_string_minutes_raises_domain_error(self):
        with pytest.raises(InvalidApprovalTimeout):
            validate_approval_timeout_minutes("30")

    @pytest.mark.xfail(
        strict=True,
        reason="已确认 bug（轻微）：函数签名是 `-> int`，但 30.5 这样的浮点数会被"
        "原样接受并返回，落库后语义不明。",
    )
    def test_fractional_minutes_rejected(self):
        with pytest.raises(InvalidApprovalTimeout):
            validate_approval_timeout_minutes(30.5)
