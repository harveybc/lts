from setuptools import setup, find_packages

setup(
    name='lts',
    version='0.1.0',
    packages=find_packages(),
    entry_points={
        'console_scripts': [
            'lts=app.main:main'
        ],
        # Pipeline plugins - Definen el flujo de procesamiento para cada instrumento
        'pipeline.plugins': [
            'default_pipeline=pipeline_plugins.default_pipeline:Plugin',
            'scalping_pipeline=pipeline_plugins.scalping_pipeline:Plugin',
            'swing_pipeline=pipeline_plugins.swing_pipeline:Plugin',
            'momentum_pipeline=pipeline_plugins.momentum_pipeline:Plugin'
        ],
        # Strategy plugins - Implementan lógica de toma de decisiones de trading
        'strategy.plugins': [
            'default_strategy=strategy_plugins.default_strategy:Plugin',
            'breakout_strategy=strategy_plugins.breakout_strategy:Plugin',
            'mean_reversion_strategy=strategy_plugins.mean_reversion_strategy:Plugin',
            'trend_following_strategy=strategy_plugins.trend_following_strategy:Plugin',
            'grid_strategy=strategy_plugins.grid_strategy:Plugin'
        ],
        # Broker API plugins - Manejan conexión y ejecución con brokers
        'broker_api.plugins': [
            'default_broker=broker_api_plugins.default_broker:Plugin',
            'oanda_broker=broker_api_plugins.oanda_broker:Plugin',
            'binance_broker=broker_api_plugins.binance_broker:Plugin',
            'mt5_broker=broker_api_plugins.mt5_broker:Plugin',
            'simulation_broker=broker_api_plugins.simulation_broker:Plugin'
        ],
        # Portfolio Manager plugins - Administran asignación de capital
        'portfolio_manager.plugins': [
            'default_portfolio=portfolio_manager_plugins.default_portfolio:Plugin',
            'equal_weight_portfolio=portfolio_manager_plugins.equal_weight_portfolio:Plugin',
            'variance_minimization_portfolio=portfolio_manager_plugins.variance_minimization_portfolio:Plugin',
            'risk_parity_portfolio=portfolio_manager_plugins.risk_parity_portfolio:Plugin',
            'kelly_criterion_portfolio=portfolio_manager_plugins.kelly_criterion_portfolio:Plugin'
        ]
    },
    install_requires=[
        'pandas',
        'numpy',
        'requests',
        'websocket-client',
        'oandapyV20',
        'python-binance',
        'MetaTrader5',
        'scipy',
        'scikit-learn',
        'pyyaml',
        'schedule'
    ],
    author='Harvey Bastidas',
    author_email='your.email@example.com',
    description=(
        'A Live Trading System (LTS) that supports dynamic loading of plugins for trading pipelines, '
        'strategies, broker APIs, and portfolio management with real-time execution capabilities.'
    )
)
