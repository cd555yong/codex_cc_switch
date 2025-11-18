# Claude/Codex API 智能切换代理

> **多协议 AI API 网关** - 支持 Claude Code、Codex CLI 和 OpenAI 格式的智能转发与自动容错

[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green.svg)](https://fastapi.tiangolo.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

## 🌟 核心特性

### 🔄 智能多API管理
- **多密钥轮换**: 配置多个 Claude 和 Codex API 密钥
- **自动故障转移**: 错误达到阈值(3次)自动切换备用 API
- **优先级调度**: 按配置顺序自动选择最优 API
- **时间调度**: 支持按星期启用不同的 API
- **定时激活**: 自动激活 API 计费周期

### 🛡️ 高级容错机制
- **实时错误检测**: 监控 API 响应状态和质量
- **智能切换**: 主API失败自动切换备用API
- **冷却管理**: 失败 API 进入10分钟冷却期
- **多重试策略**: 策略重试、普通重试、API切换
- **超时控制**: 精细化超时配置

### 📊 实时监控统计
- **Token 统计**: 按模型和日期统计使用量
- **缓存分析**: 区分输入、输出、缓存创建、缓存读取
- **Web 仪表板**: 可视化图表和实时监控
- **历史追踪**: 完整的请求和响应日志

### 🔧 灵活配置
- **Web 管理界面**: 浏览器图形化配置
- **热重载**: 配置修改无需重启
- **JSON 持久化**: 所有配置保存在 `json_data/all_configs.json`

---

## 🚀 快速开始

### 安装

```bash
# 1. 克隆项目
git clone git@github.com:cd555yong/codex_cc_switch.git
cd codex_cc_switch

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置 API 密钥（通过 Web 界面或编辑配置文件）

# 4. 启动服务
python app.py
```

服务将在端口 **5101** 启动。

### 访问管理界面

打开浏览器访问: `http://localhost:5101`

---

## 📖 使用示例

### 1. Claude Code 直连模式

**端点**: `POST /v1/messages`

**示例** (Python):
```python
import httpx

url = "http://localhost:5101/v1/messages"
headers = {
    "authorization": "Bearer YOUR_KEY",
    "content-type": "application/json",
    "anthropic-version": "2023-06-01"
}

data = {
    "model": "claude-sonnet-4-5-20250929",
    "max_tokens": 8192,
    "messages": [
        {
            "role": "user",
            "content": [{"type": "text", "text": "你好！"}]
        }
    ],
    "stream": True
}

with httpx.Client() as client:
    with client.stream("POST", url, json=data, headers=headers) as response:
        for line in response.iter_lines():
            print(line)
```

### 2. Codex CLI 直连模式

**端点**: `POST /openai/responses`

**示例** (Python):
```python
import httpx

url = "http://localhost:5101/openai/responses"
data = {
    "model": "gpt-5-codex",
    "input": [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "分析这段代码"}]
        }
    ],
    "stream": True
}

headers = {
    "authorization": "Bearer YOUR_KEY",
    "content-type": "application/json"
}

with httpx.Client() as client:
    with client.stream("POST", url, json=data, headers=headers) as response:
        for line in response.iter_lines():
            print(line)
```

### 3. OpenAI 格式转换模式

**端点**: `POST /v1/chat/completions`

**示例** (Python):
```python
from openai import OpenAI

client = OpenAI(
    api_key="YOUR_KEY",
    base_url="http://localhost:5101/v1"
)

response = client.chat.completions.create(
    model="gpt-4",  # 自动转换为 Claude 模型
    messages=[
        {"role": "system", "content": "你是一个编程助手"},
        {"role": "user", "content": "你好！"}
    ],
    stream=True
)

for chunk in response:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end='', flush=True)
```

---

## 🎯 主要功能

### Web 管理后台

访问 `http://localhost:5101` 可以管理：

- **API 配置**: 添加/编辑/删除 Claude 和 Codex API 密钥
- **OpenAI 转换配置**: 专用的 OpenAI 格式转换配置
- **重试策略**: 配置多个重试策略和超时时间
- **模型转换**: 自动转换模型名称（如 gpt-4 → claude-sonnet-4）
- **错误处理**: 配置不同 HTTP 状态码的处理策略
- **超时设置**: 连接、读取、写入超时配置
- **Token 统计**: 实时查看 Token 使用量和图表

### 配置文件

所有配置保存在 `json_data/all_configs.json`：

```json
{
  "api_configs": [...],
  "codex_configs": [...],
  "openai_to_claude_configs": [...],
  "retry_configs": [...],
  "model_conversions": [...],
  "timeout_settings": {...},
  "error_handling_strategies": {...}
}
```

---

## 🔧 技术架构

### 技术栈

- **框架**: FastAPI (异步 Web 框架)
- **HTTP 客户端**: httpx (异步 HTTP)
- **配置管理**: 基于 JSON 文件
- **日志**: Python logging 模块
- **统计**: 自定义 Token 追踪模块

### 核心模块

1. **app.py** - FastAPI 应用、API 路由、反向代理、故障转移逻辑
2. **config_manager.py** - 统一配置管理、JSON 持久化
3. **openai_adapter.py** - OpenAI→Claude 格式转换、思考模式支持
4. **openai_to_codex.py** - OpenAI→Codex 格式转换、完整 Codex 协议
5. **token_stats.py** - Token 使用追踪、实时聚合

### 数据流

```
客户端请求
  ↓
路径识别 (/v1/messages | /v1/chat/completions | /openai/responses)
  ↓
格式转换 (OpenAI→Claude | OpenAI→Codex | 直接透传)
  ↓
API选择 (主API → 备用API → 重试策略)
  ↓
请求转发 (流式/非流式)
  ↓
错误处理 (检测 → 记录 → 切换/重试)
  ↓
响应转换 (Claude→OpenAI | Codex→OpenAI | 直接透传)
  ↓
Token统计 (提取usage → 记录 → 聚合)
  ↓
返回客户端
```

---

## 📝 文档

完整的中文使用文档请参考 [使用说明.md](./使用说明.md)。

**文档涵盖内容**：
- 客户端配置（Claude Code CLI、Codex CLI、Python SDK）
- 高级 API 管理
- 智能故障转移机制
- Token 统计和监控
- 故障排查 FAQ
- 维护和运维指南

---

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

查看 [贡献指南](CONTRIBUTING.md) 了解详细信息。

---

## 📄 许可证

本项目采用 [MIT 许可证](LICENSE)。

---

## 🙏 致谢

- [Anthropic](https://www.anthropic.com/) - Claude API
- [OpenAI](https://openai.com/) - Codex CLI
- [FastAPI](https://fastapi.tiangolo.com/) - 现代 Web 框架
- [httpx](https://www.python-httpx.org/) - HTTP 客户端库

---

**版本**: 1.0
**端口**: 5101
**仓库**: https://github.com/cd555yong/codex_cc_switch

🚀 使用 [Claude Code](https://claude.com/claude-code) 生成
