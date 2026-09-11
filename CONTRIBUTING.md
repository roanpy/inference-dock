# Contributing

InferenceDock 仍处于 Developer Preview。先读 [执行计划](docs/execution-plan-2026-09-10.md)、[验收清单](docs/acceptance-checklist.md) 和 [第三方说明](THIRD_PARTY_NOTICES.md)，再提交小而可复核的改动。

## 开发约定

- Python 代码使用标准库和现有依赖；不引入只解决一处问题的新框架。
- 新适配器优先使用声明式 manifest，并明确 `discover`、`configure`、`manage`、`tested` 四个证据等级。
- 不执行第三方仓库脚本，不自动 checkout、merge、下载权重或修改用户配置。
- 同模型并发交给后端；不要在 dispatcher 里新增隐式串行队列。
- 任何状态机、SSE、取消、进程所有权和内存准入改动都要补可重复测试，且不启动真实模型。
- 公开示例不得包含个人路径、提示、令牌、日志或模型工件。

## 本地检查

```sh
python3 tests/run_tests.py
python3 tests/test_plugin_registry.py
python3 tests/test_plugin_runtime.py
python3 tests/test_migration_helper.py
python3 tests/test_update_monitor.py
swift build
sh scripts/acceptance.sh
```

真实引擎测试必须使用独立端口、临时配置和明确的资源预算，按 nightly runbook 保存证据；不要把 smoke 结果写成 MLX、DS4 或 MTPLX 的完整支持。

## Pull request 内容

说明行为变化、配置兼容性、测试命令和剩余缺口。若涉及第三方代码，注明上游版本、许可证和是否只是协议参考。维护者会在发布前重新运行公开导出扫描；任何凭据或私有配置泄露都将直接拒绝。

