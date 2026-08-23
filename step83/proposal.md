# Step 83 Proposal: CliAppsTool（CLI应用白名单工具）

## 1. 问题背景

nanobot 有 CliAppsTool，用于执行用户在 Settings 中安装的 CLI 应用，
通过受控的 argv 子进程运行，区别于普通的 ExecTool（shell 执行）。
learn_nano 目前缺少这个工具。

## 2. 目标

新建 `tools/cli_apps.py`，实现简化版 CliAppsTool：
1. CliApp 数据类：名称、入口命令、描述
2. CliAppManager：管理已注册的 CLI 应用（内存存储 + 注册/查询/执行）
3. CliAppsTool：工具类，执行已注册的 CLI 应用
4. 使用 argv 子进程（非 shell），更安全
5. 未知应用名拒绝执行

## 3. 非目标

- 不实现应用安装/卸载（只支持预注册）
- 不实现 catalog 缓存
- 不实现 runtime context provider
- 不实现 --json 输出模式（简化）

## 4. 验收标准

1. CliAppManager 可以注册/查询/列出应用
2. CliAppsTool 可以执行已注册应用
3. 未知应用名返回错误
4. 使用 argv 子进程（create_subprocess_exec）
5. 单元测试通过
