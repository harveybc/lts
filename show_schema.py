"""
LTS Database Schema Documentation

This script shows the exact tables and columns in the LTS database
based on the SQLAlchemy models defined in app/database.py
"""

def show_database_schema():
    """Display the complete LTS database schema"""
    
    print("=" * 80)
    print("LTS (Live Trading System) - Database Schema")
    print("=" * 80)
    print()
    
    tables = {
        "users": {
            "description": "User authentication and management",
            "columns": [
                ("id", "Integer", "Primary Key", "Unique user ID"),
                ("username", "String", "Unique, Not Null", "Unique username"),
                ("email", "String", "Unique, Not Null", "Unique email address"),
                ("password_hash", "String", "Not Null", "Hashed password"),
                ("role", "String", "Not Null", "User role (admin, user, trader)"),
                ("is_active", "Boolean", "Default True", "Account active flag"),
                ("created_at", "DateTime", "Not Null", "Account creation timestamp")
            ]
        },
        
        "sessions": {
            "description": "User session management",
            "columns": [
                ("id", "Integer", "Primary Key", "Unique session ID"),
                ("user_id", "Integer", "Foreign Key", "Reference to users.id"),
                ("token", "String", "Unique, Not Null", "Session token (JWT)"),
                ("created_at", "DateTime", "Not Null", "Session creation timestamp"),
                ("expires_at", "DateTime", "Not Null", "Session expiration timestamp")
            ]
        },
        
        "audit_logs": {
            "description": "System audit trail and accounting",
            "columns": [
                ("id", "Integer", "Primary Key", "Unique log entry ID"),
                ("user_id", "Integer", "Foreign Key", "Reference to users.id"),
                ("action", "String", "Not Null", "Action performed"),
                ("timestamp", "DateTime", "Not Null", "When action occurred"),
                ("details", "Text", "Nullable", "Additional context and details")
            ]
        },
        
        "config": {
            "description": "System configuration key-value store",
            "columns": [
                ("id", "Integer", "Primary Key", "Unique config entry ID"),
                ("key", "String", "Unique, Not Null", "Configuration key"),
                ("value", "Text", "Not Null", "Configuration value (JSON/text)"),
                ("updated_at", "DateTime", "Not Null", "Last update timestamp")
            ]
        },
        
        "statistics": {
            "description": "System metrics and analytics",
            "columns": [
                ("id", "Integer", "Primary Key", "Unique statistic entry ID"),
                ("key", "String", "Not Null", "Statistic key/name"),
                ("value", "Float", "Not Null", "Statistic value"),
                ("timestamp", "DateTime", "Not Null", "When statistic was recorded")
            ]
        },
        
        "portfolios": {
            "description": "User portfolios with plugin configurations",
            "columns": [
                ("id", "Integer", "Primary Key", "Unique portfolio ID"),
                ("user_id", "Integer", "Foreign Key", "Reference to users.id"),
                ("name", "String", "Not Null", "Portfolio name"),
                ("description", "Text", "Nullable", "Portfolio description"),
                ("is_active", "Boolean", "Default True", "Portfolio active flag"),
                ("portfolio_plugin", "String", "Not Null", "Portfolio plugin name"),
                ("portfolio_config", "Text", "Nullable", "Portfolio plugin JSON config"),
                ("total_capital", "Numeric", "Default 0", "Total capital allocated"),
                ("last_execution", "DateTime", "Nullable", "Last execution timestamp"),
                ("portfolio_latency", "Integer", "Default 60", "Minutes between executions"),
                ("created_at", "DateTime", "Not Null", "Portfolio creation timestamp"),
                ("updated_at", "DateTime", "Not Null", "Last update timestamp")
            ]
        },
        
        "assets": {
            "description": "Assets within portfolios with strategy/broker configs",
            "columns": [
                ("id", "Integer", "Primary Key", "Unique asset ID"),
                ("portfolio_id", "Integer", "Foreign Key", "Reference to portfolios.id"),
                ("symbol", "String", "Not Null", "Asset symbol (e.g., EUR/USD)"),
                ("name", "String", "Not Null", "Asset name"),
                ("strategy_plugin", "String", "Not Null", "Strategy plugin name"),
                ("strategy_config", "Text", "Nullable", "Strategy plugin JSON config"),
                ("broker_plugin", "String", "Not Null", "Broker plugin name"),
                ("broker_config", "Text", "Nullable", "Broker plugin JSON config"),
                ("pipeline_plugin", "String", "Not Null", "Pipeline plugin name"),
                ("pipeline_config", "Text", "Nullable", "Pipeline plugin JSON config"),
                ("allocated_capital", "Numeric", "Default 0", "Capital allocated to asset"),
                ("is_active", "Boolean", "Default True", "Asset active flag"),
                ("created_at", "DateTime", "Not Null", "Asset creation timestamp"),
                ("updated_at", "DateTime", "Not Null", "Last update timestamp")
            ]
        },
        
        "orders": {
            "description": "Trading orders and execution records",
            "columns": [
                ("id", "Integer", "Primary Key", "Unique order ID"),
                ("asset_id", "Integer", "Foreign Key", "Reference to assets.id"),
                ("order_type", "String", "Not Null", "Order type (market, limit, etc.)"),
                ("side", "String", "Not Null", "Order side (buy, sell)"),
                ("quantity", "Numeric", "Not Null", "Order quantity"),
                ("price", "Numeric", "Nullable", "Order price"),
                ("stop_loss", "Numeric", "Nullable", "Stop loss price"),
                ("take_profit", "Numeric", "Nullable", "Take profit price"),
                ("status", "String", "Not Null", "Order status"),
                ("broker_order_id", "String", "Nullable", "Broker's order ID"),
                ("broker_response", "Text", "Nullable", "Full broker response JSON"),
                ("created_at", "DateTime", "Not Null", "Order creation timestamp"),
                ("updated_at", "DateTime", "Not Null", "Last update timestamp"),
                ("filled_at", "DateTime", "Nullable", "Order fill timestamp")
            ]
        },
        
        "positions": {
            "description": "Open and closed trading positions",
            "columns": [
                ("id", "Integer", "Primary Key", "Unique position ID"),
                ("asset_id", "Integer", "Foreign Key", "Reference to assets.id"),
                ("order_id", "Integer", "Foreign Key", "Reference to orders.id"),
                ("side", "String", "Not Null", "Position side (long, short)"),
                ("quantity", "Numeric", "Not Null", "Position quantity"),
                ("entry_price", "Numeric", "Not Null", "Entry price"),
                ("current_price", "Numeric", "Nullable", "Current market price"),
                ("unrealized_pnl", "Numeric", "Default 0", "Unrealized P&L"),
                ("realized_pnl", "Numeric", "Default 0", "Realized P&L"),
                ("status", "String", "Not Null", "Position status (open, closed)"),
                ("broker_position_id", "String", "Nullable", "Broker's position ID"),
                ("opened_at", "DateTime", "Not Null", "Position opening timestamp"),
                ("closed_at", "DateTime", "Nullable", "Position closing timestamp"),
                ("updated_at", "DateTime", "Not Null", "Last update timestamp")
            ]
        }
    }
    
    for table_name, table_info in tables.items():
        print(f"Table: {table_name.upper()}")
        print(f"Description: {table_info['description']}")
        print("-" * 80)
        print(f"{'Column':<20} {'Type':<12} {'Constraints':<15} {'Description'}")
        print("-" * 80)
        
        for column_info in table_info['columns']:
            column_name, column_type, constraints, description = column_info
            print(f"{column_name:<20} {column_type:<12} {constraints:<15} {description}")
        
        print()
        print()
    
    print("=" * 80)
    print("Key Features of the LTS Database:")
    print("=" * 80)
    print("• User-centric design: Each user can have multiple portfolios")
    print("• Portfolio-centric trading: Each portfolio has its own plugin configuration")
    print("• Asset-level customization: Each asset has its own strategy and broker plugins")
    print("• Complete audit trail: All actions are logged in audit_logs")
    print("• Flexible configuration: Plugin configs stored as JSON in database")
    print("• Trading execution tracking: Orders and positions fully tracked")
    print("• Capital management: Portfolio and asset-level capital allocation")
    print("• Plugin-based architecture: All components are pluggable")
    print()
    
    print("=" * 80)
    print("Trading Flow:")
    print("=" * 80)
    print("1. Core plugin executes every 'global_latency' minutes")
    print("2. For each active portfolio:")
    print("   - Check if 'portfolio_latency' minutes have passed")
    print("   - Portfolio plugin allocates capital among assets")
    print("   - For each active asset:")
    print("     • Strategy plugin processes market data and returns action")
    print("     • Broker plugin executes the action with broker API")
    print("     • Results are saved to orders and positions tables")
    print("3. Web interface displays portfolios, assets, and trading activity")
    print("4. All actions are logged for audit and compliance")
    print("=" * 80)

if __name__ == "__main__":
    show_database_schema()
