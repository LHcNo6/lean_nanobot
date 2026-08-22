# Step 104 Proposal: dream_run_completed + build_dream_commit_message

## 1. 问题背景

Dream 运行后无法判断是否正常完成（可能因 max_iterations 耗尽或异常中断），且 commit message 由 LLM 自报告生成，不可靠。nanobot 通过 `metadata._stop_reason` 判断完成状态，commit message 基于真实 git diff 构建。

## 2. 目标

1. 新增 `dream_run_completed(resp)` 静态方法，通过 `resp.metadata["_stop_reason"] == "completed"` 判断 Dream run 是否正常完成
2. 新增 `build_dream_commit_message(prefix, diff_body)` 静态方法，基于真实 diff 构建 commit message
3. main.run_dream 中用 `dream_run_completed` 判断是否推进 dream cursor

## 3. 非目标

- 不实现自动 commit（step105-106 GitStore 完成后再集成）
- 不修改 Dream prompt 或工具集

## 4. 验收标准

1. `dream_run_completed(None)` 返回 False
2. `dream_run_completed` 无 metadata / metadata 非 dict / 无 _stop_reason / _stop_reason != "completed" 均返回 False
3. `dream_run_completed` metadata._stop_reason == "completed" 返回 True
4. `build_dream_commit_message` 空 diff 返回纯 prefix
5. `build_dream_commit_message` 有 diff 返回 `prefix\n\ndiff_body`（strip 后）
6. main.run_dream 未完成时不推进 cursor
7. 单元测试通过
