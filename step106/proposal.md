# Step 100 Proposal: token 估算对齐 + unified_session + WeakValueDictionary

## 1. 问题背景

Consolidator 当前用 `sum(estimate_message_tokens(m))` 逐个估算消息 token，未考虑系统提示、工具定义等开销，估算不准确。_locks 使用普通 dict 会无限增长。缺少 unified_session 参数。

## 2. 目标

1. `__init__` 新增 `unified_session: bool = False` 参数
2. `_locks` 改用 `weakref.WeakValueDictionary[str, asyncio.Lock]`
3. 新增 `estimate_session_prompt_tokens(session, runtime)` 方法，通过 `_build_messages` 构建完整 probe 后调用 `estimate_prompt_tokens_chain`
4. `maybe_consolidate_by_tokens` 改用 `estimate_session_prompt_tokens`，返回 `(estimated, source)` 元组

## 3. 非目标

- 不修改 _build_messages 签名（后续 step）
- 不修改 archive 方法（step99 已完成）

## 4. 验收标准

1. unified_session 参数可传入并存储
2. _locks 为 WeakValueDictionary 类型
3. estimate_session_prompt_tokens 返回 (int, str) 元组
4. maybe_consolidate_by_tokens 使用新方法估算
5. 现有测试全部通过
