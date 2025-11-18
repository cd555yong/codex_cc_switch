# 参与贡献 - Claude/Codex API 智能切换代理

首先，感谢您考虑为本项目做出贡献！🎉

[English](./CONTRIBUTING.md) | **简体中文**

---

## 🤝 如何贡献

### 报告Bug

如果您发现了bug，请创建一个issue并包含以下信息：

- **描述**：清晰描述bug现象
- **复现步骤**：详细的复现步骤
- **预期行为**：您期望发生什么
- **实际行为**：实际发生了什么
- **环境信息**：Python版本、操作系统、相关配置
- **日志**：相关的日志摘录（如适用）

### 功能建议

欢迎提出功能增强建议！请创建issue并包含：

- **功能描述**：清晰描述建议的功能
- **使用场景**：说明这个功能为什么有用
- **实现想法**：（可选）您认为如何实现

### 提交Pull Request

1. **Fork仓库**
   ```bash
   git clone git@github.com:cd555yong/codex_cc_switch.git
   cd codex_cc_switch
   ```

2. **创建分支**
   ```bash
   git checkout -b feature/你的功能名称
   ```

3. **进行修改**
   - 遵循现有代码风格
   - 为复杂逻辑添加注释
   - 如需要，更新文档

4. **测试修改**
   ```bash
   python app.py
   # 手动测试您的修改或添加自动化测试
   ```

5. **提交修改**
   ```bash
   git add .
   git commit -m "Add: 您的功能描述"
   ```

6. **推送到您的Fork**
   ```bash
   git push origin feature/你的功能名称
   ```

7. **创建Pull Request**
   - 访问原始仓库
   - 点击"New Pull Request"
   - 选择您的分支
   - 提供清晰的修改描述

---

## 📝 代码风格指南

### Python代码风格

- 遵循 [PEP 8](https://pep8.org/) 风格指南
- 使用有意义的变量和函数名
- 为函数和类添加文档字符串
- 保持函数专注于单一任务
- 适当使用类型提示

### 提交信息规范

格式：`<类型>: <描述>`

类型：
- `Add`：新功能
- `Fix`：Bug修复
- `Update`：更新现有功能
- `Refactor`：代码重构
- `Docs`：文档更改
- `Style`：代码格式调整
- `Test`：添加或更新测试
- `Chore`：维护任务

示例：
```
Add: 支持GPT-4 Turbo模型
Fix: 修复所有API处于冷却期时的切换逻辑
Update: 改进压缩响应的错误检测
Docs: 添加Docker部署指南
```

---

## 🧪 测试

### 手动测试

1. 启动服务器：`python app.py`
2. 测试不同端点：
   - Claude直连模式：`/v1/messages`
   - Codex模式：`/openai/responses`
   - OpenAI转换：`/v1/chat/completions`
3. 测试错误处理（无效API密钥、网络错误等）
4. 测试故障转移机制（触发错误以测试API切换）

### 自动化测试（期待）

欢迎贡献添加自动化测试！

---

## 📚 文档

添加新功能时，请更新：

- `README.md`（英文）
- `README_CN.md`（中文）
- `使用说明.md`（中文详细文档）
- 代码注释和文档字符串

---

## 🎯 欢迎贡献的领域

以下领域特别欢迎贡献：

1. **测试**：添加自动化测试（单元测试、集成测试）
2. **文档**：改进文档、添加示例
3. **错误处理**：增强错误检测和恢复机制
4. **性能**：优化性能和资源使用
5. **功能**：添加新的API提供商、模型支持
6. **界面**：改进Web管理界面
7. **监控**：添加指标和监控能力
8. **Docker**：改进Docker配置和部署

---

## 🐛 已知问题

查看 [Issues](https://github.com/cd555yong/codex_cc_switch/issues) 页面了解已知问题和计划功能。

---

## 📞 联系方式

如果您有疑问或需要帮助：

- 在GitHub上创建issue
- 查看现有issues和讨论

---

## 📄 许可证

通过贡献，您同意您的贡献将按照MIT许可证授权。

---

## 🔧 开发环境设置

### 推荐的开发工具

- **IDE**：VS Code、PyCharm
- **Python版本管理**：pyenv
- **虚拟环境**：venv 或 conda
- **代码格式化**：black、autopep8
- **代码检查**：pylint、flake8

### 本地开发流程

1. **克隆并设置环境**
   ```bash
   git clone git@github.com:cd555yong/codex_cc_switch.git
   cd codex_cc_switch
   python -m venv venv
   source venv/bin/activate  # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **配置开发环境**
   ```bash
   # 复制配置模板
   cp json_data/all_configs.json json_data/all_configs.json.dev
   # 编辑开发配置（使用测试API密钥）
   ```

3. **启动开发服务器**
   ```bash
   python app.py
   ```

4. **进行修改和测试**
   - 修改代码
   - 测试功能
   - 检查日志（`logs/` 目录）

---

## 🌟 贡献者行为准则

### 我们的承诺

为了营造开放和友好的环境，我们承诺：

- 使用包容的语言
- 尊重不同的观点和经验
- 优雅地接受建设性批评
- 关注对社区最有利的事情
- 对其他社区成员表示同理心

### 不可接受的行为

- 使用性化的语言或图像
- 侮辱/贬损性评论和人身攻击
- 公开或私下骚扰
- 未经许可发布他人的私人信息
- 其他在专业环境中被认为不适当的行为

---

## 📈 贡献流程图

```
发现问题/想法
    ↓
创建Issue讨论
    ↓
Fork仓库
    ↓
创建功能分支
    ↓
进行修改
    ↓
本地测试
    ↓
提交修改
    ↓
推送到Fork
    ↓
创建Pull Request
    ↓
代码审查
    ↓
合并到主分支
```

---

## 🎁 贡献奖励

虽然这是一个开源项目，但我们重视每一位贡献者：

- ✨ 您的名字将出现在贡献者列表中
- 🏆 重要贡献将在Release Notes中特别提及
- 📢 优秀贡献可能会在项目主页展示
- 🤝 加入项目维护团队的机会

---

感谢您的贡献！🚀

**项目维护者**：[@cd555yong](https://github.com/cd555yong)
