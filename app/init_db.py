"""
init_db.py

Database initialization script for the LTS (Live Trading System).
This script creates the database tables and sets up initial data.

:author: LTS Development Team
:copyright: (c) 2025 LTS Project
:license: MIT
"""

import sys
import os

# Add the parent directory to the path so we can import from app
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.database import init_db, SessionLocal, User, Config, Portfolio, Asset
from datetime import datetime, timezone
import hashlib
import json

def create_default_admin():
    """Create default admin user"""
    db = SessionLocal()
    try:
        # Check if admin user already exists
        admin_user = db.query(User).filter(User.username == "admin").first()
        if not admin_user:
            # Create default admin user
            password_hash = hashlib.sha256("admin123".encode()).hexdigest()
            admin_user = User(
                username="admin",
                email="admin@lts.local",
                password_hash=password_hash,
                role="admin",
                is_active=True,
                created_at=datetime.now(timezone.utc)
            )
            db.add(admin_user)
            db.commit()
            print("Default admin user created (username: admin, password: admin123)")
        else:
            print("Admin user already exists")
    except Exception as e:
        print(f"Error creating admin user: {e}")
        db.rollback()
    finally:
        db.close()

def create_sample_portfolios():
    """Create sample portfolios and assets for testing"""
    db = SessionLocal()
    try:
        # Check if sample portfolios already exist
        existing_portfolio = db.query(Portfolio).filter(Portfolio.name == "Sample Portfolio").first()
        if not existing_portfolio:
            # Get the admin user
            admin_user = db.query(User).filter(User.username == "admin").first()
            if admin_user:
                # Create sample portfolio
                portfolio = Portfolio(
                    user_id=admin_user.id,
                    name="Sample Portfolio",
                    description="Sample trading portfolio for testing",
                    is_active=True,
                    portfolio_plugin="default_portfolio",
                    portfolio_config=json.dumps({
                        "total_capital": 10000.0,
                        "allocation_method": "equal_weight",
                        "max_total_exposure": 0.95,
                        "rebalance_frequency": "daily"
                    }),
                    total_capital=10000.0,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc)
                )
                db.add(portfolio)
                db.commit()
                db.refresh(portfolio)
                
                # Create sample assets
                sample_assets = [
                    {
                        "symbol": "EUR/USD",
                        "name": "Euro / US Dollar",
                        "strategy_plugin": "default_strategy",
                        "strategy_config": json.dumps({
                            "entry_rule": "prediction_threshold",
                            "exit_rule": "stop_loss_take_profit",
                            "max_open_positions": 1,
                            "position_size": 0.02
                        }),
                        "broker_plugin": "default_broker",
                        "broker_config": json.dumps({
                            "broker_name": "demo_broker",
                            "account_id": "demo_account",
                            "spread": 0.0002
                        }),
                        "pipeline_plugin": "default_pipeline",
                        "pipeline_config": json.dumps({
                            "trailing_stop": 0.0050,
                            "take_profit": 0.0100,
                            "stop_loss": 0.0080,
                            "signal_filter": "ema"
                        }),
                        "allocated_capital": 5000.0
                    },
                    {
                        "symbol": "GBP/USD",
                        "name": "British Pound / US Dollar",
                        "strategy_plugin": "default_strategy",
                        "strategy_config": json.dumps({
                            "entry_rule": "prediction_threshold",
                            "exit_rule": "stop_loss_take_profit",
                            "max_open_positions": 1,
                            "position_size": 0.02
                        }),
                        "broker_plugin": "default_broker",
                        "broker_config": json.dumps({
                            "broker_name": "demo_broker",
                            "account_id": "demo_account",
                            "spread": 0.0003
                        }),
                        "pipeline_plugin": "default_pipeline",
                        "pipeline_config": json.dumps({
                            "trailing_stop": 0.0060,
                            "take_profit": 0.0120,
                            "stop_loss": 0.0100,
                            "signal_filter": "ema"
                        }),
                        "allocated_capital": 5000.0
                    }
                ]
                
                for asset_data in sample_assets:
                    asset = Asset(
                        portfolio_id=portfolio.id,
                        symbol=asset_data["symbol"],
                        name=asset_data["name"],
                        is_active=True,
                        strategy_plugin=asset_data["strategy_plugin"],
                        strategy_config=asset_data["strategy_config"],
                        broker_plugin=asset_data["broker_plugin"],
                        broker_config=asset_data["broker_config"],
                        pipeline_plugin=asset_data["pipeline_plugin"],
                        pipeline_config=asset_data["pipeline_config"],
                        allocated_capital=asset_data["allocated_capital"],
                        max_positions=1,
                        created_at=datetime.now(timezone.utc),
                        updated_at=datetime.now(timezone.utc)
                    )
                    db.add(asset)
                
                db.commit()
                print("Sample portfolio and assets created")
            else:
                print("Admin user not found, skipping sample portfolio creation")
        else:
            print("Sample portfolio already exists")
    except Exception as e:
        print(f"Error creating sample portfolios: {e}")
        db.rollback()
    finally:
        db.close()

def create_default_config():
    """Create default configuration entries"""
    db = SessionLocal()
    try:
        # Default configurations
        default_configs = [
            {"key": "system.name", "value": "LTS - Live Trading System"},
            {"key": "system.version", "value": "1.0.0"},
            {"key": "aaa_plugin", "value": "default_aaa"},
            {"key": "core_plugin", "value": "default_core"},
            {"key": "pipeline_plugin", "value": "default_pipeline"},
            {"key": "strategy_plugin", "value": "default_strategy"},
            {"key": "broker_plugin", "value": "default_broker"},
            {"key": "portfolio_plugin", "value": "default_portfolio"},
        ]
        
        for config_data in default_configs:
            existing_config = db.query(Config).filter(Config.key == config_data["key"]).first()
            if not existing_config:
                config_entry = Config(
                    key=config_data["key"],
                    value=config_data["value"],
                    updated_at=datetime.now(timezone.utc)
                )
                db.add(config_entry)
        
        db.commit()
        print("Default configuration entries created")
    except Exception as e:
        print(f"Error creating default config: {e}")
        db.rollback()
    finally:
        db.close()

def main():
    """Initialize the database"""
    print("Initializing LTS Database...")
    
    # Create tables
    init_db()
    
    # Create default admin user
    create_default_admin()
    
    # Create default configuration
    create_default_config()
    
    # Create sample portfolios
    create_sample_portfolios()
    
    print("Database initialization completed successfully!")

if __name__ == "__main__":
    main()
