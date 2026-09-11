# Third-Party Notices

InferenceDock 本身按 [MIT](LICENSE) 发布。以下项目是协议和插件接入参考或用户自行安装的运行时，不随本仓库分发模型权重或第三方二进制：

| Project | Role in this repository | Distribution boundary |
| --- | --- | --- |
| PyYAML | 运行时配置解析依赖，按 `requirements.txt` 安装 | 依赖自身 MIT 许可；不复制源码 |
| DS4 / DS4f | 用户本地的外部或 managed 服务候选 | 本仓库只读取经授权的运行时信息，不分发其代码或模型 |
| MLX-Serve | 优先验证的 OpenAI-compatible 外部服务 | 本仓库只保存脱敏配置示例，不分发引擎或权重 |
| MTPLX | managed 服务候选 | 需要用户提供固定版本、二进制和资产 |
| llama.cpp, Ollama, oMLX, LM Studio, mlx-lm, mlx-vlm, vllm-mlx | 协议/插件兼容候选 | 通过用户显式登记接入，不代表已真实测试 |

名称、链接和协议兼容性不构成 endorsement。发布者必须在打包第三方代码或二进制前，逐项核查上游许可证、NOTICE 和分发义务；公开源代码归档不包含上述运行时、模型、缓存或本机配置。

