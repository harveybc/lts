# Database Schema: Feature Extractor (LTS)

This document describes the SQLite/SQLAlchemy schema for all persistent data in the Feature Extractor system, matching all design documents and requirements.

## Tables and Columns

### users
- id: Integer, primary key, indexed
- username: String, unique, indexed, not null
- email: String, unique, indexed
- hashed_password: String, not null
- role: String, default 'user'
- is_active: Boolean, default True
- created_at: DateTime, default now

### sessions
- id: Integer, primary key, indexed
- user_id: Integer, foreign key to users.id
- token: String, unique, not null
- created_at: DateTime, default now
- expires_at: DateTime, not null

### audit_logs
- id: Integer, primary key, indexed
- user_id: Integer, foreign key to users.id
- action: String, not null
- timestamp: DateTime, default now
- details: Text

### config_entries
- id: Integer, primary key, indexed
- key: String, unique, not null
- value: Text, not null
- updated_at: DateTime, default now

### statistics
- id: Integer, primary key, indexed
- key: String, indexed, not null
- value: Float, not null
- timestamp: DateTime, default now

### portfolios
- id: Integer, primary key, indexed
- user_id: Integer, foreign key to users.id
- name: String, not null
- is_active: Boolean, default True
- created_at: DateTime, default now
- portfolio_plugin: String (name of portfolio plugin)
- portfolio_plugin_config: Text (JSON string)
- strategy_plugin: String (name of strategy plugin)
- strategy_plugin_config: Text (JSON string)

### assets
- id: Integer, primary key, indexed
- portfolio_id: Integer, foreign key to portfolios.id
- symbol: String, not null
- parameters: JSON
- created_at: DateTime, default now
- broker_plugin: String (name of broker plugin)
- broker_plugin_config: Text (JSON string)
- strategy_plugin: String (name of strategy plugin)
- strategy_plugin_config: Text (JSON string)

### analytics
- id: Integer, primary key, indexed
- portfolio_id: Integer, foreign key to portfolios.id
- metric: String, not null
- value: Float
- plot_data: Text (JSON or base64-encoded image)
- timestamp: DateTime, default now

---

All plugin assignments and configurations are explicit fields in the portfolios and assets tables. No plugin_assignments table is present. This schema is fully aligned with all design and traceability requirements.
