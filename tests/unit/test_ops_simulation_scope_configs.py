"""`scripts/run_ops_simulation.py::_scope_configs` 的回归保护。

## 这条测试为什么存在

`_scope_configs` 给四类修复动作生成允许范围（白名单）。它的失败形态很难看出来：
**少配一个字段、或少配一整类动作，那类动作的提议会在进入审批之前就被
"默认拒绝"挡下**（`create_proposed_action` 对没配白名单的类型转 `rejected_pre`，
这是刻意的默认拒绝，见 CLAUDE.md §5）。于是演示现场看到的是"提议了但被拒了"，
跟功能真的坏掉在界面上没有任何区别——而这恰恰是这个模块最需要演示成功的一环。

配置本身是一个字典字面量，没有任何东西会在写错时报错：
- `scale_instances` 漏掉 `max_multiplier_of_baseline` → `InvalidScopeConfig`，
  提议时 500 / 拒绝，只有真的走到那一步才看得见；
- `restart_service` 的 `allowed_targets` 跟环境里的服务名对不上 → 每一次
  重启提议都越界；
- 新增第五类动作却忘了在这里配 → 那一类永远空跑。

所以这里断言的是**生成的配置能被 `aiops_scope` 真正接受**，不是断言字典长什么样
（后者等于把实现抄一遍，判别力为零）。

## 判别力是怎么确认的

`TestMutantsFailTheseAssertions` 把 `_scope_configs` 真实返回的配置按三种典型
写错方式改坏（删字段 / 删整类 / 服务名对不上），跑同一批断言拿到失败，
证明上面那些断言不是摆设。这是对**真实返回值**做变异，不是照抄一份错误实现。
"""

from __future__ import annotations

import copy
from typing import Any, Dict

import pytest

from scripts.run_ops_simulation import _scope_configs
from services.ops_probe_demo.environments import ENVIRONMENTS, FAULT_KINDS
from src.ragent_backend.aiops_scope import (
    ACTION_TYPE_CLEAN_DISK,
    ACTION_TYPE_RESTART_SERVICE,
    ACTION_TYPE_ROLLBACK_DEPLOYMENT,
    ACTION_TYPE_SCALE_INSTANCES,
    ACTION_TYPES,
    InvalidScopeConfig,
    check_target_in_scope,
)

# 三套仿真环境里最"大"的那套，服务名跟真实调用点一致：
# `_bring_up` 传的是 `sorted(env.services)`。
_ECOMMERCE_SERVICES = sorted(ENVIRONMENTS["ecommerce"].services)


def _configs(services=None) -> Dict[str, Dict[str, Any]]:
    return _scope_configs(services if services is not None else _ECOMMERCE_SERVICES)


class TestAllFourActionTypesAreConfigured:
    def test_covers_exactly_the_v1_action_types(self):
        """四类一个都不能少，也不能多出 `aiops_scope` 不认识的类型。

        少一类 = 演示里那一类被默认拒绝（看起来像功能坏了）；
        多一类 = `PUT .../remediation-scopes/{action_type}` 那一步直接 400，
        脚本会在建环境时就崩掉。
        """
        assert set(_configs()) == set(ACTION_TYPES)

    def test_every_demonstrable_fault_has_a_matching_scope(self):
        """每一种可注入故障建议的修复动作，都得有对应的允许范围。

        `environments.py` 已经用一条断言钉住"每类动作都有故障能演出来"，
        这条钉的是另一半：**故障演出来了，修复动作也得能提议成功**。
        两边只有一边成立时，演示会停在"提议被拒"那一格。
        """
        suggested = {k.suggested_action for k in FAULT_KINDS.values()}
        assert suggested <= set(_configs()), (
            f"这些故障建议的修复动作没有配允许范围：{sorted(suggested - set(_configs()))}"
        )


class TestConfigsAreAcceptedByTheValidator:
    """配置形状合法 = `check_target_in_scope` 不抛 `InvalidScopeConfig`。

    刻意传空提议 `{}`：这样越界判定必然走到"提议缺字段"那一支，
    于是**唯一可能抛 `InvalidScopeConfig` 的原因就只剩配置本身有问题**，
    把"管理员配错了"和"AI 提议越界"这两类失败干净地分开
    （`aiops_scope` 的 docstring 明确要求调用方能区分这两者）。
    """

    @pytest.mark.parametrize("action_type", sorted(ACTION_TYPES))
    def test_scope_config_shape_is_valid(self, action_type):
        configs = _configs()
        result = check_target_in_scope(action_type, configs[action_type], {})
        # 空提议当然不该被放行——这里只关心它没有因为配置本身而抛异常。
        assert result.allowed is False


class TestRestartServiceTargets:
    def test_every_service_of_every_environment_is_restartable(self):
        """三套环境各自建连接器时都会调一次 `_scope_configs(sorted(env.services))`。

        服务名跟白名单对不上是"重启提议永远越界"的唯一成因，
        而它在界面上的样子是"AI 提了一个被拒的建议"，很像模型不聪明。
        """
        for env in ENVIRONMENTS.values():
            configs = _scope_configs(sorted(env.services))
            for service in env.services:
                result = check_target_in_scope(
                    ACTION_TYPE_RESTART_SERVICE, configs[ACTION_TYPE_RESTART_SERVICE],
                    {"target": service},
                )
                assert result.allowed is True, f"{env.key}/{service} 不在允许重启的清单里"

    def test_service_from_another_environment_is_rejected(self):
        """判别力对照：白名单不是"什么都放行"。

        没有这条的话，一个恒返回 `allowed_targets=["*"]`（或者干脆放行一切）
        的实现也能让上面那条全绿。
        """
        configs = _scope_configs(sorted(ENVIRONMENTS["ecommerce"].services))
        result = check_target_in_scope(
            ACTION_TYPE_RESTART_SERVICE, configs[ACTION_TYPE_RESTART_SERVICE],
            {"target": "ledger-service"},  # 这是 payments 环境的服务
        )
        assert result.allowed is False


class TestScaleInstances:
    """⚠️ 基线走 `measured_baseline_instances`（平台实测），不是提议里那个
    `baseline_instances`——2026-08-27 修掉"被约束方自己填写约束"那个自指缺口
    之后的契约。演示链路里这个键由 `with_measured_baseline` 向探针实测后注入，
    所以这里的提议也照那个形状构造，不用 AI 自陈述的字段。
    """

    def test_realistic_scale_up_is_allowed(self):
        # 队列积压/流量激增两种故障建议的就是扩容，这是演示里真会走的提议。
        result = check_target_in_scope(
            ACTION_TYPE_SCALE_INSTANCES, _configs()[ACTION_TYPE_SCALE_INSTANCES],
            {"target": "notification-worker", "measured_baseline_instances": 2,
             "target_instances": 4},
        )
        assert result.allowed is True

    @pytest.mark.parametrize("env_key,service", [
        ("ecommerce", "notification-worker"),   # queue_backlog 会打在它身上
        ("payments", "settlement-worker"),
        ("internal", "ci-runner"),
    ])
    def test_demo_multiplier_leaves_room_for_a_real_scale_up(self, env_key, service):
        """把演示配置和演示数据绑在一起看：探针实测报上来的实例数，
        在 `max_multiplier_of_baseline=3` 下必须还扩得动。

        这两边是分开写的——`environments.py` 调实例数、`_scope_configs` 调倍数，
        谁都不知道对方。倍数配得太小（比如 1）时，演示里"扩容"这一类
        提议会**全部被拦下**，而现场看到的只是"AI 提的建议被拒了"。
        """
        spec = ENVIRONMENTS[env_key].services[service]
        configs = _scope_configs(sorted(ENVIRONMENTS[env_key].services))
        result = check_target_in_scope(
            ACTION_TYPE_SCALE_INSTANCES, configs[ACTION_TYPE_SCALE_INSTANCES],
            {"target": service, "measured_baseline_instances": float(spec.instances),
             "target_instances": spec.instances * 2},
        )
        assert result.allowed is True, (
            f"{env_key}/{service} 实测基线 {spec.instances}，翻倍扩容却被拦下——"
            "演示里的扩容动作会全部停在 rejected_pre"
        )

    def test_scaling_beyond_the_multiplier_is_rejected(self):
        # 判别力对照：上界真的在起作用，不是配了个形同虚设的值。
        result = check_target_in_scope(
            ACTION_TYPE_SCALE_INSTANCES, _configs()[ACTION_TYPE_SCALE_INSTANCES],
            {"target": "notification-worker", "measured_baseline_instances": 2,
             "target_instances": 99},
        )
        assert result.allowed is False

    def test_scaling_below_the_floor_is_rejected(self):
        # `min_instances` 存在的意义：缩容不能把服务缩到 0 实例（那是停服，
        # 不是修复）。配 0 或漏配都会让这条边界消失。
        result = check_target_in_scope(
            ACTION_TYPE_SCALE_INSTANCES, _configs()[ACTION_TYPE_SCALE_INSTANCES],
            {"target": "notification-worker", "measured_baseline_instances": 4,
             "target_instances": 0},
        )
        assert result.allowed is False


class TestCleanDisk:
    def test_log_path_is_allowed(self):
        # `disk_full` 故障建议的就是清理磁盘，演示时提议的正是这类路径。
        result = check_target_in_scope(
            ACTION_TYPE_CLEAN_DISK, _configs()[ACTION_TYPE_CLEAN_DISK],
            {"target": "order-service", "path": "/var/log/order-service/access.log.1"},
        )
        assert result.allowed is True

    def test_data_directory_is_rejected(self):
        """数据目录永远不许清——这是演示"越界提议会被拦下"最好用的一条。

        配置里 `excluded_path_patterns` 写了 `/var/lib/*`，即使将来有人往
        `allowed_path_patterns` 里加了更宽的模式，排除规则也优先（§10.3）。
        """
        result = check_target_in_scope(
            ACTION_TYPE_CLEAN_DISK, _configs()[ACTION_TYPE_CLEAN_DISK],
            {"target": "order-service", "path": "/var/lib/postgresql/data/base.dat"},
        )
        assert result.allowed is False

    def test_path_traversal_out_of_an_allowed_prefix_is_rejected(self):
        # 这条配置组合（allowed 含 /var/log/*，excluded 含 /var/lib/*）正是
        # 路径穿越防护的现实场景：字面串匹配下 `..` 能从允许前缀跳进被排除的目录。
        result = check_target_in_scope(
            ACTION_TYPE_CLEAN_DISK, _configs()[ACTION_TYPE_CLEAN_DISK],
            {"target": "order-service", "path": "/var/log/app/../../lib/postgresql/data/base.dat"},
        )
        assert result.allowed is False


class TestRollbackDeployment:
    def test_rolling_back_one_version_is_allowed(self):
        # `bad_release` 故障建议的就是回滚，演示里几乎必然是回退 1 个版本。
        result = check_target_in_scope(
            ACTION_TYPE_ROLLBACK_DEPLOYMENT, _configs()[ACTION_TYPE_ROLLBACK_DEPLOYMENT],
            {"target": "order-service", "target_version_offset": 1},
        )
        assert result.allowed is True

    def test_rolling_back_too_far_is_rejected(self):
        result = check_target_in_scope(
            ACTION_TYPE_ROLLBACK_DEPLOYMENT, _configs()[ACTION_TYPE_ROLLBACK_DEPLOYMENT],
            {"target": "order-service", "target_version_offset": 9},
        )
        assert result.allowed is False


# ---------------------------------------------------------------------------
# 判别力自查（CLAUDE.md §7.2）
# ---------------------------------------------------------------------------

class TestMutantsFailTheseAssertions:
    """把 `_scope_configs` **真实返回的**配置按三种典型写错方式改坏，
    证明上面的断言真的会红。不是照抄一份错误实现来自证。
    """

    def test_missing_required_field_raises(self):
        broken = copy.deepcopy(_configs())
        broken[ACTION_TYPE_SCALE_INSTANCES].pop("max_multiplier_of_baseline")
        with pytest.raises(InvalidScopeConfig):
            check_target_in_scope(ACTION_TYPE_SCALE_INSTANCES, broken[ACTION_TYPE_SCALE_INSTANCES], {})

    def test_missing_action_type_is_caught_by_the_coverage_assertion(self):
        broken = copy.deepcopy(_configs())
        broken.pop(ACTION_TYPE_CLEAN_DISK)
        assert set(broken) != set(ACTION_TYPES), (
            "少配一类动作必须被 TestAllFourActionTypesAreConfigured 抓到"
        )
        suggested = {k.suggested_action for k in FAULT_KINDS.values()}
        assert not suggested <= set(broken), (
            "`disk_full` 故障还在，但它建议的 clean_disk 没有允许范围了——"
            "test_every_demonstrable_fault_has_a_matching_scope 会因此变红"
        )

    def test_wrong_service_names_break_every_restart_proposal(self):
        # 典型写错方式：传进去的服务名跟环境里的对不上（比如写死了一份旧清单）。
        stale = _scope_configs(["订单服务", "支付网关"])
        for service in ENVIRONMENTS["ecommerce"].services:
            result = check_target_in_scope(
                ACTION_TYPE_RESTART_SERVICE, stale[ACTION_TYPE_RESTART_SERVICE],
                {"target": service},
            )
            assert result.allowed is False, (
                "服务名对不上时每一条重启提议都会越界——"
                "test_every_service_of_every_environment_is_restartable 会因此变红"
            )
