"""极简 ACL：按 collection 粒度控制哪些 user_id 能访问哪些共享知识库。

设计原则：
- 粒度 = collection，跟现有检索/摄取的隔离粒度一致，不做文档级 ACL。
- 每个对话私有的 `conv_{conversation_id}` collection 天然只有该对话的使用者知道其 ID，
  不受这里的规则约束。
- 纯函数，不做任何 I/O——"某个 user_id 能访问哪些 collection" 这件事由调用方从
  UserStore（Postgres users 表）查出来，再传进来判断。这里只负责判断逻辑本身，
  方便测试，也不用关心调用方是从 DB 查的还是别的来源。
"""

from __future__ import annotations

from typing import List

_WILDCARD = "*"


def _is_private_conversation_collection(collection: str) -> bool:
    """每个对话自己的 collection（conv_xxx）天然私有，不需要走 ACL。"""
    return collection.startswith("conv_")


def is_collection_allowed(collection: str, allowed_collections: List[str]) -> bool:
    """判断 allowed_collections 是否覆盖某个共享 collection。"""
    if _is_private_conversation_collection(collection):
        return True
    return _WILDCARD in allowed_collections or collection in allowed_collections


def filter_allowed_collections(
    collection_names: List[str], allowed_collections: List[str]
) -> List[str]:
    """过滤 list_collections 之类的返回结果，隐藏无权限看到的共享知识库。"""
    if _WILDCARD in allowed_collections:
        return collection_names
    return [
        name
        for name in collection_names
        if _is_private_conversation_collection(name) or name in allowed_collections
    ]
