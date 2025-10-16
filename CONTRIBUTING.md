# Contributing to AJIB

Thank you for your interest in contributing to AJIB! This guide will help you get started.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
- [Project Structure](#project-structure)
- [Making Changes](#making-changes)
- [Testing](#testing)
- [Submitting Changes](#submitting-changes)
- [Code Style](#code-style)
- [Documentation](#documentation)

## Code of Conduct

This project follows a simple code of conduct:
- Be respectful and inclusive
- Focus on constructive feedback
- Help others learn and grow
- Keep discussions professional and on-topic

## Getting Started

1. Fork the repository on GitHub
2. Clone your fork locally:
   ```bash
   git clone https://github.com/YOUR_USERNAME/ajib.git
   cd ajib
   ```

3. Add the upstream repository:
   ```bash
   git remote add upstream https://github.com/SeyedHashtag/ajib.git
   ```

4. Create a branch for your changes:
   ```bash
   git checkout -b feature/your-feature-name
   ```

## Development Setup

### Local Development (without installation)

1. Create a virtual environment:
   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   pip install -r requirements-dev.txt  # If it exists
   ```

3. Create a `.env` file (copy from `.env.example`):
   ```bash
   cp .env.example .env
   # Edit .env and add your credentials
   ```

4. Run the bot:
   ```bash
   python -m ajib.bot.core.bot
   ```

### Testing Installation Scripts

To test the installation scripts in a clean Ubuntu environment, use a VM or Docker:

```bash
# Using Docker
docker run -it --rm ubuntu:22.04 bash

# Inside the container
apt update && apt install -y curl sudo git
curl -fsSL https://raw.githubusercontent.com/YOUR_USERNAME/ajib/YOUR_BRANCH/deploy/install.sh | bash
```

## Project Structure

```
ajib/
├── ajib/                      # Main Python package
│   ├── __init__.py           # Package metadata
│   ├── bot/                  # Telegram bot code
│   │   ├── core/             # Core bot functionality
│   │   │   ├── bot.py        # Bot runner and dispatcher
│   │   │   ├── config.py     # Configuration management
│   │   │   ├── database.py   # Database layer
│   │   │   └── i18n.py       # Internationalization
│   │   ├── handlers/         # Message handlers
│   │   │   ├── admin.py      # Admin commands
│   │   │   └── customer.py   # Customer commands
│   │   ├── keyboards/        # Keyboard layouts
│   │   ├── locales/          # Translation files
│   │   │   ├── en.yml        # English translations
│   │   │   └── fa.yml        # Persian translations
│   │   └── services/         # External API clients
│   │       ├── blitz_client.py    # Blitz API
│   │       └── heleket_client.py  # Heleket payments
│   ├── cli/                  # CLI tool
│   │   └── ajib              # Main CLI script
│   └── deploy/               # Deployment scripts
│       ├── install.sh        # Installation script
│       ├── update.sh         # Update script
│       ├── uninstall.sh      # Uninstall script
│       └── ajib-bot.service  # Systemd service file
├── .env.example              # Example environment file
├── .gitignore                # Git ignore patterns
├── LICENSE                   # MIT License
├── README.md                 # Project documentation
├── SETUP.md                  # Setup guide
├── CONTRIBUTING.md           # This file
├── pyproject.toml            # Python package configuration
└── requirements.txt          # Python dependencies
```

## Making Changes

### Types of Contributions

1. **Bug Fixes**: Fix issues in existing code
2. **Features**: Add new functionality
3. **Documentation**: Improve or add documentation
4. **Refactoring**: Improve code quality without changing functionality
5. **Translations**: Add or improve translations
6. **Tests**: Add or improve test coverage

### Development Workflow

1. **Create a branch** for your changes:
   ```bash
   git checkout -b type/short-description
   # Examples:
   # feature/add-webhook-receiver
   # fix/database-connection-leak
   # docs/improve-setup-guide
   ```

2. **Make your changes** following the code style guidelines

3. **Test your changes** thoroughly

4. **Commit your changes** with clear messages:
   ```bash
   git add .
   git commit -m "feat: add webhook receiver for Heleket payments"
   ```

   Use conventional commit messages:
   - `feat:` for new features
   - `fix:` for bug fixes
   - `docs:` for documentation changes
   - `refactor:` for refactoring
   - `test:` for adding tests
   - `chore:` for maintenance tasks

5. **Push to your fork**:
   ```bash
   git push origin your-branch-name
   ```

6. **Create a Pull Request** on GitHub

## Testing

### Manual Testing

Before submitting changes, test:

1. **Bot functionality**: Run the bot and test affected features
2. **Installation**: Test installation on a fresh Ubuntu VM/container
3. **CLI commands**: Test all relevant CLI commands
4. **Configuration changes**: Ensure config changes don't break existing setups

### Automated Testing (Future)

We plan to add:
- Unit tests with pytest
- Integration tests
- CI/CD with GitHub Actions

## Submitting Changes

### Pull Request Guidelines

1. **Title**: Clear and descriptive (e.g., "Add webhook receiver for Heleket payments")

2. **Description**: Include:
   - What changes were made
   - Why the changes were necessary
   - How to test the changes
   - Related issues (if any)

3. **Checklist**:
   - [ ] Code follows project style guidelines
   - [ ] Comments added for complex logic
   - [ ] Documentation updated (if needed)
   - [ ] Tested on Ubuntu 22.04
   - [ ] No breaking changes (or documented if unavoidable)
   - [ ] Locale files updated (if adding user-facing text)

### Example PR Description

```markdown
## Description
This PR adds a webhook receiver endpoint for Heleket payment notifications.

## Changes
- Added webhook route handler in `bot/handlers/webhook.py`
- Implemented signature verification
- Added async payment status update
- Updated documentation with webhook setup instructions

## Testing
1. Set up ngrok or similar tunnel
2. Configure Heleket webhook URL
3. Create test payment
4. Verify webhook is received and signature verified
5. Confirm payment status updated in database

## Related Issues
Closes #42
```

## Code Style

### Python

We follow PEP 8 with some modifications:

- **Line length**: 100 characters (not 79)
- **Indentation**: 4 spaces
- **Imports**: Grouped (stdlib, third-party, local) and sorted
- **Type hints**: Use for function signatures
- **Docstrings**: Use for public functions and classes

### Formatting Tools

We use:
- **Black**: Code formatting
- **Ruff**: Linting

Run before committing:
```bash
# Format code
black ajib/

# Lint
ruff check ajib/
```

### Example Good Code

```python
from __future__ import annotations

from typing import Optional

async def get_user_configs(
    telegram_id: int,
    status: Optional[str] = None,
) -> list[UserConfig]:
    """
    Fetch user configurations from the database.

    Args:
        telegram_id: Telegram user ID
        status: Optional status filter (active, expired, etc.)

    Returns:
        List of UserConfig objects

    Raises:
        DatabaseError: If database query fails
    """
    # Implementation here
    pass
```

## Documentation

### When to Update Documentation

- Adding new features
- Changing configuration options
- Modifying CLI commands
- Changing installation process
- Adding new environment variables

### Documentation Files

- **README.md**: Overview and quick start
- **SETUP.md**: Detailed setup guide
- **CONTRIBUTING.md**: This file
- **Code comments**: Complex logic and non-obvious decisions
- **Docstrings**: Public functions and classes

### Locale Files

When adding user-facing text:

1. Add key to `bot/locales/en.yml`
2. Add translation to `bot/locales/fa.yml`
3. Use the key in code: `i18n.t(lang, "your_key", param=value)`

## Questions?

- **Open an issue** for questions or discussions
- **Join discussions** in existing issues and PRs
- **Check documentation** first (README, SETUP, code comments)

## License

By contributing to AJIB, you agree that your contributions will be licensed under the MIT License.

---

Thank you for contributing! 🎉
