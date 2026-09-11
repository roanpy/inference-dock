<div align="center">

# InferenceDock

**一个本地入口，统一适配和调度 macOS 上的开源模型运行框架。**

不修改引擎源码，统一协调端口、进程、内存交接和 Agent 配置。

**[English](README.md) · 简体中文**

[![状态：开发者预览](https://img.shields.io/badge/状态-开发者预览-2563eb)](docs/release-notes.md)
[![CI](https://github.com/roanpy/inference-dock/actions/workflows/ci.yml/badge.svg)](https://github.com/roanpy/inference-dock/actions/workflows/ci.yml)
![macOS 13+](https://img.shields.io/badge/macOS-13%2B-111827?logo=apple)
![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776ab?logo=python&logoColor=white)
[![许可证：MIT](https://img.shields.io/badge/许可证-MIT-16a34a.svg)](LICENSE)

</div>

## 它解决什么问题

真实模型测试数据和未覆盖边界见[运行验收记录](docs/runtime-validation-2026-09-10.md)。

InferenceDock 是面向 macOS 本地 Agent 的轻量模型服务调度器。它给 Pi、Hermes、OpenCode 等客户端一个稳定的 `127.0.0.1:18800/v1` 入口，同时让 MLX-Serve、DS4、MTPLX 和其他推理服务器继续由用户自己安装、升级和拥有。

它针对的是多端口、多启动脚本、大模型驻留冲突以及不同 Agent 配置逐渐偏离的问题；推理、批处理、KV 缓存和并发仍由各自服务器负责。

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

退出菜单栏只退出界面，不会停止调度核心或在途请求；需要停止本应用启动的核心时，使用单独的“停止 InferenceDock 核心”操作。该操作不会触碰外部服务。

原生“设置”窗口让菜单专注于运行操作，提供服务器与配置路径检查、实例和模型能力只读预览、互斥/常驻/内存策略、Agent 地址预览以及更新诊断。服务器页可对用户明确选择的本机端点、可执行文件、源码目录或受支持配置文件调用内置插件导入器生成预览；该操作只读，不扫描目录，也不会写入 `engines.yaml`。模型页的“清理失效配置”只移除已确认本地缺失且未加载的模型映射和不再被引用的适配器：提交时带配置版本号，核心会拒绝过期版本、仍在驻留的模型和被引用的适配器，并在写入前保留权限受限的备份；模型文件、第三方配置和 provider 文件不会被修改，发布回滚仍按维护者流程执行。

## 重要语义

- 同模型并发直接交给第三方后端；InferenceDock 不在入口再造请求队列。
- 冷启动并发只合并一次加载；跨模型切换遇到在途请求返回结构化 409，不强制杀服务。
- 菜单按服务器分组，每个 canonical backend model 只显示一行，并把未加载、加载中、已加载、生成中、失败和外部管理分开。
- 取消请求、卸载模型和停止服务是三个不同动作；外部服务没有可验证停止接口时按钮保持禁用。
- `/v1/metrics?limit=N` 的记录保留冷加载、排队、TTFT、prefill、decode、缓存和结束原因。最大/平均/中位速度只统计明确成功且速度为正的样本，其余显示为排除；缺失数据显示 `unknown`。
- 刷新和导入结果会分别标识平台、安装和实例；同一实例的相同后端模型只显示一行，其余保留为兼容别名。
- 成功加载后最多记录一次监听进程内存作为实测证据，不会自动覆盖配置中的内存估值。
- Chat Completions 与 Responses API 共用同一调度路径；后端不支持的接口会保留原始错误，不由 InferenceDock 模拟能力。

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

本地模型可通过原生 CLI 目录、在线服务的模型列表或配置中明确列出的文件进行只读确认。确定缺失的模型会自动退出 `/v1/models`、运行菜单和 Agent 预览，但设置中的配置与诊断记录仍会保留；服务暂时离线或响应异常只标记为“未知”，不会误删或误隐藏配置。需要真正移除这些记录时，在“模型”页使用“清理失效配置”；策略文件里残留的已删除模型或适配器会被忽略并在“诊断”页显示警告，不再导致整份策略（智能调度、空闲卸载）被重置。

Agent 配置修改使用版本感知的 `scripts/agent_config.py` 预览、校验、应用和恢复流程，并保留同目录备份；菜单栏只做校验和预览，不会静默改写 provider 文件。

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
