"""智能运维模块（AIOps）——设计见 `docs/aiops_module_design.md`。

包内分工（多会话并行开发，边界是刻意划的，别跨界改）：

- `types.py`         —— 联邦查询层与连接器之间的**共享契约**：请求/结果/错误结构，
                        以及 `ConnectorTransport` 协议。两边都 import 这里，谁也不 import 对方。
- `federation/`      —— 联邦查询层（§3.5）：抽象查询 → 多连接器 fan-out → 合并，
                        部分失败显式呈现，短 TTL 内存缓存。
- `connector_*`      —— BYOC 连接器会话（§3.2/§10.1）：WebSocket 注册握手、心跳、
                        token 轮换。由另一个会话实现，它实现 `ConnectorTransport`。
"""
