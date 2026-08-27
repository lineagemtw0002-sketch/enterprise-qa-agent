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
import posixpath
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


def validate_approval_timeout_minutes(minutes: Any) -> int:
    """越界直接拒绝，不静默夹紧（夹紧会让管理员以为自己配置的值生效了）。

    2026-08-26 修复两条类型缺陷（变异测试实测发现）：① 字符串输入原来会在
    区间比较那一步直接抛裸 `TypeError`，不是这个函数自己的 `InvalidApprovalTimeout`
    ——调用方没法用一致的方式捕获"配置不合法"这类错误。② 函数签名声称
    `-> int`，但 `30.5` 这样的浮点数会被原样接受并返回，落库后语义不明
    （"30.5 分钟"不是一个有意义的超时配置）。`bool` 同样显式排除，
    理由跟 `_require_config_number` 一致：`isinstance(True, int)` 为真，
    不排除的话 `approval_timeout_minutes=True` 会被当成 1 分钟接受。"""
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        raise InvalidApprovalTimeout(
            f"approval_timeout_minutes 必须是整数分钟数，收到 {minutes!r}"
        )
    if not (MIN_APPROVAL_TIMEOUT_MINUTES <= minutes <= MAX_APPROVAL_TIMEOUT_MINUTES):
        raise InvalidApprovalTimeout(
            f"approval_timeout_minutes={minutes} 超出允许范围 "
            f"[{MIN_APPROVAL_TIMEOUT_MINUTES}, {MAX_APPROVAL_TIMEOUT_MINUTES}]"
        )
    return minutes


def _require_config_number(scope_config: Dict[str, Any], field: str, action_type: str) -> float:
    """`scope_config` 里的数值字段必须是真的数字，不是字符串/None/其它——
    2026-08-26 变异测试实测发现：不校验的话，管理员把 `max_multiplier_of_baseline`
    填成字符串会在比较/乘法运算时直接抛裸 `TypeError`，把"配置本身有问题"
    伪装成一次未知的服务器错误，而不是 `check_target_in_scope` docstring
    承诺的"能被调用方区分成 InvalidScopeConfig"。`bool` 显式排除——
    `isinstance(True, int)` 在 Python 里是 `True`，混进数值字段会是一个
    容易被忽略的行为，宁可拒绝也不要让 `min_instances=true` 被当成 1。"""
    value = scope_config.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidScopeConfig(
            f"{action_type} 的 scope_config 字段 '{field}' 必须是数字，收到 {value!r}"
        )
    return value


def _require_proposed_number(proposed: Dict[str, Any], field: str) -> Any:
    """返回一个 `float`（合法时）或 `ScopeCheckResult`（不合法时）——调用方
    用 `isinstance(value, ScopeCheckResult)` 判断，命中就直接 `return value`。
    `proposed`（AI 提议）里的数值字段类型不对时不抛异常：跟
    `_require_config_number` 的区别是这里错的是 AI 的提议，不是管理员的
    配置，属于"越界/不合法的提议"这个既有分类，不该升级成
    `InvalidScopeConfig`（那个专门留给管理员配置本身的问题）。"""
    value = proposed.get(field)
    if value is None:
        return ScopeCheckResult(allowed=False, reason=f"提议缺少 {field}")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ScopeCheckResult(allowed=False, reason=f"提议的 {field} 不是合法数字：{value!r}")
    return value


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
    if "min_instances" not in scope_config or "max_multiplier_of_baseline" not in scope_config:
        raise InvalidScopeConfig(
            "scale_instances 的 scope_config 缺少 min_instances 或 max_multiplier_of_baseline"
        )
    min_instances = _require_config_number(scope_config, "min_instances", "scale_instances")
    max_multiplier = _require_config_number(scope_config, "max_multiplier_of_baseline", "scale_instances")

    target_instances = _require_proposed_number(proposed, "target_instances")
    if isinstance(target_instances, ScopeCheckResult):
        return target_instances
    # ⚠️ **基线只认平台实测值，不认 AI 自陈述**（2026-08-27 用户拍板修复）。
    #
    # 原来的实现里 `target_instances` 和 `baseline_instances` 都来自同一份 AI
    # 提议——模型谎报一个虚高基线就能把自己的天花板抬到任意高
    # （baseline=5000 / target=10000 在 multiplier=2.0 下被判合法）。
    # 一个由被约束方自己填写的约束，不是约束。
    #
    # 现在基线走 `measured_baseline_instances`：**只有平台侧的调用方能填**，
    # 值来自向连接器实测（见 `src/ops/measured_baseline.py`）。
    # 提议里那个 `baseline_instances` 保留但**不参与判定**——它现在只是
    # "模型以为有几个实例"，跟实测值不一致本身是个有用的信号（模型看错了、
    # 或者状态在提议之后变了），留给上层去比对。
    #
    # ⚠️ **缺失即拒绝，不是跳过。** 调用方没测就调进来（连接器离线、忘了测），
    # 这里必须拒绝而不是回退到 AI 自报的值——回退等于这个修复在最需要它的
    # 时候（连接器不可达）自动失效。判定不了就不放行，是这个模块一贯的默认。
    measured = proposed.get("measured_baseline_instances")
    if measured is None:
        return ScopeCheckResult(
            allowed=False,
            reason=("无法确认该服务当前的实例数（未取到实测基线），因此无法判定扩缩容"
                    "是否越界。连接器离线或不支持上报实例数时，扩缩容一律不予放行。"),
        )
    baseline_instances = _require_proposed_number(proposed, "measured_baseline_instances")
    if isinstance(baseline_instances, ScopeCheckResult):
        return baseline_instances
    if baseline_instances <= 0:
        return ScopeCheckResult(
            allowed=False,
            reason=f"实测基线实例数为 {baseline_instances}，无法据此推算上界",
        )

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
                f"{max_allowed}（实测基线 {baseline_instances} × {max_multiplier}）"
            ),
        )
    return ScopeCheckResult(allowed=True)


def _check_clean_disk(scope_config: Dict[str, Any], proposed: Dict[str, Any]) -> ScopeCheckResult:
    allowed_patterns = scope_config.get("allowed_path_patterns")
    excluded_patterns = scope_config.get("excluded_path_patterns", [])
    if not isinstance(allowed_patterns, list) or not all(isinstance(p, str) for p in allowed_patterns):
        raise InvalidScopeConfig("clean_disk 的 scope_config 缺少 allowed_path_patterns 字符串列表")
    # 2026-08-26 修复：原来只校验了 allowed_patterns 是不是 list，
    # excluded_patterns 完全没校验——而排除规则才是安全侧那一半，配置错误在
    # 这里比在 allowed 那边危险得多（allowed 配错顶多误拒，excluded 配错是
    # 误放行）。含 "*" 的坏字符串会在 fnmatch 里被当成"匹配一切"（意外地
    # fail-closed，不危险但会造成"整条规则失效"这种难排查的现象）；不含
    # "*" 的坏字符串会静默永远不匹配（fail-open，真正的危险形态）；None
    # 混进列表会在 fnmatch.fnmatch 里直接抛 TypeError。三种情形只有在这里
    # 提前拒绝配置才能统一处理，不能指望 fnmatch 自己报出有意义的错误。
    if not isinstance(excluded_patterns, list) or not all(isinstance(p, str) for p in excluded_patterns):
        raise InvalidScopeConfig("clean_disk 的 scope_config 的 excluded_path_patterns 必须是字符串列表")

    path = proposed.get("path")
    if not path or not isinstance(path, str):
        return ScopeCheckResult(allowed=False, reason="提议缺少 path")

    # 2026-08-26 修复：路径穿越可以击穿排除规则——`fnmatch` 是纯字面串匹配，
    # 不做路径规范化。`/var/log/app/../../lib/postgresql/data/base.dat`
    # 字面上匹配 `allowed_path_patterns=["/var/log/app/*"]`（`*` 吞掉了
    # 后面的 `../..`），却完全不匹配
    # `excluded_path_patterns=["/var/lib/postgresql/*"]`（字符串不是以这个
    # 前缀开头）——但操作系统真正处理这个路径时会规范化成
    # `/var/lib/postgresql/data/base.dat`，正是排除规则要保护的那个文件。
    # 這不是"边界条件没考虑周全"，是排除规则这道闸本身可以被绕过去。
    #
    # 修法：**先拒绝任何包含 `..` 路径段的提议**，不试图"规范化之后再判断"
    # ——一个真实的清理目标（AI 从连接器实际观察到的磁盘路径）永远不需要
    # `..`，允许它存在没有任何合法理由，直接拒绝比"规范化+信任结果"更简单
    # 也更不容易被绕过（规范化本身要考虑符号链接、大小写等一整类新问题，
    # 不该在这一层引入）。同时也拒绝非绝对路径——相对路径的"允许/排除前缀"
    # 判断在语义上没有意义（相对于哪里？），一律按不合法处理。
    if not path.startswith("/"):
        return ScopeCheckResult(allowed=False, reason=f"路径 '{path}' 不是绝对路径，拒绝")
    if ".." in path.split("/"):
        return ScopeCheckResult(allowed=False, reason=f"路径 '{path}' 包含 '..'，拒绝（路径穿越防护）")
    # 规范化只用来处理冗余分隔符（"//", 结尾 "/" 等无害差异），不是穿越防护
    # 本身——真正的防护是上面那条 ".." 硬拒绝。两者都做是纵深防御。
    normalized_path = posixpath.normpath(path)

    # §10.3 硬性要求：排除规则优先——即便命中了允许模式，只要也命中排除模式
    # 就必须拒绝，顺序不能反过来（不是"先匹配到哪条算哪条"）。
    for pattern in excluded_patterns:
        if fnmatch.fnmatch(normalized_path, pattern):
            return ScopeCheckResult(
                allowed=False,
                reason=f"路径 '{path}' 命中排除规则 '{pattern}'（排除规则优先于允许规则）",
            )
    for pattern in allowed_patterns:
        if fnmatch.fnmatch(normalized_path, pattern):
            return ScopeCheckResult(allowed=True)
    return ScopeCheckResult(
        allowed=False,
        reason=f"路径 '{path}' 不匹配任何允许的模式 {allowed_patterns}",
    )


def _check_rollback_deployment(scope_config: Dict[str, Any], proposed: Dict[str, Any]) -> ScopeCheckResult:
    if "max_versions_back" not in scope_config:
        raise InvalidScopeConfig("rollback_deployment 的 scope_config 缺少 max_versions_back")
    max_versions_back = _require_config_number(scope_config, "max_versions_back", "rollback_deployment")

    target_version_offset = _require_proposed_number(proposed, "target_version_offset")
    if isinstance(target_version_offset, ScopeCheckResult):
        return target_version_offset
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
