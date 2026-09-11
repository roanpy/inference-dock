# InferenceDock

[English README](README.md)

真实模型测试数据和未覆盖边界见[运行验收记录](docs/runtime-validation-2026-09-10.md)。

InferenceDock 是面向 macOS 本地 Agent 的轻量模型服务调度器。它给 Pi、Hermes、OpenCode 等客户端一个稳定的 `127.0.0.1:18800/v1` 入口，同时让 MLX-Serve、DS4、MTPLX 和其他推理服务器继续由用户自己安装、升级和拥有。

> 当前是 **Developer Preview**。仓库中的 fake backend 验证调度、SSE、取消、配置和发布安全；它不证明你的真实引擎、长上下文、视觉能力或内存安全。真实测试必须按[验收清单](docs/acceptance-checklist.md)独立记录。

## 为什么优先从 MLX 开始

InferenceDock 不重新实现 MLX 推理，也不偷偷接管 MLX 服务。你只需要登记 loopback 端点和后端模型 ID，调度器就能转发请求、记录可观测指标，并在无法证明安全切换时返回明确的 409。

```yaml
adapters:
  mlx-serve:
    type: external
    endpoint: http://127.0.0.1:11234
    health_path: /health
    models_path: /v1/models

models:
  qwen38-27b:
    adapter: mlx-serve
    backend_model: qwen38-27b
    context_window: 262144
    capabilities: {chat: true, stream: true, vision: false}
```

`external` 表示只观察、健康检查和转发，InferenceDock 不停止它，也不删除模型。MLX 的原生加载/卸载路由只有在固定版本、真实探针和回退步骤都验证后才加入配置；声明存在不等于能力已验证。

## 重要语义

- 同模型并发直接交给第三方后端；InferenceDock 不在入口再造请求队列。
- 冷启动并发只合并一次加载；跨模型切换遇到在途请求返回结构化 409，不强制杀服务。
- 菜单按服务器分组，每个 canonical backend model 只显示一行，并把未加载、加载中、已加载、生成中、失败和外部管理分开。
- 取消请求、卸载模型和停止服务是三个不同动作；外部服务没有可验证停止接口时按钮保持禁用。
- `/v1/metrics?limit=N` 的记录保留冷加载、排队、TTFT、prefill、decode、缓存和结束原因。最大/平均/中位速度只统计明确成功且速度为正的样本，其余显示为排除；缺失数据显示 `unknown`。

## 快速开始

需要 Python 3.11+、PyYAML；菜单栏应用需要 macOS 13+ 和 Swift 5.9+。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/model_dispatch.py --config config/engines.example.yaml --check-config
.venv/bin/python scripts/model_dispatch.py --config config/engines.example.yaml
```

示例只使用 fake backend，不下载权重，也不会启动真实模型。菜单应用是未签名的本地开发包：

```sh
swift build
sh scripts/acceptance.sh
```

## MLX 配置和迁移

从现有 MLX 服务迁移时，先备份 Agent provider，保留原端口，使用一个 canary 客户端验证健康、流式输出、取消、工具和回退，再迁移其他 Agent。配置值与后端实际生效值必须分别记录；模型发现只产生候选，不自动写入或删除权重。

- [配置指南](docs/configuration.md)
- [兼容矩阵](docs/compatibility-matrix.md)
- [故障与回退](docs/troubleshooting.md)
- [夜间真实引擎规程](docs/nightly-runbook.md)
- [逐项验收清单](docs/acceptance-checklist.md)

## 开源与安全边界

MIT 许可只覆盖本仓库。引擎、模型和缓存各自遵循上游许可，InferenceDock 不分发它们。公开包只允许使用仓库中的脱敏示例配置；本机 `Application Support`、凭据、提示、模型、缓存和日志不会被默认打包。发布前运行：

```sh
python3 scripts/export_source.py dist/inference-dock-source.tar.gz
```

参见 [SECURITY.md](SECURITY.md)、[CONTRIBUTING.md](CONTRIBUTING.md) 和 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 当前缺口

真实 MLX-Serve、DS4 和 MTPLX 的版本化 lifecycle、视觉、长上下文、缓存恢复和性能 parity 尚未由本仓库 smoke suite 证明。请把每项标记为 `真实通过`、`模拟通过` 或 `环境跳过`，不要把短请求成功写成完整支持。
