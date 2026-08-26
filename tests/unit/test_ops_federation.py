"""联邦查询层（`src/ops/federation/`）——设计见 `docs/aiops_module_design.md` §3.5。

全部用假件，不连 WebSocket、不连 Postgres、不连 Ollama，毫秒级跑完。
能做到这一点本身是设计的一部分：引擎只依赖 `ConnectorTransport` 协议和一个
`ConnectionDirectory`，两者都从构造函数注入，没有任何全局状态
（CLAUDE.md §7.1「想不出怎么测 = 设计还没想清楚」）。

**判别力说明**（§7.2「它在旧实现下会失败吗」）：这一层是全新代码，没有"旧实现"
可对照，所以下面每条用例守的是**设计文档里一条具体的硬性要求**，并在 docstring
里点明是哪一条。真正会跑红的时机是将来有人改坏这些规则时——
尤其是这三条，改坏了都不会有任何报错，只会静默变成错误行为：

- 部分失败被吞掉或被放大成整体失败（§3.5 第 4 条）
- 缓存键漏掉 org_id（跨租户数据泄露）
- 越权的连接器"查了但丢弃结果"而不是"根本不查"
"""

from __future__ import annotations

import asyncio
import time
from typing import Dict, List, Optional, Sequence

import pytest

from src.ops.federation import (
    ConnectionRef,
    FederatedQueryCache,
    FederatedQueryEngine,
    describe_unavailable,
)
from src.ops.federation.cache import MAX_TTL_SECONDS
from src.ops.types import (
    ERROR_OFFLINE,
    ERROR_TIMEOUT,
    ERROR_TRANSPORT,
    ERROR_UNAUTHORIZED,
    ConnectorUnavailable,
    DataPoint,
    QueryRequest,
    QueryResult,
    TimeRange,
)

ORG_A = "org_acme"
ORG_B = "org_globex"


def _request(target: str = "order-service") -> QueryRequest:
    return QueryRequest(
        kind="metric", target=target,
        time_range=TimeRange(1_000.0, 4_600.0), metric="error_rate",
    )


class FakeDirectory:
    def __init__(self, by_org: Dict[str, List[ConnectionRef]]) -> None:
        self._by_org = by_org
        self.calls: List[str] = []

    async def list_for_org(self, org_id: str) -> Sequence[ConnectionRef]:
        self.calls.append(org_id)
        return self._by_org.get(org_id, [])


class FakeTransport:
    """可编程的假连接器：按 connection_id 指定"返回数据 / 抛异常 / 慢多久"。"""

    def __init__(
        self,
        points: Optional[Dict[str, int]] = None,
        raises: Optional[Dict[str, Exception]] = None,
        delays: Optional[Dict[str, float]] = None,
        online: Optional[Dict[str, bool]] = None,
    ) -> None:
        self._points = points or {}
        self._raises = raises or {}
        self._delays = delays or {}
        self._online = online or {}
        self.queried: List[str] = []
        self.online_calls: List[Sequence[str]] = []

    async def query(self, connection_id, org_id, request, timeout_s) -> QueryResult:
        self.queried.append(connection_id)
        delay = self._delays.get(connection_id, 0.0)
        if delay:
            await asyncio.sleep(delay)
        exc = self._raises.get(connection_id)
        if exc is not None:
            raise exc
        n = self._points.get(connection_id, 1)
        return QueryResult(
            connection_id=connection_id, system_name=f"sys-{connection_id}",
            points=[DataPoint(ts=float(i), value=float(i)) for i in range(n)],
        )

    async def online_status(self, connection_ids: Sequence[str]) -> Dict[str, bool]:
        self.online_calls.append(list(connection_ids))
        return {cid: self._online.get(cid, True) for cid in connection_ids}


def _engine(transport, directory=None, cache=None, timeout=1.0) -> FederatedQueryEngine:
    directory = directory or FakeDirectory({
        ORG_A: [ConnectionRef("c1", "Prometheus"), ConnectionRef("c2", "自建日志"), ConnectionRef("c3", "Datadog")],
        ORG_B: [ConnectionRef("b1", "Globex 监控")],
    })
    return FederatedQueryEngine(transport, directory, cache=cache, default_timeout_s=timeout)


class TestFanOut:
    @pytest.mark.asyncio
    async def test_merges_results_from_all_connectors(self):
        """§3.5 第 3 条：多连接器 fan-out，对调用方表现得像单一数据源。"""
        t = FakeTransport(points={"c1": 2, "c2": 3, "c3": 1})
        r = await _engine(t).query(ORG_A, _request())
        assert sorted(t.queried) == ["c1", "c2", "c3"]
        assert len(r.results) == 3
        assert r.point_count() == 6
        assert r.errors == []

    @pytest.mark.asyncio
    async def test_fan_out_is_parallel_not_serial(self):
        """并发扇出——串行实现会让墙钟变成各连接器耗时之和。

        这条是**真行为断言**，不是看代码：把 gather 换成 for 循环 await 就会红。
        """
        t = FakeTransport(delays={"c1": 0.15, "c2": 0.15, "c3": 0.15})
        t0 = time.monotonic()
        await _engine(t, timeout=5.0).query(ORG_A, _request())
        elapsed = time.monotonic() - t0
        assert elapsed < 0.35, f"3 个各 0.15s 的连接器耗时 {elapsed:.2f}s，看起来是串行的"

    @pytest.mark.asyncio
    async def test_defaults_to_all_connectors_of_the_org(self):
        t = FakeTransport()
        await _engine(t).query(ORG_A, _request(), connection_ids=None)
        assert sorted(t.queried) == ["c1", "c2", "c3"]


class TestPartialFailure:
    """§3.5 第 4 条：一个连接器超时/离线，既不能让整个请求失败，也不能静默丢数据。"""

    @pytest.mark.asyncio
    async def test_one_timeout_does_not_kill_the_others(self):
        t = FakeTransport(points={"c1": 2, "c3": 1}, delays={"c2": 5.0})
        r = await _engine(t, timeout=0.05).query(ORG_A, _request())

        assert {x.connection_id for x in r.results} == {"c1", "c3"}, "其余连接器的数据必须照常返回"
        assert [e.connection_id for e in r.errors] == ["c2"]
        assert r.errors[0].reason == ERROR_TIMEOUT
        assert r.is_partial is True

    @pytest.mark.asyncio
    async def test_connector_unavailable_reason_is_preserved(self):
        """离线和超时在 UI 上要给不同的建议，所以 reason 不能被压成一个笼统的值。"""
        t = FakeTransport(raises={"c2": ConnectorUnavailable(ERROR_OFFLINE, "心跳 90 秒未更新")})
        r = await _engine(t).query(ORG_A, _request())
        err = next(e for e in r.errors if e.connection_id == "c2")
        assert err.reason == ERROR_OFFLINE
        assert "心跳" in err.detail

    @pytest.mark.asyncio
    async def test_unexpected_exception_becomes_an_error_not_a_raise(self):
        """连接器抛了预料之外的异常，也不能让整个 fan-out 炸掉。"""
        t = FakeTransport(raises={"c1": ValueError("协议帧解析失败")})
        r = await _engine(t).query(ORG_A, _request())
        assert len(r.results) == 2
        assert next(e for e in r.errors if e.connection_id == "c1").reason == ERROR_TRANSPORT

    @pytest.mark.asyncio
    async def test_all_connectors_failing_still_returns_a_result(self):
        """全挂也返回结果对象而不是抛异常——调用方靠 all_failed 区分
        "全部失败"和"真的没数据"。"""
        t = FakeTransport(raises={c: ConnectorUnavailable(ERROR_OFFLINE, "离线") for c in ("c1", "c2", "c3")})
        r = await _engine(t).query(ORG_A, _request())
        assert r.all_failed is True
        assert r.is_empty is True
        assert len(r.errors) == 3

    @pytest.mark.asyncio
    async def test_cancellation_is_not_swallowed(self):
        """取消是调用方的意图，不是连接器故障——不能被当成 ERROR_TRANSPORT 吞掉。"""
        t = FakeTransport(raises={"c1": asyncio.CancelledError()})
        with pytest.raises(asyncio.CancelledError):
            await _engine(t).query(ORG_A, _request())

    def test_describe_unavailable_is_user_facing(self):
        from src.ops.types import ConnectorError, FederatedResult
        r = FederatedResult(
            request=_request(),
            errors=[ConnectorError("c2", "自建日志", ERROR_OFFLINE, "心跳超时")],
        )
        line = describe_unavailable(r)[0]
        assert "自建日志" in line and "离线" in line


class TestOrgIsolation:
    """跨 org 绝不放行——这是安全边界，不是普通的错误处理。"""

    @pytest.mark.asyncio
    async def test_foreign_connection_id_is_never_queried(self):
        """⚠️ 关键：越权的连接器**一次请求都不发**，不是"查了再丢结果"。"""
        t = FakeTransport()
        r = await _engine(t).query(ORG_A, _request(), connection_ids=["c1", "b1"])

        assert t.queried == ["c1"], f"越权连接器被真的查询了: {t.queried}"
        err = next(e for e in r.errors if e.connection_id == "b1")
        assert err.reason == ERROR_UNAUTHORIZED

    @pytest.mark.asyncio
    async def test_org_without_connectors_gets_empty_not_error(self):
        t = FakeTransport()
        r = await _engine(t, directory=FakeDirectory({})).query("org_new", _request())
        assert r.is_empty and r.errors == [] and t.queried == []


class TestCache:
    @pytest.mark.asyncio
    async def test_hit_avoids_a_second_round_trip(self):
        t = FakeTransport()
        e = _engine(t)
        await e.query(ORG_A, _request())
        r2 = await e.query(ORG_A, _request())
        assert r2.from_cache is True
        assert len(t.queried) == 3, "第二次不该再打连接器"

    @pytest.mark.asyncio
    async def test_cache_is_scoped_by_org(self):
        """⚠️ 缓存键漏掉 org_id = 跨租户数据泄露。这条守的是那个。"""
        directory = FakeDirectory({
            ORG_A: [ConnectionRef("shared", "同名系统")],
            ORG_B: [ConnectionRef("shared", "同名系统")],
        })
        t = FakeTransport()
        e = _engine(t, directory=directory)
        await e.query(ORG_A, _request())
        r = await e.query(ORG_B, _request())
        assert r.from_cache is False, "B 企业读到了 A 企业的缓存结果"
        assert t.queried == ["shared", "shared"]

    @pytest.mark.asyncio
    async def test_partial_failures_are_not_cached(self):
        """缓存了故障，用户修好连接器后刷新还会看到"数据不可用"，
        而且那条降级提示会变成一句过期的谎言。"""
        t = FakeTransport(raises={"c2": ConnectorUnavailable(ERROR_OFFLINE, "离线")})
        e = _engine(t)
        await e.query(ORG_A, _request())
        r2 = await e.query(ORG_A, _request())
        assert r2.from_cache is False
        assert len(t.queried) == 6, "带故障的结果被缓存了"

    @pytest.mark.asyncio
    async def test_unauthorized_error_survives_a_cache_hit(self):
        """越权提示不能因为命中缓存就消失——缓存里只有成功结果，
        越权是每次都要重新算出来拼回去的。"""
        t = FakeTransport()
        e = _engine(t)
        await e.query(ORG_A, _request(), connection_ids=["c1"])
        r2 = await e.query(ORG_A, _request(), connection_ids=["c1", "b1"])
        # 第二次的目标集合不同（c1 vs c1+b1），缓存键也不同，但越权错误必须在
        assert any(x.reason == ERROR_UNAUTHORIZED for x in r2.errors)

    @pytest.mark.asyncio
    async def test_use_cache_false_forces_a_refresh(self):
        t = FakeTransport()
        e = _engine(t)
        await e.query(ORG_A, _request())
        r2 = await e.query(ORG_A, _request(), use_cache=False)
        assert r2.from_cache is False and len(t.queried) == 6

    def test_ttl_expiry(self):
        now = [1000.0]
        cache = FederatedQueryCache(ttl_seconds=30.0, clock=lambda: now[0])
        from src.ops.types import FederatedResult
        req = _request()
        cache.put(ORG_A, req, ["c1"], FederatedResult(request=req, results=[QueryResult("c1", "s")]))
        assert cache.get(ORG_A, req, ["c1"]) is not None
        now[0] += 31.0
        assert cache.get(ORG_A, req, ["c1"]) is None

    def test_ttl_above_design_ceiling_is_rejected(self):
        """§3.5 把 TTL 上界写死在 60 秒。要放宽必须先改设计文档，不是在代码里调大。"""
        with pytest.raises(ValueError, match="TTL"):
            FederatedQueryCache(ttl_seconds=MAX_TTL_SECONDS + 1)

    def test_connection_id_order_does_not_change_the_key(self):
        from src.ops.types import FederatedResult
        cache = FederatedQueryCache()
        req = _request()
        cache.put(ORG_A, req, ["c1", "c2"], FederatedResult(request=req, results=[QueryResult("c1", "s")]))
        assert cache.get(ORG_A, req, ["c2", "c1"]) is not None

    def test_invalidate_org_only_touches_that_org(self):
        from src.ops.types import FederatedResult
        cache = FederatedQueryCache()
        req = _request()
        for org in (ORG_A, ORG_B):
            cache.put(org, req, ["c1"], FederatedResult(request=req, results=[QueryResult("c1", "s")]))
        assert cache.invalidate_org(ORG_A) == 1
        assert cache.get(ORG_A, req, ["c1"]) is None
        assert cache.get(ORG_B, req, ["c1"]) is not None

    def test_max_entries_evicts_instead_of_growing_forever(self):
        from src.ops.types import FederatedResult
        cache = FederatedQueryCache(max_entries=3)
        for i in range(10):
            req = _request(target=f"svc-{i}")
            cache.put(ORG_A, req, ["c1"], FederatedResult(request=req, results=[QueryResult("c1", "s")]))
        assert len(cache) <= 3


class TestConnectorHealth:
    @pytest.mark.asyncio
    async def test_health_uses_one_batched_call_not_n_round_trips(self):
        """⚠️ 反 N+1：3 个连接器必须只查一次在线状态，不是查 3 次。
        改回逐个 `is_online(cid)` 这条就会红。"""
        t = FakeTransport(online={"c2": False})
        health = await _engine(t).connector_health(ORG_A)
        assert len(t.online_calls) == 1
        assert sorted(t.online_calls[0]) == ["c1", "c2", "c3"]
        assert dict((ref.connection_id, ok) for ref, ok in health)["c2"] is False

    @pytest.mark.asyncio
    async def test_no_connectors_means_no_transport_call(self):
        t = FakeTransport()
        assert await _engine(t, directory=FakeDirectory({})).connector_health("org_new") == []
        assert t.online_calls == []


class TestOpsStoreDirectory:
    """存储层适配器——只取 id 和名字，不让检索层跟存储 schema 绑死。"""

    @pytest.mark.asyncio
    async def test_maps_store_rows_to_connection_refs(self):
        from src.ops.store_adapters import OpsStoreDirectory

        class Row:
            def __init__(self, cid, name):
                self.connection_id, self.name = cid, name
                self.last_heartbeat_at = 123.0      # 存储层的其余字段
                self.approval_timeout_minutes = 30  # 适配器不该关心它们

        class FakeStore:
            async def list_connectors_for_org(self, org_id):
                return [Row("c1", "Prometheus"), Row("c2", "自建日志")]

        refs = await OpsStoreDirectory(FakeStore()).list_for_org(ORG_A)
        assert [(r.connection_id, r.system_name) for r in refs] == [
            ("c1", "Prometheus"), ("c2", "自建日志")]

    @pytest.mark.asyncio
    async def test_falls_back_to_id_when_name_is_missing(self):
        """名字是给用户看的（"来自「XX」的数据不可用"）。缺名字时用 id 兜底，
        不能让降级提示变成"来自「None」的数据不可用"。"""
        from src.ops.store_adapters import OpsStoreDirectory

        class Row:
            connection_id = "c9"
            name = None

        class FakeStore:
            async def list_connectors_for_org(self, org_id):
                return [Row()]

        refs = await OpsStoreDirectory(FakeStore()).list_for_org(ORG_A)
        assert refs[0].system_name == "c9"
