# Contributing to Claude/Codex API Smart Switch

First off, thank you for considering contributing to this project! 🎉

**English** | [简体中文](./CONTRIBUTING_CN.md)

---

## 🤝 How to Contribute

### Reporting Bugs

If you find a bug, please create an issue with the following information:

- **Description**: Clear description of the bug
- **Steps to Reproduce**: Detailed steps to reproduce the issue
- **Expected Behavior**: What you expected to happen
- **Actual Behavior**: What actually happened
- **Environment**: Python version, OS, relevant configuration
- **Logs**: Relevant log excerpts (if applicable)

### Suggesting Enhancements

Enhancement suggestions are welcome! Please create an issue with:

- **Feature Description**: Clear description of the proposed feature
- **Use Case**: Why this feature would be useful
- **Implementation Ideas**: (Optional) How you think it could be implemented

### Pull Requests

1. **Fork the Repository**
   ```bash
   git clone git@github.com:cd555yong/codex_cc_switch.git
   cd codex_cc_switch
   ```

2. **Create a Branch**
   ```bash
   git checkout -b feature/your-feature-name
   ```

3. **Make Your Changes**
   - Follow the existing code style
   - Add comments for complex logic
   - Update documentation if needed

4. **Test Your Changes**
   ```bash
   python app.py
   # Test your changes manually or add automated tests
   ```

5. **Commit Your Changes**
   ```bash
   git add .
   git commit -m "Add: your feature description"
   ```

6. **Push to Your Fork**
   ```bash
   git push origin feature/your-feature-name
   ```

7. **Create a Pull Request**
   - Go to the original repository
   - Click "New Pull Request"
   - Select your branch
   - Provide a clear description of your changes

## 📝 Code Style Guidelines

### Python Code Style

- Follow [PEP 8](https://pep8.org/) style guide
- Use meaningful variable and function names
- Add docstrings for functions and classes
- Keep functions focused on a single task
- Use type hints where appropriate

### Commit Message Guidelines

Format: `<type>: <description>`

Types:
- `Add`: New feature
- `Fix`: Bug fix
- `Update`: Update existing feature
- `Refactor`: Code refactoring
- `Docs`: Documentation changes
- `Style`: Code style changes (formatting, etc.)
- `Test`: Adding or updating tests
- `Chore`: Maintenance tasks

Examples:
```
Add: support for GPT-4 Turbo model
Fix: API switching logic when all APIs are in cooldown
Update: improve error detection for compressed responses
Docs: add Docker deployment guide
```

## 🧪 Testing

### Manual Testing

1. Start the server: `python app.py`
2. Test different endpoints:
   - Claude direct mode: `/v1/messages`
   - Codex mode: `/openai/responses`
   - OpenAI conversion: `/v1/chat/completions`
3. Test error handling (invalid API key, network errors, etc.)
4. Test failover mechanisms (trigger errors to test API switching)

### Automated Testing (Future)

We welcome contributions to add automated tests!

## 📚 Documentation

When adding new features, please update:

- `README.md` (English)
- `使用说明.md` (Chinese)
- Code comments and docstrings

## 🎯 Areas for Contribution

Here are some areas where contributions are especially welcome:

1. **Testing**: Add automated tests (unit tests, integration tests)
2. **Documentation**: Improve documentation, add examples
3. **Error Handling**: Enhance error detection and recovery
4. **Performance**: Optimize performance and resource usage
5. **Features**: Add new API providers, model support
6. **UI/UX**: Improve web dashboard interface
7. **Monitoring**: Add metrics and monitoring capabilities
8. **Docker**: Improve Docker configuration and deployment

## 🐛 Known Issues

Check the [Issues](https://github.com/cd555yong/codex_cc_switch/issues) page for known issues and planned features.

## 📞 Contact

If you have questions or need help:

- Create an issue on GitHub
- Check existing issues and discussions

## 📄 License

By contributing, you agree that your contributions will be licensed under the MIT License.

---

Thank you for contributing! 🚀
