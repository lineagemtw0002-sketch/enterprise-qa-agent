"""
智能运维模块 —— 修复目标越界判定（纯函数层）

设计见 `docs/aiops_module_design.md` §3.3.1 / §10.3 / §10.4。

## 这里刻意不碰数据库

跟 `activation.py` / `auth.py::resolve_jwt_secret` 是同一个模式：判定函数只接收
"已经从库里读出来的"配置和"AI 提议的目标"，返回一个结构化结果，不做任何 IO。
这段逻辑是 V1 唯一的硬性安全边界——`docs/aiops_module_design.md` §3.3.1 明确要求
"AI 提议的目标必须落在管理员预先配置的白名单/边界内，否则在进入
`pending_approval` 之前就应被拒绝，不能指望审批人肉眼发现"，把它写成纯函数
才能真正被测试覆盖到每一条边界（四类动作各构造一条越界用例，见 §6 测试设计）。

## 四类动作是 V1 唯一允许的类型，不是示例

`docs/aiops_module_design.md` §0⑥：重启服务/进程、扩容/缩容实例数量、清理磁盘
空间、回滚到上一个部署版本。新增类型需要重新走一次设计确认，不能在实施过程中
悄悄扩展——`ACTION_TYPES` 这个常量因此没有设计成"可配置"，硬编码就是设计意图。

## 排除规则优先于允许规则

§10.3 原文："`clean_disk` 里如果一个路径同时匹配 `allowed_path_patterns` 和
`excluded_path_patterns`，排除规则优先生效，不是先匹配到哪条算哪条。"
`_check_clean_disk` 因此总是先判排除、再判允许，顺序不是随意的。
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any, Dict

# V1 唯一允许的四类动作——硬编码是设计意图，不是遗漏。新增需要重新走设计确认。
ACTION_TYPE_RESTART_SERVICE = "restart_service"
ACTION_TYPE_SCALE_INSTANCES = "scale_instances"
ACTION_TYPE_CLEAN_DISK = "clean_disk"
ACTION_TYPE_ROLLBACK_DEPLOYMENT = "rollback_deployment"

ACTION_TYPES = frozenset({
    ACTION_TYPE_RESTART_SERVICE,
    ACTION_TYPE_SCALE_INSTANCES,
    ACTION_TYPE_CLEAN_DISK,
    ACTION_TYPE_ROLLBACK_DEPLOYMENT,
})

# §10.4：默认 30 分钟，可配置范围 5 分钟～24 小时。下界是为了给审批人留出看完
# "数据血缘"等字段的时间，太短等于形同虚设；上界是避免"设置了超时但形同虚设"。
DEFAULT_APPROVAL_TIMEOUT_MINUTES = 30
MIN_APPROVAL_TIMEOUT_MINUTES = 5
MAX_APPROVAL_TIMEOUT_MINUTES = 24 * 60


class InvalidActionType(ValueError):
    """action_type 不在 V1 允许的四类里。"""


class InvalidScopeConfig(ValueError):
    """scope_config_json 本身的形状不对（管理员配置错了，不是 AI 提议越界）。"""


class InvalidApprovalTimeout(ValueError):
    """approval_timeout_minutes 超出 [5, 1440] 范围。"""


@dataclass(frozen=True)
class ScopeCheckResult:
    """AI 提议的目标是否落在管理员预先登记的白名单/边界内。

    `allowed=False` 时 `reason` 是给审批日志/拒绝提示用的人类可读说明，
    不用于程序判断分支（判断只看 `allowed`）。
    """
    allowed: bool
    reason: str = ""


def validate_action_type(action_type: str) -> None:
    """action_type 必须是 V1 四类之一，否则拒绝——不存在"未知类型默认放行"。"""
    if action_type not in ACTION_TYPES:
        raise InvalidActionType(
            f"不支持的动作类型 '{action_type}'，V1 仅支持 {sorted(ACTION_TYPES)}"
        )


def validate_approval_timeout_minutes(minutes: int) -> int:
    """越界直接拒绝，不静默夹紧（夹紧会让管理员以为自己配置的值生效了）。"""
    if not (MIN_APPROVAL_TIMEOUT_MINUTES <= minutes <= MAX_APPROVAL_TIMEOUT_MINUTES):
        raise InvalidApprovalTimeout(
            f"approval_timeout_minutes={minutes} 超出允许范围 "
            f"[{MIN_APPROVAL_TIMEOUT_MINUTES}, {MAX_APPROVAL_TIMEOUT_MINUTES}]"
        )
    return minutes


def _check_restart_service(scope_config: Dict[str, Any], proposed: Dict[str, Any]) -> ScopeCheckResult:
    allowed_targets = scope_config.get("allowed_targets")
    if not isinstance(allowed_targets, list):
        raise InvalidScopeConfig("restart_service 的 scope_config 缺少 allowed_targets 列表")
    target = proposed.get("target")
    if target in allowed_targets:
        return ScopeCheckResult(allowed=True)
    return ScopeCheckResult(
        allowed=False,
        reason=f"目标服务 '{target}' 不在允许重启的清单 {allowed_targets} 内",
    )


def _check_scale_instances(scope_config: Dict[str, Any], proposed: Dict[str, Any]) -> ScopeCheckResult:
    min_instances = scope_config.get("min_instances")
    max_multiplier = scope_config.get("max_multiplier_of_baseline")
    if min_instances is None or max_multiplier is None:
        raise InvalidScopeConfig(
            "scale_instances 的 scope_config 缺少 min_instances 或 max_multiplier_of_baseline"
        )
    target_instances = proposed.get("target_instances")
    baseline_instances = proposed.get("baseline_instances")
    if target_instances is None or baseline_instances is None:
        return ScopeCheckResult(allowed=False, reason="提议缺少 target_instances 或 baseline_instances")
    max_allowed = baseline_instances * max_multiplier
    if target_instances < min_instances:
        return ScopeCheckResult(
            allowed=False,
            reason=f"目标实例数 {target_instances} 低于下界 {min_instances}",
        )
    if target_instances > max_allowed:
        return ScopeCheckResult(
            allowed=False,
            reason=(
                f"目标实例数 {target_instances} 超过上界 "
                f"{max_allowed}（基线 {baseline_instances} × {max_multiplier}）"
            ),
        )
    return ScopeCheckResult(allowed=True)


def _check_clean_disk(scope_config: Dict[str, Any], proposed: Dict[str, Any]) -> ScopeCheckResult:
    allowed_patterns = scope_config.get("allowed_path_patterns")
    excluded_patterns = scope_config.get("excluded_path_patterns", [])
    if not isinstance(allowed_patterns, list):
        raise InvalidScopeConfig("clean_disk 的 scope_config 缺少 allowed_path_patterns 列表")
    path = proposed.get("path")
    if not path:
        return ScopeCheckResult(allowed=False, reason="提议缺少 path")

    # §10.3 硬性要求：排除规则优先——即便命中了允许模式，只要也命中排除模式
    # 就必须拒绝，顺序不能反过来（不是"先匹配到哪条算哪条"）。
    for pattern in excluded_patterns:
        if fnmatch.fnmatch(path, pattern):
            return ScopeCheckResult(
                allowed=False,
                reason=f"路径 '{path}' 命中排除规则 '{pattern}'（排除规则优先于允许规则）",
            )
    for pattern in allowed_patterns:
        if fnmatch.fnmatch(path, pattern):
            return ScopeCheckResult(allowed=True)
    return ScopeCheckResult(
        allowed=False,
        reason=f"路径 '{path}' 不匹配任何允许的模式 {allowed_patterns}",
    )


def _check_rollback_deployment(scope_config: Dict[str, Any], proposed: Dict[str, Any]) -> ScopeCheckResult:
    max_versions_back = scope_config.get("max_versions_back")
    if max_versions_back is None:
        raise InvalidScopeConfig("rollback_deployment 的 scope_config 缺少 max_versions_back")
    target_version_offset = proposed.get("target_version_offset")
    if target_version_offset is None:
        return ScopeCheckResult(allowed=False, reason="提议缺少 target_version_offset")
    if target_version_offset < 1:
        return ScopeCheckResult(allowed=False, reason="target_version_offset 必须 >= 1（至少回滚一个版本）")
    if target_version_offset > max_versions_back:
        return ScopeCheckResult(
            allowed=False,
            reason=f"回滚 {target_version_offset} 个版本超过允许上限 {max_versions_back}",
        )
    return ScopeCheckResult(allowed=True)


_CHECKERS = {
    ACTION_TYPE_RESTART_SERVICE: _check_restart_service,
    ACTION_TYPE_SCALE_INSTANCES: _check_scale_instances,
    ACTION_TYPE_CLEAN_DISK: _check_clean_disk,
    ACTION_TYPE_ROLLBACK_DEPLOYMENT: _check_rollback_deployment,
}


def check_target_in_scope(
    action_type: str, scope_config: Dict[str, Any], proposed: Dict[str, Any]
) -> ScopeCheckResult:
    """AI 提议的修复目标是否落在管理员预先登记的白名单/边界内。

    这是 §3.3.1 要求的"进入 pending_approval 之前"那道硬拦截——调用方必须在
    生成审批记录之前调这个函数，`allowed=False` 时直接拒绝，不允许流到审批人
    那一步才依赖人肉发现。

    Raises:
        InvalidActionType: action_type 不是 V1 四类之一。
        InvalidScopeConfig: scope_config 本身的形状不对（管理员配置错误，
            不是 AI 提议越界——这两种失败必须能被调用方区分处理）。
    """
    validate_action_type(action_type)
    return _CHECKERS[action_type](scope_config, proposed)
