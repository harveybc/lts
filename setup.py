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
            'default_pipeline=plugins_pipeline.default_pipeline:Plugin'
        ],
        # Strategy plugins - Implementan lógica de toma de decisiones de trading
        'strategy.plugins': [
            'default_strategy=plugins_strategy.default_strategy:Plugin'
        ],
        # Broker API plugins - Manejan conexión y ejecución con brokers
        'broker.plugins': [
            'default_broker=plugins_broker.default_broker:Plugin'
        ],
        # Portfolio Manager plugins - Administran asignación de capital
        'portfolio.plugins': [
            'default_portfolio=plugins_portfolio.default_portfolio:Plugin'
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
