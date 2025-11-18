# Claude/Codex API 中转代理 - 完整使用指南

> **通用API中转代理** - 支持 Claude Code、Codex CLI 和 OpenAI 格式的智能转发与格式转换

---

## 📋 目录

- [项目概述](#项目概述)
- [核心特性](#核心特性)
- [快速开始](#快速开始)
- [客户端配置示例](#客户端配置示例)
  - [Claude Code CLI 配置](#claude-code-cli-配置)
  - [Codex CLI 配置](#codex-cli-配置)
  - [Python OpenAI SDK 配置](#python-openai-sdk-配置)
- [API使用方式](#api使用方式)
  - [1. Claude Code 直连模式](#1-claude-code-直连模式)
  - [2. Codex CLI 直连模式](#2-codex-cli-直连模式)
  - [3. OpenAI 格式转换模式](#3-openai-格式转换模式)
- [Web管理后台](#web管理后台)
- [配置管理](#配置管理)
- [智能容错机制](#智能容错机制)
- [常见问题](#常见问题)

---

## 项目概述

这是一个功能完整的 **多协议API中转代理服务器**,支持:

- ✅ **Claude Code API** 中转和多KEY轮换
- ✅ **Codex CLI API** 完整代理(GPT-5-Codex)
- ✅ **OpenAI格式** 自动转换为Claude格式
- ✅ **智能容错** - 自动切换、重试、冷却管理
- ✅ **实时统计** - Token使用量统计和可视化
- ✅ **Web管理** - 图形化配置管理界面

**端口**: `5101`
**协议**: HTTP/HTTPS
**启动方式**: `python app.py`

---

## 核心特性

### 🔄 多API智能管理
- **多配置支持**: 可配置多个Claude API和Codex API
- **主备切换**: 主API不可用时自动切换到备用API
- **时间使能**: 支持按星期几启用不同的API(周一~周日)
- **优先级调度**: 按配置顺序自动选择最优API
- **定时激活**: 自动在指定时间激活API计费周期

### 🛡️ 智能容错机制
- **错误检测**: 实时检测API错误并记录
- **自动切换**: 连续错误达到阈值自动切换API
- **冷却管理**: 错误API进入冷却期,避免频繁调用
- **重试策略**: 支持策略重试、普通重试、切换重试
- **超时控制**: 精细化超时配置,避免长时间等待

### 📊 实时统计与监控
- **Token统计**: 自动统计每次请求的Token使用量
- **按模型分类**: 分别统计不同模型的使用情况
- **按日期统计**: 每日使用量统计和可视化
- **缓存统计**: 区分新输入、输出、缓存创建、缓存读取

### 🔧 灵活配置
- **Web管理界面**: 图形化配置所有参数
- **热重载**: 配置修改后无需重启即可生效
- **JSON配置**: 所有配置保存在 `json_data/all_configs.json`

---

## 快速开始

### 1. 安装依赖

```bash
pip install fastapi uvicorn httpx
```

### 2. 启动服务

```bash
cd /path/to/project
python app.py
```

启动后会看到:
```
============================================================
Claude Code API Server Startup
============================================================
API轮动配置:
  主API（按配置优先级顺序）:
    优先级#1: 周一KEY | https://api-provider-1.example.com
    ...
  备用API（全周可用）:
    备用API: https://api-provider-2.example.com/code
    ...
端口: 5101
============================================================
```

### 3. 访问管理后台

打开浏览器访问: `http://localhost:5101`

可以管理:
- API配置(Claude和Codex)
- OpenAI转Claude配置
- 超时重试策略
- 模型转换规则
- 错误处理策略
- Token使用统计

---

## 客户端配置示例

### Claude Code CLI 配置

**配置文件位置**: `~/.claude/settings.json` (Windows: `C:\Users\用户名\.claude\settings.json`)

**配置内容**:
```json
{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "123",
    "ANTHROPIC_BASE_URL": "http://localhost:5101/api",
    "CLAUDE_CODE_MAX_THINKING_TOKENS": "32000",
    "MAX_THINKING_TOKENS": "32000",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "API_TIMEOUT_MS": "600000"
  },
  "permissions": {
    "allow": [],
    "deny": []
  }
}
```

**字段说明**:
- `ANTHROPIC_AUTH_TOKEN`: 用户密钥（需要在中转服务的 app.py 中配置映射）
- `ANTHROPIC_BASE_URL`: 中转服务地址（注意路径为 `/api`）
- `CLAUDE_CODE_MAX_THINKING_TOKENS`: 思考模式最大Token数
- `API_TIMEOUT_MS`: API超时时间（毫秒）

**使用方式**:
```bash
# 配置完成后，直接使用 Claude Code CLI
claude "请帮我分析这个项目的代码结构"
```

---

### Codex CLI 配置

**配置文件位置**: `~/.codex/config.toml` (Windows: `C:\Users\用户名\.codex\config.toml`)

**配置内容**:
```toml
model_provider = "code"
model = "gpt-5.1-codex"
model_reasoning_effort = "high"
model_verbosity = "high"
disable_response_storage = true
windows_wsl_setup_acknowledged = true

[http]
# 禁用系统代理，避免与自定义HTTPS端点冲突
proxy = "no_proxy"

[model_providers.code]
name = "code"
base_url = "http://localhost:5101/openai"
wire_api = "responses"
requires_openai_auth = true  # 重要：支持模型切换

[projects.'项目路径']
trust_level = "trusted"

[notice]
hide_gpt5_1_migration_prompt = true
```

**字段说明**:
- `model`: Codex模型名称（gpt-5.1-codex 或 gpt-5-codex）
- `base_url`: 中转服务地址（注意路径为 `/openai`）
- `wire_api`: 使用 responses 协议
- `requires_openai_auth`: 必须设置为 true，支持模型切换

**使用方式**:
```bash
# 配置完成后，直接使用 Codex CLI
codex "请帮我重构这段代码"
```

---

### Python OpenAI SDK 配置

#### 方式1：使用 OpenAI SDK（推荐）

```python
#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
使用 OpenAI SDK 调用 Claude API（通过中转服务）
支持非流式和流式回复
"""

from openai import OpenAI
import sys
import io

# Windows控制台UTF-8支持
if sys.platform == 'win32':
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
    except AttributeError:
        pass

# 配置
BASE_URL = "http://localhost:5101"
API_KEY = "123"  # 用户密钥（需在中转服务配置）
MODEL = "claude-sonnet-4-5-20250929"  # 或使用 "gpt-4" 自动转换

# 创建客户端
client = OpenAI(
    api_key=API_KEY,
    base_url=f"{BASE_URL}/v1"
)

# 示例1：非流式回复
print("=" * 60)
print("测试1：非流式回复")
print("=" * 60)

response = client.chat.completions.create(
    model=MODEL,
    messages=[
        {"role": "system", "content": "你是一个编程助手"},
        {"role": "user", "content": "你好，请用中文说一句话"}
    ],
    stream=False,
    max_tokens=100
)

print(f"\n回复内容：{response.choices[0].message.content}")
print(f"Token使用：{response.usage.total_tokens}")


# 示例2：流式回复
print("\n" + "=" * 60)
print("测试2：流式回复")
print("=" * 60)

stream = client.chat.completions.create(
    model=MODEL,
    messages=[
        {"role": "user", "content": "请用中文写一首关于春天的短诗"}
    ],
    stream=True,
    max_tokens=500
)

print("\n流式响应：")
for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end='', flush=True)

print("\n\n✅ 测试完成！")
```

#### 方式2：使用 requests 库（调试用）

```python
#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
使用 requests 库直接调用（用于调试流式响应问题）
"""

import requests
import json
import sys
import io

# Windows控制台UTF-8支持
if sys.platform == 'win32':
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    except AttributeError:
        pass

# 配置
BASE_URL = "http://localhost:5101"
API_KEY = "123"
MODEL = "claude-sonnet-4-5-20250929"

url = f"{BASE_URL}/v1/chat/completions"
headers = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}

data = {
    "model": MODEL,
    "messages": [
        {"role": "user", "content": "请用中文说一句话"}
    ],
    "stream": True,
    "max_tokens": 100
}

print(f"发送流式请求到: {url}")
print(f"模型: {MODEL}\n")

try:
    response = requests.post(url, headers=headers, json=data, stream=True, timeout=30)

    print(f"响应状态码: {response.status_code}")
    print(f"开始读取流式响应:\n")
    print("-" * 60)

    # 逐行读取SSE流式响应
    for line in response.iter_lines():
        if line:
            line_str = line.decode('utf-8')

            # 解析SSE格式：data: {...}
            if line_str.startswith('data: '):
                data_str = line_str[6:]

                # 跳过[DONE]标记
                if data_str == '[DONE]':
                    continue

                try:
                    # 解析JSON并提取content
                    chunk_obj = json.loads(data_str)
                    if 'choices' in chunk_obj and len(chunk_obj['choices']) > 0:
                        delta = chunk_obj['choices'][0].get('delta', {})
                        content = delta.get('content', '')
                        if content:
                            print(content, end='', flush=True)
                except json.JSONDecodeError:
                    pass

    print("\n" + "-" * 60)
    print("\n✅ 读取完成")

except Exception as e:
    print(f"\n❌ 错误: {e}")
```

**配置说明**:
- `BASE_URL`: 中转服务地址 `http://localhost:5101`
- `API_KEY`: 用户密钥，需要在 app.py 的 `USER_KEY_MAPPING` 中配置映射
- `MODEL`: 可以使用 Claude 模型名或 GPT 模型名（自动转换）

**支持的模型名**:
- `claude-sonnet-4-5-20250929` - Claude 4.5 Sonnet（直接指定）
- `gpt-4` - 自动转换为 Claude 4.0 Sonnet
- `gpt-4-turbo` - 自动转换为 Claude 4.0 Sonnet
- `gpt-3.5-turbo` - 自动转换为 Claude 4.0 Sonnet

**思考模式**:
```python
# 启用思考模式 - 在模型名后加 -thinking
MODEL = "claude-sonnet-4-5-20250929-thinking"
# 注意：思考模式下 temperature 必须为 1
```

---


### API端点列表

- `POST /v1/messages` - Claude直连
- `POST /v1/chat/completions` - OpenAI转Claude
- `POST /openai/responses` - Codex直连
- `GET /` - Web管理首页
- `GET /token-stats.html` - Token统计页面
- `GET /api/configs` - 获取API配置
- `POST /api/reload` - 重新加载配置
- `GET /api/token-stats` - 获取Token统计
- `POST /api/token-stats/reset` - 重置统计

### 环境要求

- Python 3.8+
- FastAPI 0.100+
- httpx 0.24+
- uvicorn 0.22+

---

**文档版本**: 1.0
**最后更新**: 请参考项目最新版本
**项目路径**: `./项目目录`
