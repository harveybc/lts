from setuptools import setup, find_packages

setup(
    name='lts',
    version='0.1.0',
    packages=find_packages(),
    entry_points={
        'console_scripts': [
            'lts=app.main:main',
            'lts-oanda-practice=app.oanda_practice_cli:main',
            'lts-alpaca-paper=app.alpaca_paper_cli:main',
            'lts-ibkr-paper=app.ibkr_paper_cli:main'
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
            'prediction_strategy=plugins_strategy.prediction_strategy:PredictionBasedStrategy',
            'eurusd_mr_strategy=plugins_strategy.eurusd_mr_strategy:EurUsdMrStrategy',
            'usdjpy_tsmom_strategy=plugins_strategy.usdjpy_tsmom_strategy:UsdJpyTsmomStrategy',
            'usdjpy_dual_momentum_strategy=plugins_strategy.usdjpy_dual_momentum_strategy:UsdJpyDualMomentumStrategy'
        ],
        # Broker plugins - Connection and execution with brokers
        'plugins_broker': [
            'default_broker=plugins_broker.default_broker:DefaultBroker',
            'backtrader_broker=plugins_broker.backtrader_broker:BacktraderBroker',
            'backtrader_simulation_broker=plugins_broker.backtrader_simulation_broker:BacktraderSimulationBroker',
            'oanda_broker=plugins_broker.oanda_broker:OandaBroker',
            'alpaca_paper_broker=plugins_broker.alpaca_paper_broker:AlpacaPaperBroker',
            'ibkr_paper_broker=plugins_broker.ibkr_paper_broker:IbkrPaperBroker'
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
        'python-dateutil',  # For datetime parsing
        'oandapyV20',  # OANDA v20 REST API client
        'ib_async==2.1.0'  # Current maintained TWS API client wrapper
    ],
    author='LTS Development Team',
    author_email='lts@example.com',
    description=(
        'LTS (Live Trading System) - A secure, modular trading framework with plugin-based architecture '
        'for authentication, authorization, accounting, and all trading components.'
    )
)
