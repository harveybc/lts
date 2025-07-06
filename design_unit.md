# Unit-Level Design Constraints and Requirements

This document details the unit-level (component/module/API/AAA/web) design for the LTS project. All requirements are mapped to acceptance, integration, and system requirements and referenced in the unit test plan for full traceability.

## Unit Requirements Table

| Unit Req ID | Requirement/Scenario | Description | Source |
|-------------|---------------------|-------------|--------|
| U1 | AAA plugin methods (register, login, role, audit, session) | AAA plugins must implement secure registration, authentication, role management, session, and audit methods. | plugins_aaa/ |
| U2 | Web/API endpoint handlers (auth, portfolio, asset, analytics) | Web/API must implement endpoint handlers for all user, portfolio, asset, and analytics actions, with full validation, error handling, and rate limiting. | web.py |
| U3 | Plugin initialization with config and default params | Plugins must initialize with config dict and default params as specified in plugin_params. | REFERENCE_plugins.md, plugin code |
| U4 | Plugin set_params, get_debug_info, add_debug_info methods | Plugins must implement set_params, get_debug_info, add_debug_info with exact structure. | REFERENCE_plugins.md, plugin code |
| U5 | Plugin main interface methods (run, decide, open_order, allocate, etc.) | Each plugin type must implement its required main interface methods. | REFERENCE_plugins.md, plugin code |
| U6 | Negative tests: invalid parameters, missing config, unexpected input | Plugins and API must handle invalid parameters, missing config, unexpected input, and rate limit violations gracefully. | REFERENCE_plugins.md, plugin code, web.py |
| U7 | Core modules (config, loader, web, etc.) behave as specified | Core modules must implement their public interfaces and handle errors as specified, including security controls. | config_handler.py, cli.py, web.py |

## Design Constraints
- AAA (auth/authz/accounting) must be implemented as plugins, with secure password handling and session management.
- All web/API endpoint handlers must validate input, enforce authentication/authorization, implement rate limiting/backoff, and handle errors gracefully.
- Each plugin and module must have a clear, testable public interface.
- All plugins must use the exact required structure for params and debug info.
- Each behavior must be testable in isolation, with both positive and negative tests.
- API endpoints must enforce rate limiting and backoff strategies to prevent abuse and DoS.
- No tests are written for plugin loading or config merging logic.

## Design Decisions
- All plugins implement:
  - `plugin_params` (dict of default values)
  - `plugin_debug_vars` (list of debug/metric variable names)
  - `__init__`, `set_params`, `get_debug_info`, `add_debug_info` (with exact structure)
- Main interface methods (e.g., `run`, `decide`, `open_order`, etc.) are implemented as specified in REFERENCE_plugins.md.
- Negative tests are included for invalid parameters, error handling, and rate limit violations.
- Web/API endpoint handlers are implemented with full validation, error handling, audit logging, and rate limiting/backoff (e.g., per-IP, per-user, global limits).
- API endpoints log and report all rate limit violations and security events.
- Security measures include input validation, authentication, authorization, rate limiting, audit logging, and error sanitization.

## Referenced Files
- `REFERENCE_plugins.md` (plugin interface, required structure)
- `plugins_aaa/` (AAA plugin implementations)
- `web.py` (API endpoints, validation, error handling, rate limiting)
- `plugins_broker/`, `plugins_pipeline/`, `plugins_portfolio/`, `plugins_strategy/` (plugin implementations)
- `config_handler.py`, `cli.py` (core modules)

## Traceability
- Each unit requirement is mapped to one or more unit test cases in `plan_unit.md`.
- All requirements are covered by at least one test case.

---

# Feature-Extractor Project: Unit-Level Design Constraints and Requirements

This document details the unit-level design for the feature-extractor project. All requirements are mapped to the unit test plan for full traceability.

## Security, Testability, and Code Quality Requirements (Additions)
- All argument parsing, config, and I/O MUST be robust to invalid input, edge cases, and malicious data (fuzz/property-based testing recommended).
- All modules MUST avoid use of insecure functions (e.g., eval, exec, unsafe subprocess calls).
- All outputs (model, logs, results) MUST have secure file permissions.
- All error messages and logs MUST be sanitized to avoid leaking sensitive information.
- All dependencies MUST be pinned and scanned for vulnerabilities.
- All secrets/configs MUST be stored securely (never in plain text/config files; use environment variables or vaults).
- All modules MUST be covered by static analysis and linting in CI.
- All critical modules MUST have high test coverage (e.g., 100% for security-relevant code).
- All remote files/configs MUST be integrity-checked (e.g., checksum, signature).
- All unit tests MUST be fully isolated (no shared state, no network unless explicitly tested).
- Mutation testing MUST be used to ensure test suite effectiveness.
- All public functions/classes MUST have docstrings and documentation coverage must be measured.
- All API endpoints MUST implement rate limiting and backoff strategies, log violations, and be tested for abuse scenarios.

## Unit Design Requirements Table
| Unit Req ID | Requirement/Scenario | Description | Source |
|-------------|---------------------|-------------|--------|
| UTR1 | CLI argument parsing | CLI must parse and validate all arguments, including plugin-specific ones. | cli.py |
| UTR2 | Config loading/merging | Config handler must load/merge from CLI, file, remote. | config_handler.py |
| UTR3 | Data loading/writing | Data handler must load/write CSV robustly. | data_handler.py |
| UTR4 | Plugin loader | Loader must dynamically import plugins and enforce interface. | plugin_loader.py |
| UTR5 | Plugin base classes | Encoder/decoder plugin base must enforce required methods/params. | encoder_plugins/, decoder_plugins/ |
| UTR6 | Model save/load | Model I/O must support local/remote for both plugin types. | main.py, config_handler.py |
| UTR7 | Remote logging | Logging must support remote endpoints and error handling. | main.py |
| UTR8 | Error handling | All modules must catch, log, and report errors. | all |
| UTR9 | Secure coding and analysis | All code must pass static analysis, linting, and avoid insecure functions. | all |
| UTR10 | Test coverage | All critical modules must have high test coverage. | all |
| UTR11 | Test isolation | All unit tests must be fully isolated. | tests/ |
| UTR12 | Mutation testing | Mutation testing must be used for test suite effectiveness. | tests/ |
| UTR13 | Documentation coverage | All public functions/classes must have docstrings and be measured. | all |
| UTR14 | API rate limiting | All API endpoints must implement and test rate limiting/backoff, and log/report violations. | web.py |

## Design Constraints
- All argument parsing, config, and I/O must be robust to invalid input, edge cases, and malicious data.
- Plugin loader must enforce interface for all plugins and avoid insecure code execution.
- Model I/O must support both local and remote endpoints, with integrity checks.
- Logging must be robust to network errors and sanitized.
- All error handling must be comprehensive and logged.
- All outputs must have secure permissions.
- All dependencies must be pinned and scanned for vulnerabilities.
- All secrets/configs must be handled securely.
- All code must pass static analysis and linting.
- All critical modules must have high test coverage.
- All unit tests must be fully isolated.
- Mutation testing must be used for test suite effectiveness.
- All public functions/classes must have docstrings and documentation coverage must be measured.
- All API endpoints must implement and test rate limiting/backoff, and log/report violations.

## Design Decisions
- Use argparse for CLI parsing, supporting plugin-specific args, with input validation and fuzz testing.
- Config handler merges CLI, file, and remote config with precedence and integrity checks.
- Data handler uses pandas for CSV I/O, with error handling and input validation.
- Plugin loader uses importlib and interface checks, avoids insecure code execution.
- Plugin base classes define required methods and parameter schema.
- Model I/O uses standard formats and requests for HTTP, with integrity checks.
- Logging uses Python logging module, with remote support and sanitized output.
- All errors are logged and reported to the user, with sanitized output.
- All dependencies are pinned and scanned for vulnerabilities.
- All code is statically analyzed and linted in CI.
- All critical modules have high test coverage.
- All unit tests are fully isolated.
- Mutation testing is used for test suite effectiveness.
- All public functions/classes have docstrings and documentation coverage is measured.
- All API endpoints implement and test rate limiting/backoff, and log/report violations.

## Traceability
- Each unit design requirement is mapped to one or more unit test cases in `plan_unit.md`.
- All requirements are covered by at least one test case.

---

# Unit-Level Database Requirements and ORM Integration

## Database Requirements
- All persistent data (AAA, config, statistics, audit logs) MUST be stored in SQLite via SQLAlchemy ORM.
- No direct SQL or alternative database engines are permitted; all access must use SQLAlchemy ORM models.
- The database schema MUST be fully documented and version-controlled. See `README.md` for the complete schema reference.
- All database models MUST be defined in Python using SQLAlchemy declarative base, with explicit types, constraints, and relationships.
- All migrations, queries, and schema changes MUST be covered by unit tests.
- All plugin/database integration (e.g., AAA, core, UI plugins) MUST use the ORM for all persistent data access and modification.
- All database operations MUST be atomic, transactional, and robust to errors (with rollback and error handling tested at the unit level).
- All sensitive data (e.g., passwords, tokens) MUST be securely stored (hashed/salted) and never in plain text.
- All database access patterns (CRUD, search, audit, statistics) MUST be tested for correctness, security, and performance at the unit level.

## Database Schema Summary (see README.md for full details)
| Table                | Purpose                        | Key Fields/Constraints                |
|----------------------|--------------------------------|---------------------------------------|
| users                | AAA user accounts              | id, username, password_hash, roles    |
| sessions             | AAA session management         | id, user_id, token, expiry            |
| audit_logs           | Security/audit events          | id, user_id, event, timestamp         |
| config               | System/plugin configuration    | id, key, value, updated_at            |
| statistics           | Usage/statistics tracking      | id, metric, value, timestamp          |

- All tables are defined in SQLAlchemy ORM and referenced in the main documentation.
- All plugin types that persist data MUST use these tables or extend them via ORM models.

## Unit Test Coverage for Database
- All ORM models MUST have unit tests for creation, update, delete, and query operations.
- All migrations and schema changes MUST be tested for correctness and reversibility.
- All plugin/database integration points MUST be covered by unit tests, including error and edge cases.
- All AAA, config, statistics, and audit log operations MUST be tested for security, correctness, and traceability.
- All database-related code MUST be isolated in tests (using in-memory SQLite or test fixtures).

## Traceability
- Each database requirement is mapped to one or more unit test cases in `plan_unit.md` and `tests/unit/test_unit.py`.
- All requirements are covered by at least one test case, with references to the main documentation for schema and integration details.

---

This document is updated as new unit requirements, constraints, or design decisions are defined. All requirements are specified with exact parameters, behaviors, and referenced files for full traceability, coverage, and security.

*End of Document*
