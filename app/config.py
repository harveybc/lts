"""
config.py

Default configuration values for the LTS (Live Trading System).
This file contains all parameters used in the app and its plugins with their default values.
The format must remain exactly the same as it's used by the config merger.

:author: LTS Development Team
:copyright: (c) 2025 LTS Project
:license: MIT
"""

DEFAULT_VALUES = {
    # System Configuration
    "load_config": None,
    "quiet_mode": False,
    "debug_mode": False,
    "log_level": "WARNING",
    
    # Core Plugin Configuration
    "core_plugin": "default_core",
    "global_latency": 5,  # minutes between portfolio executions
    "api_host": "0.0.0.0",
    "api_port": 8000,
    "api_workers": 1,
    "enable_api": True,
    
    # AAA Plugin Configuration
    "aaa_plugin": "default_aaa",
    "session_timeout": 3600,  # seconds
    "password_hash_algorithm": "sha256",
    "jwt_secret": "your-secret-key-change-this",
    "jwt_algorithm": "HS256",
    "jwt_expiration": 3600,
    
    # Pipeline Plugin Configuration
    "pipeline_plugin": "default_pipeline",
    "pipeline_timeout": 30,  # seconds
    "max_concurrent_portfolios": 10,
    
    # Strategy Plugin Configuration
    "strategy_plugin": "default_strategy",
    "default_position_size": 0.02,
    "default_max_open_positions": 1,
    "default_entry_rule": "prediction_threshold",
    "default_exit_rule": "stop_loss_take_profit",
    
    # Broker Plugin Configuration
    "broker_plugin": "default_broker",
    "default_broker_name": "demo_broker",
    
    # Prediction Provider Integration Configuration
    "prediction_provider_url": "http://localhost:8000",
    "prediction_provider_api_key": None,
    "prediction_provider_timeout": 300,  # seconds
    "prediction_provider_retries": 3,
    "prediction_provider_retry_delay": 5,  # seconds
    
    # Model Configuration for Predictions
    # Short-term predictions (1-6 hours) using 1h transformer model
    "short_term_model": {
        "predictor_plugin": "transformer",
        "window_size": 144,
        "batch_size": 128,
        "prediction_horizon": 6,
        "interval": "1h",
        "lookback_ticks": 1000,  # Number of previous ticks needed
        "model_config": "phase_3_1/phase_3_1_transformer_1h_config.json"
    },
    
    # Long-term predictions (1-6 days) using 1d CNN model  
    "long_term_model": {
        "predictor_plugin": "cnn", 
        "window_size": 256,
        "batch_size": 128,
        "prediction_horizon": 6,
        "interval": "1d",
        "lookback_ticks": 1000,  # Number of previous ticks needed
        "model_config": "phase_3_1_daily/phase_3_1_cnn_1d_config.json"
    },
    
    # CSV Test Configuration (for testing with perfect predictions)
    "csv_test_mode": False,
    "csv_test_data_path": "examples/data/phase_3/base_d1.csv",
    "csv_test_lookahead": True,  # Use future data for perfect predictions
    
    # Portfolio Plugin Configuration
    "portfolio_plugin": "default_portfolio",
    "default_allocation_method": "equal_weight",
    "default_max_total_exposure": 0.95,
    "default_rebalance_frequency": "daily",
    "default_portfolio_latency": 1,  # minutes between portfolio executions
    
    # Database Configuration
    "database_url": "sqlite:///./lts_trading.db",
    "database_echo": False,
    "database_pool_size": 10,
    "database_max_overflow": 20,
    
    # Web Interface Configuration
    "web_host": "0.0.0.0",
    "web_port": 8080,
    "web_workers": 1,
    "templates_dir": "templates",
    "static_dir": "static",
    
    # Trading Configuration
    "default_trailing_stop": 0.0050,
    "default_take_profit": 0.0100,
    "default_stop_loss": 0.0080,
    "default_signal_filter": "ema",
    "min_capital_per_asset": 100.0,
    "max_capital_per_asset": 50000.0,
    
    # Risk Management
    "max_drawdown": 0.20,
    "max_daily_loss": 0.05,
    "max_positions_per_user": 50,
    "max_portfolios_per_user": 10,
    
    # Logging Configuration
    "log_file": "lts.log",
    "log_rotation": "midnight",
    "log_retention": 30,  # days
    "remote_logging": False,
    "remote_log_url": None,
    
    # Performance Configuration
    "cache_enabled": True,
    "cache_ttl": 300,  # seconds
    "max_memory_usage": 1024,  # MB
    "max_cpu_usage": 80,  # percentage
    
    # Security Configuration
    "rate_limit_enabled": True,
    "max_requests_per_minute": 60,
    "max_login_attempts": 5,
    "lockout_duration": 300,  # seconds
    
    # Plugin-specific default parameters
    "plugin_timeout": 30,  # seconds
    "plugin_retry_attempts": 3,
    "plugin_retry_delay": 1,  # seconds
}
