#!/usr/bin/env python3
"""
main.py

Entry point for the LTS (Live Trading System) application. This script orchestrates:
- Loading and merging configurations (CLI, files, remote).
- Initializing all plugins: AAA, Core, Pipeline, Strategy, Broker, and Portfolio.
- Starting the pipeline plugin to launch the trading system.

The LTS is a secure, modular trading framework with plugin-based architecture for
authentication, authorization, accounting, and all trading components.

:author: LTS Development Team
:copyright: (c) 2025 LTS Project
:license: MIT
"""

import sys
from typing import Any, Dict
import logging
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# ---------------------------------------------------------------------------
# Quiet mode: suppress verbose output when LTS_QUIET=1
# Only allows ERROR/WARN/final-metric lines through.
# ---------------------------------------------------------------------------
import builtins as _builtins
_original_print = _builtins.print

def _quiet_print(*args, **kwargs):
    """Filtered print that only passes through important messages."""
    if args:
        msg = str(args[0])
        _pass = any(kw in msg.upper() for kw in [
            'ERROR', 'WARN', 'EXCEPTION', 'TRACEBACK', 'FATAL',
            'FINAL', 'BEST VAL', 'TEST MAE', 'VAL MAE', 'RESULT',
            'IMPROVEMENT', 'VERDICT', 'SUMMARY',
        ])
        if _pass:
            _original_print(*args, **kwargs)
        return
    _original_print(*args, **kwargs)

if os.environ.get('LTS_QUIET', '0') == '1':
    _builtins.print = _quiet_print
    # Also suppress verbose logging
    logging.basicConfig(level=logging.WARNING)

from app.config_handler import load_config
from app.cli import parse_args
from app.config import DEFAULT_VALUES
from app.plugin_loader import load_plugin
from app.config_merger import merge_config, process_unknown_args

logger = logging.getLogger(__name__)

def setup_logging(config: Dict[str, Any]):
    """
    Setup logging configuration based on config options.

    Priority: LTS_QUIET=1 env var > quiet_mode config > log_level config.
    Defaults to WARNING if none specified.

    :param config: Application configuration dictionary
    :type config: Dict[str, Any]
    :return: Configured logger
    :rtype: logging.Logger
    """
    # Determine effective log level
    if os.environ.get('LTS_QUIET', '0') == '1' or config.get('quiet_mode', False):
        level = logging.ERROR
    else:
        level_name = config.get('log_level', 'WARNING').upper()
        level = getattr(logging, level_name, logging.WARNING)

    # Reset root logger handlers to avoid duplicate basicConfig calls
    root = logging.getLogger()
    root.handlers.clear()
    
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler('app.log')
        ],
        force=True,
    )
    return logging.getLogger(__name__)

def main():
    """
    Orchestrates the execution of the LTS (Live Trading System).
    """
    logger.info("--- Initializing LTS (Live Trading System) ---")

    # 1. Configuration Loading
    args, unknown_args = parse_args()
    cli_args: Dict[str, Any] = vars(args)
    config: Dict[str, Any] = DEFAULT_VALUES.copy()
    file_config: Dict[str, Any] = {}

    if args.load_config:
        try:
            file_config = load_config(args.load_config)
            logger.info("Loaded local config from: %s", args.load_config)
        except Exception as e:
            logger.error("Failed to load local configuration: %s", e)
            sys.exit(1)

    # First merge pass (without plugin-specific parameters)
    logger.info("Merging configuration (first pass)...")
    unknown_args_dict = process_unknown_args(unknown_args)
    config = merge_config(config, {}, {}, file_config, cli_args, unknown_args_dict)

    # Setup logging based on merged config
    setup_logging(config)

    # 2. Plugin Loading
    plugin_types = ['aaa', 'core', 'pipeline', 'strategy', 'broker', 'portfolio']
    plugins = {}

    for plugin_type in plugin_types:
        plugin_name = config.get(f'{plugin_type}_plugin', f'default_{plugin_type}')
        logger.info("Loading %s Plugin: %s", plugin_type.capitalize(), plugin_name)
        try:
            plugin_class, _ = load_plugin(f'plugins_{plugin_type}', plugin_name)
            plugin_instance = plugin_class(config)
            plugin_instance.set_params(**config)
            plugins[plugin_type] = plugin_instance
        except Exception as e:
            logger.error("Failed to load or initialize %s Plugin '%s': %s", plugin_type.capitalize(), plugin_name, e)
            sys.exit(1)

    # Second merge pass (with all plugin parameters)
    logger.info("Merging configuration (second pass, with plugin params)...")
    for plugin_type, plugin_instance in plugins.items():
        config = merge_config(config, plugin_instance.plugin_params, {}, file_config, cli_args, unknown_args_dict)

    # 3. Start Pipeline Plugin
    pipeline_plugin = plugins.get('pipeline')
    if not pipeline_plugin:
        logger.error("Fatal: Pipeline plugin not found. Cannot start application.")
        sys.exit(1)
    
    # Pass all loaded plugins to the pipeline
    pipeline_plugin.set_plugins(plugins)
    
    try:
        logger.info("Starting Pipeline Plugin...")
        pipeline_plugin.start()
    except Exception as e:
        logger.error("An unexpected error occurred while starting the pipeline plugin: %s", e)
        pipeline_plugin.stop()
        sys.exit(1)

if __name__ == "__main__":
    main()
