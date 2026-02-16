"""
database.py

Defines the SQLAlchemy ORM models and database setup for the LTS (Live Trading System).
This implements the complete LTS schema based on the documentation and user requirements.

The system is portfolio-centric where:
- Users have portfolios (active/inactive)
- Portfolios have portfolio plugins and JSON configs
- Portfolios contain assets
- Assets have strategy/broker/pipeline plugins and JSON configs
- Orders and positions track trading activity
"""
import os as _os
_QUIET = _os.environ.get('LTS_QUIET', '0') == '1'

from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, ForeignKey, Boolean, Text, JSON, Numeric, text
from sqlalchemy.orm import sessionmaker, relationship, declarative_base
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
import datetime
import asyncio
from contextlib import asynccontextmanager, contextmanager

# Use aiosqlite for async operations
DATABASE_URL = 'sqlite+aiosqlite:///./lts_trading.db'
ASYNC_DATABASE_URL = 'sqlite+aiosqlite://'

# Synchronous engine for tests and sync components
sync_engine = create_engine(DATABASE_URL.replace('+aiosqlite', ''), connect_args={"check_same_thread": False})
SyncSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=sync_engine)

# Base for declarative models
Base = declarative_base()

def get_db_session():
    engine = create_engine(DATABASE_URL.replace('+aiosqlite', ''), connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return SessionLocal()

@contextmanager
def db_session():
    """Provide a transactional scope around a series of operations (for synchronous parts)."""
    engine = create_engine(DATABASE_URL.replace('+aiosqlite', ''), connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except:
        session.rollback()
        raise
    finally:
        session.close()

class Database:
    def __init__(self, db_path=':memory:'):
        self.is_memory = db_path == ':memory:'
        self.db_url = f"{ASYNC_DATABASE_URL}/{db_path}" if self.is_memory else f"sqlite+aiosqlite:///{db_path}"
        self.engine = create_async_engine(self.db_url, echo=False)
        self.SessionLocal = sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        )

    async def initialize(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def cleanup(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    @asynccontextmanager
    async def get_session(self) -> AsyncSession:
        """Provide a transactional scope around a series of operations."""
        session = self.SessionLocal()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def execute_sql(self, sql, params={}):
        async with self.get_session() as session:
            await session.execute(text(sql), params)

    async def fetch_all(self, sql, params={}):
        async with self.get_session() as session:
            result = await session.execute(text(sql), params)
            return result.fetchall()

# LTS Database Models - Complete Schema

class User(Base):
    """User table for AAA (Authentication, Authorization, Accounting)"""
    __tablename__ = 'users'
    
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    email = Column(String(100), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), nullable=False, default="user")  # admin, user, etc.
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc), 
                       onupdate=lambda: datetime.datetime.now(datetime.timezone.utc), nullable=False)
    
    # Relationships
    sessions = relationship("Session", back_populates="user")
    audit_logs = relationship("AuditLog", back_populates="user")
    portfolios = relationship("Portfolio", back_populates="user")
    orders = relationship("Order", back_populates="user")

class Session(Base):
    """Session table for user session management"""
    __tablename__ = 'sessions'
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    token = Column(String(255), unique=True, index=True, nullable=False)  # JWT or random token
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    
    # Relationships
    user = relationship("User", back_populates="sessions")

class AuditLog(Base):
    """Audit log table for accounting and traceability"""
    __tablename__ = 'audit_logs'
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=True)
    action = Column(String(100), nullable=False)
    timestamp = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc), nullable=False)
    details = Column(Text, nullable=True)
    
    # Relationships
    user = relationship("User", back_populates="audit_logs")

class Config(Base):
    """Configuration table for system settings"""
    __tablename__ = 'config'
    
    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(100), unique=True, index=True, nullable=False)
    value = Column(Text, nullable=False)  # JSON or text value
    updated_at = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc), 
                       onupdate=lambda: datetime.datetime.now(datetime.timezone.utc), nullable=False)

class Statistics(Base):
    """Statistics table for system metrics and analytics"""
    __tablename__ = 'statistics'
    
    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(100), nullable=False)
    value = Column(Float, nullable=False)
    timestamp = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc), nullable=False)

class Portfolio(Base):
    """Portfolio table - core of the LTS system"""
    __tablename__ = 'portfolios'
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    portfolio_plugin = Column(String(100), nullable=False, default="default_portfolio")
    portfolio_config = Column(JSON, nullable=True)  # JSON config for portfolio plugin
    total_capital = Column(Numeric(15, 2), nullable=False, default=0.0)
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc), 
                       onupdate=lambda: datetime.datetime.now(datetime.timezone.utc), nullable=False)
    
    # Relationships
    user = relationship("User", back_populates="portfolios")
    assets = relationship("Asset", back_populates="portfolio")
    orders = relationship("Order", back_populates="portfolio")
    positions = relationship("Position", back_populates="portfolio")

class Asset(Base):
    """Asset table - instruments within portfolios"""
    __tablename__ = 'assets'
    
    id = Column(Integer, primary_key=True, index=True)
    portfolio_id = Column(Integer, ForeignKey('portfolios.id'), nullable=False)
    symbol = Column(String(20), nullable=False)  # e.g., "EUR/USD", "AAPL"
    name = Column(String(100), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    strategy_plugin = Column(String(100), nullable=False, default="default_strategy")
    strategy_config = Column(JSON, nullable=True)  # JSON config for strategy plugin
    broker_plugin = Column(String(100), nullable=False, default="default_broker")
    broker_config = Column(JSON, nullable=True)  # JSON config for broker plugin
    pipeline_plugin = Column(String(100), nullable=False, default="default_pipeline")
    pipeline_config = Column(JSON, nullable=True)  # JSON config for pipeline plugin
    allocated_capital = Column(Numeric(15, 2), nullable=False, default=0.0)
    max_positions = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc), 
                       onupdate=lambda: datetime.datetime.now(datetime.timezone.utc), nullable=False)
    
    # Relationships
    portfolio = relationship("Portfolio", back_populates="assets")
    orders = relationship("Order", back_populates="asset")
    positions = relationship("Position", back_populates="asset")

class Order(Base):
    """Order table - trading orders and execution records"""
    __tablename__ = 'orders'
    
    id = Column(Integer, primary_key=True, index=True)
    portfolio_id = Column(Integer, ForeignKey('portfolios.id'), nullable=False)
    asset_id = Column(Integer, ForeignKey('assets.id'), nullable=False)
    external_id = Column(String(100), nullable=True)  # Broker's order ID
    order_type = Column(String(20), nullable=False)  # "buy", "sell", "stop", "limit"
    status = Column(String(20), nullable=False, default="pending")  # "pending", "filled", "cancelled", "rejected"
    symbol = Column(String(20), nullable=False)
    quantity = Column(Numeric(15, 8), nullable=False)
    price = Column(Numeric(15, 8), nullable=True)
    stop_price = Column(Numeric(15, 8), nullable=True)
    filled_quantity = Column(Numeric(15, 8), nullable=False, default=0.0)
    filled_price = Column(Numeric(15, 8), nullable=True)
    commission = Column(Numeric(15, 8), nullable=False, default=0.0)
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc), 
                       onupdate=lambda: datetime.datetime.now(datetime.timezone.utc), nullable=False)
    executed_at = Column(DateTime, nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"))

    # Relationships
    portfolio = relationship("Portfolio", back_populates="orders")
    asset = relationship("Asset", back_populates="orders")
    user = relationship("User", back_populates="orders")

class Position(Base):
    """Position table - open positions tracking"""
    __tablename__ = 'positions'
    
    id = Column(Integer, primary_key=True, index=True)
    portfolio_id = Column(Integer, ForeignKey('portfolios.id'), nullable=False)
    asset_id = Column(Integer, ForeignKey('assets.id'), nullable=False)
    symbol = Column(String(20), nullable=False)
    side = Column(String(10), nullable=False)  # "long", "short"
    quantity = Column(Numeric(15, 8), nullable=False)
    entry_price = Column(Numeric(15, 8), nullable=False)
    current_price = Column(Numeric(15, 8), nullable=True)
    unrealized_pnl = Column(Numeric(15, 2), nullable=False, default=0.0)
    realized_pnl = Column(Numeric(15, 2), nullable=False, default=0.0)
    commission = Column(Numeric(15, 8), nullable=False, default=0.0)
    is_open = Column(Boolean, default=True, nullable=False)
    opened_at = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc), nullable=False)
    closed_at = Column(DateTime, nullable=True)
    
    # Relationships
    portfolio = relationship("Portfolio", back_populates="positions")
    asset = relationship("Asset", back_populates="positions")

# Database utility functions
def create_tables():
    """Create all database tables"""
    engine = create_engine(DATABASE_URL.replace('+aiosqlite', ''), connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)

def get_db():
    """Get database session (for synchronous parts, like FastAPI dependencies)."""
    engine = create_engine(DATABASE_URL.replace('+aiosqlite', ''), connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    """Initialize the database with tables"""
    create_tables()
    if not _QUIET: print("Database initialized with complete LTS schema")
