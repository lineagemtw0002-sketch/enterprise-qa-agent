"""把 `ragent_backend` 的存储层接到联邦查询层的适配器。

**为什么要有这一层，而不是让引擎直接用 `OpsStore`**：
`FederatedQueryEngine` 只需要"这个 org 有哪些连接器（id + 名字）"，
而 `OpsStore.list_connectors_for_org` 返回的是完整记录（含心跳时间、创建人、
审批超时配置……）。让引擎依赖那个完整类型，等于让检索逻辑跟存储 schema 绑死——
存储层加一个字段就要动检索层的类型标注。

适配器放在**我这边**（`src/ops/`）而不是存储那边，也是刻意的：
`ops_store.py` 由另一条线维护，我不往里加只有我用得到的方法。
"""

from __future__ import annotations

from typing import Any, List, Sequence

from src.ops.federation.engine import ConnectionRef


class OpsStoreDirectory:
    """用 `OpsStore` 实现 `ConnectionDirectory`。

    注入的是 store 实例本身（不是类），所以测试里传个假件就能替换掉，
    不需要连 Postgres。
    """

    def __init__(self, ops_store: Any) -> None:
        self._store = ops_store

    async def list_for_org(self, org_id: str) -> Sequence[ConnectionRef]:
        rows = await self._store.list_connectors_for_org(org_id)
        out: List[ConnectionRef] = []
        for row in rows:
            # 只取两个字段。row 上其余字段（心跳、创建人、超时配置）跟联邦查询无关，
            # 取了反而会让人以为这里会用到它们。
            out.append(ConnectionRef(
                connection_id=row.connection_id,
                system_name=getattr(row, "name", None) or row.connection_id,
            ))
        return out
