"""智能运维模块目标越界判定的回归保护 —— `docs/aiops_module_design.md` §3.3.1 /
§10.3 / §10.4 / §6（测试设计）。

判别力自查（`CLAUDE.md` §7.2）：这是新代码，没有"旧实现"可以对比。判别力体现在
每条越界用例都配一条对应的合法用例做对照组——如果判定逻辑写反了（比如把
`<` 写成 `<=`，或者排除/允许两条规则的优先级反了），至少会有一侧变色。
"""

from __future__ import annotations

import pytest

from src.ragent_backend.aiops_scope import (
    ACTION_TYPES,
    DEFAULT_APPROVAL_TIMEOUT_MINUTES,
    InvalidActionType,
    InvalidApprovalTimeout,
    InvalidScopeConfig,
    check_target_in_scope,
    validate_action_type,
    validate_approval_timeout_minutes,
)


class TestActionTypeIsLockedToFour:
    def test_four_and_only_four_action_types(self):
        # V1 硬编码是设计意图（docs/aiops_module_design.md §0⑥），这条测试
        # 钉住"到底是不是四个"——新增第五个动作类型必须先改设计文档再改这里。
        assert ACTION_TYPES == {
            "restart_service", "scale_instances", "clean_disk", "rollback_deployment",
        }

    def test_unknown_action_type_rejected(self):
        with pytest.raises(InvalidActionType):
            validate_action_type("delete_database")

    def test_known_action_types_pass(self):
        for at in ACTION_TYPES:
            validate_action_type(at)  # 不抛异常


class TestRestartServiceScope:
    SCOPE = {"allowed_targets": ["order-service", "payment-gateway"]}

    def test_target_in_allowlist_is_allowed(self):
        result = check_target_in_scope("restart_service", self.SCOPE, {"target": "order-service"})
        assert result.allowed is True

    def test_target_outside_allowlist_is_rejected(self):
        result = check_target_in_scope("restart_service", self.SCOPE, {"target": "admin-database"})
        assert result.allowed is False
        assert "admin-database" in result.reason

    def test_malformed_scope_config_raises(self):
        with pytest.raises(InvalidScopeConfig):
            check_target_in_scope("restart_service", {}, {"target": "order-service"})


class TestScaleInstancesScope:
    SCOPE = {"min_instances": 1, "max_multiplier_of_baseline": 2.0}

    def test_within_bounds_is_allowed(self):
        result = check_target_in_scope(
            "scale_instances", self.SCOPE, {"target_instances": 6, "baseline_instances": 4},
        )
        assert result.allowed is True

    def test_exceeds_upper_bound_is_rejected(self):
        # 基线 4，上限 4*2.0=8，提议 10 越界。
        result = check_target_in_scope(
            "scale_instances", self.SCOPE, {"target_instances": 10, "baseline_instances": 4},
        )
        assert result.allowed is False
        assert "10" in result.reason

    def test_boundary_value_exactly_at_max_is_allowed(self):
        # 8 == 4*2.0，等于上界应该放行，不是"严格小于才行"。
        result = check_target_in_scope(
            "scale_instances", self.SCOPE, {"target_instances": 8, "baseline_instances": 4},
        )
        assert result.allowed is True

    def test_below_lower_bound_is_rejected(self):
        result = check_target_in_scope(
            "scale_instances", self.SCOPE, {"target_instances": 0, "baseline_instances": 4},
        )
        assert result.allowed is False

    def test_malformed_scope_config_raises(self):
        with pytest.raises(InvalidScopeConfig):
            check_target_in_scope(
                "scale_instances", {"min_instances": 1}, {"target_instances": 2, "baseline_instances": 4},
            )


class TestCleanDiskScope:
    SCOPE = {
        "allowed_path_patterns": ["/var/log/app/*.log"],
        "excluded_path_patterns": ["/var/lib/postgresql/*"],
    }

    def test_allowed_path_is_allowed(self):
        result = check_target_in_scope(
            "clean_disk", self.SCOPE, {"path": "/var/log/app/access.log"},
        )
        assert result.allowed is True

    def test_path_outside_allowlist_is_rejected(self):
        result = check_target_in_scope(
            "clean_disk", self.SCOPE, {"path": "/etc/passwd"},
        )
        assert result.allowed is False

    def test_excluded_pattern_wins_even_when_also_allowed(self):
        """§10.3 硬性要求的那条边界：一个路径同时匹配 allowed 和 excluded 时，
        排除规则必须优先生效——构造一条显式覆盖两者的用例。"""
        scope = {
            "allowed_path_patterns": ["/var/lib/*"],
            "excluded_path_patterns": ["/var/lib/postgresql/*"],
        }
        result = check_target_in_scope(
            "clean_disk", scope, {"path": "/var/lib/postgresql/data/base.dat"},
        )
        assert result.allowed is False
        assert "排除" in result.reason

    def test_malformed_scope_config_raises(self):
        with pytest.raises(InvalidScopeConfig):
            check_target_in_scope("clean_disk", {}, {"path": "/var/log/app/x.log"})


class TestRollbackDeploymentScope:
    SCOPE = {"max_versions_back": 5}

    def test_within_bound_is_allowed(self):
        result = check_target_in_scope(
            "rollback_deployment", self.SCOPE, {"target_version_offset": 3},
        )
        assert result.allowed is True

    def test_at_bound_is_allowed(self):
        result = check_target_in_scope(
            "rollback_deployment", self.SCOPE, {"target_version_offset": 5},
        )
        assert result.allowed is True

    def test_exceeds_bound_is_rejected(self):
        result = check_target_in_scope(
            "rollback_deployment", self.SCOPE, {"target_version_offset": 6},
        )
        assert result.allowed is False

    def test_zero_offset_is_rejected(self):
        # 0 个版本back = 没有回滚，不是一个合法的回滚提议。
        result = check_target_in_scope(
            "rollback_deployment", self.SCOPE, {"target_version_offset": 0},
        )
        assert result.allowed is False

    def test_malformed_scope_config_raises(self):
        with pytest.raises(InvalidScopeConfig):
            check_target_in_scope("rollback_deployment", {}, {"target_version_offset": 1})


class TestApprovalTimeoutBounds:
    def test_default_is_within_bounds(self):
        assert validate_approval_timeout_minutes(DEFAULT_APPROVAL_TIMEOUT_MINUTES) == DEFAULT_APPROVAL_TIMEOUT_MINUTES

    def test_below_minimum_rejected(self):
        with pytest.raises(InvalidApprovalTimeout):
            validate_approval_timeout_minutes(4)

    def test_at_minimum_accepted(self):
        assert validate_approval_timeout_minutes(5) == 5

    def test_above_maximum_rejected(self):
        with pytest.raises(InvalidApprovalTimeout):
            validate_approval_timeout_minutes(24 * 60 + 1)

    def test_at_maximum_accepted(self):
        assert validate_approval_timeout_minutes(24 * 60) == 24 * 60

    def test_zero_rejected(self):
        with pytest.raises(InvalidApprovalTimeout):
            validate_approval_timeout_minutes(0)

    def test_negative_rejected(self):
        with pytest.raises(InvalidApprovalTimeout):
            validate_approval_timeout_minutes(-5)
