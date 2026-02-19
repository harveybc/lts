from setuptools import setup, find_packages

setup(
    name='lts',
    version='0.1.0',
    packages=find_packages(),
    entry_points={
        'console_scripts': [
            'lts=app.main:main'
        ],
        # AAA plugins - Authentication, Authorization, Accounting
        'plugins_aaa': [
            'default_aaa=plugins_aaa.default_aaa:DefaultAAA'
        ],
        # Core plugins - Main trading loop and API server
        'plugins_core': [
            'default_core=plugins_core.default_core:DefaultCore'
        ],
        # Pipeline plugins - Orchestrate the trading flow for each instrument
        'plugins_pipeline': [
            'default_pipeline=plugins_pipeline.default_pipeline:DefaultPipeline'
        ],
        # Strategy plugins - Trading decision logic
        'plugins_strategy': [
            'default_strategy=plugins_strategy.default_strategy:DefaultStrategy',
            'prediction_strategy=plugins_strategy.prediction_strategy:PredictionBasedStrategy'
        ],
        # Broker plugins - Connection and execution with brokers
        'plugins_broker': [
            'default_broker=plugins_broker.default_broker:DefaultBroker',
            'backtrader_broker=plugins_broker.backtrader_broker:BacktraderBroker',
            'backtrader_simulation_broker=plugins_broker.backtrader_simulation_broker:BacktraderSimulationBroker',
            'oanda_broker=plugins_broker.oanda_broker:OandaBroker'
        ],
        # Portfolio plugins - Capital allocation management
        'plugins_portfolio': [
            'default_portfolio=plugins_portfolio.default_portfolio:DefaultPortfolio'
        ]
    },
    install_requires=[
        'pandas',
        'numpy',
        'httpx',  # For async HTTP requests to prediction provider
        'requests',
        'websocket-client',
        'fastapi',
        'uvicorn',
        'sqlalchemy',
        'jinja2',
        'python-multipart',
        'passlib',
        'python-jose',
        'backtrader',  # For backtesting and strategy evaluation
        'bcrypt',
        'pydantic',
        'asyncio',
        'schedule',
        'matplotlib',
        'seaborn',
        'backtrader',  # For broker simulation
        'python-dateutil',  # For datetime parsing
        'oandapyV20'  # OANDA v20 REST API client
    ],
    author='LTS Development Team',
    author_email='lts@example.com',
    description=(
        'LTS (Live Trading System) - A secure, modular trading framework with plugin-based architecture '
        'for authentication, authorization, accounting, and all trading components.'
    )
)
