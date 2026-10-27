# Contributing to Alfred Solver

Thank you for your interest in contributing to Alfred Solver! This document outlines the guidelines for contributing to this project.

## Code of Conduct

By participating in this project, you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md).

## How to Contribute

### Reporting Bugs

Before submitting a bug report, please check if the issue has already been reported. If not, create a new issue using the bug report template.

### Suggesting Features

Feature requests are welcome! Please use the feature request template and provide as much detail as possible.

### Pull Requests

1. Fork the repository
2. Create a new branch (`git checkout -b feature/your-feature-name`)
3. Make your changes
4. Ensure all tests pass
5. Submit a pull request

### Development Setup

```bash
git clone https://github.com/gb99xbear/Alfred-Solver.git
cd Alfred-Solver
./install.sh
```

### Running Tests

```bash
./run.sh
# In another terminal:
pytest tests/ -v
```

## Code Style

- Follow PEP 8 for Python code
- Use type hints where possible
- Keep functions small and focused
- Write docstrings for public functions

## Commit Messages

Use conventional commit format:
- `feat:` for new features
- `fix:` for bug fixes
- `docs:` for documentation changes
- `refactor:` for code refactoring
- `test:` for test additions/changes
- `chore:` for maintenance tasks

## Security

If you discover a security vulnerability, please see our [Security Policy](SECURITY.md).

## License

By contributing, you agree that your contributions will be licensed under the MIT License.