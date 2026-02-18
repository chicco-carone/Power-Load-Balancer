# AGENTS.md - Agentic Coding Guidelines

This document provides guidelines for agentic coding assistants operating in the Power Load Balancer repository.

## Build, Lint, and Test Commands

### Setup
```bash
./scripts/setup
```
Installs Python dependencies from requirements.txt (Home Assistant, ruff).

### Lint and Format
```bash
./scripts/lint
```
Runs ruff formatter and linter with automatic fixes.
- Format: `ruff format .`
- Check/Fix: `ruff check . --fix`

### Development
```bash
./scripts/develop
```
Starts a local Home Assistant instance with the integration loaded in the config directory.

### Manual Testing
No automated test suite exists. Testing is done via Home Assistant UI and manual verification.

## Code Style Guidelines

### General Rules (from .github/copilot-instructions.md)
- Never use single-line comments; use multi-line comments or docstrings only
- Code must be mypy and ruff compliant
- Always add type hints to functions; avoid using `Any`
- Always write docstrings in English

### Python Version
Target Python 3.13 (specified in .ruff.toml)

### Ruff Configuration
- Select: ALL linting rules
- Ignore: ANN401 (Any), D203 (no-blank-line-before-class), D212 (multi-line-summary-first-line), COM812, ISC001
- Max complexity: 25 (McCabe)

### Imports
- Use standard library imports first, then third-party imports, then local imports
- Separate groups with blank lines
- Order imports alphabetically within each group
- Always include type hints for function parameters and return types
- Use `from typing import ...` for type annotations

### Formatting
- Use ruff formatter (line length follows ruff defaults)
- Follow Black-compatible formatting (ruff --fix auto-corrects style)
- No trailing whitespace

### Type Hints and Naming
- All function parameters and return types must have type hints
- Use descriptive variable names in snake_case
- Use UPPERCASE_WITH_UNDERSCORES for constants
- Avoid using `Any` type; use specific types or TypeVar/Protocol

### Error Handling
- Use custom exceptions from `exceptions.py` instead of generic exceptions
- Wrap external calls in try/except with detailed logging
- Log errors with ContextLogger for operation correlation
- Include error codes and details in custom exceptions

## Architecture Overview

This is a Home Assistant custom integration that monitors household power consumption and automatically turns off less critical appliances when exceeding a configured power budget.

### Core Components
- **power_balancer.py**: Central class managing monitoring, balancing logic, appliance control
- **balancing_engine.py**: Core balancing algorithms and state management
- **power_monitor.py**: Power sensor monitoring and tracking
- **appliance_controller.py**: Appliance control and state management
- **context_logger.py**: Structured logging with operation IDs
- **validation.py**: Entity and power value validation
- **circuit_breaker.py**: Circuit breaker pattern for reliability
- **retry.py**: Retry decorators for transient failures
- **config_flow.py**: Multi-step configuration UI
- **exceptions.py**: Custom exception hierarchy

### Key Patterns
- **Logging**: Use ContextLogger with operation IDs: `logger = ContextLogger(_LOGGER, "component").new_operation("op")`
- **Async**: Use `hass.async_create_task()` for background tasks; avoid blocking
- **State Tracking**: Use `async_track_state_change_event()` for reactive updates
- **Power Units**: Always convert to watts before calculations
- **Validation**: Validate entities with `validate_entity_id()` and power with `validate_power_value()`

## Coding Patterns

### Docstring Format
```python
"""
Brief description of the function.

Longer description if needed. Explain parameters, return value, and any exceptions
that might be raised.
"""
```

### Type Hints
```python
def process_power(value: float, unit: str) -> float:
    """Convert power value to watts."""
    return value
```

### Error Handling
```python
try:
    await self.hass.services.async_call(...)
except PowerLoadBalancerError as err:
    logger.error("Operation failed", error_code=err.code, details=err.details)
    raise
```

### Custom Exceptions
Always use exceptions from `exceptions.py` with error codes:
```python
raise ConfigurationError(
    "Invalid entity",
    error_code="INVALID_ENTITY",
    details={"entity_id": entity_id}
)
```
