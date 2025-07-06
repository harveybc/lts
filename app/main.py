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

from app.config_handler import load_config
from app.cli import parse_args
from app.config import DEFAULT_VALUES
from app.plugin_loader import load_plugin
from app.config_merger import merge_config, process_unknown_args

def setup_logging(level=logging.INFO):
    """
    Setup logging configuration for the application.

    :param level: Logging level (default: logging.INFO)
    :type level: int
    :return: Configured logger
    :rtype: logging.Logger
    """
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler('app.log')
        ]
    )
    return logging.getLogger(__name__)

def main():
    """
    Orchestrates the execution of the LTS (Live Trading System).
    """
    print("--- Initializing LTS (Live Trading System) ---")

    # 1. Configuration Loading
    args, unknown_args = parse_args()
    cli_args: Dict[str, Any] = vars(args)
    config: Dict[str, Any] = DEFAULT_VALUES.copy()
    file_config: Dict[str, Any] = {}

    if args.load_config:
        try:
            file_config = load_config(args.load_config)
            print(f"Loaded local config from: {args.load_config}")
        except Exception as e:
            print(f"Failed to load local configuration: {e}")
            sys.exit(1)

    # First merge pass (without plugin-specific parameters)
    print("Merging configuration (first pass)...")
    unknown_args_dict = process_unknown_args(unknown_args)
    config = merge_config(config, {}, {}, file_config, cli_args, unknown_args_dict)

    # 2. Plugin Loading
    plugin_types = ['aaa', 'core', 'pipeline', 'strategy', 'broker', 'portfolio']
    plugins = {}

    for plugin_type in plugin_types:
        plugin_name = config.get(f'{plugin_type}_plugin', f'default_{plugin_type}')
        print(f"Loading {plugin_type.capitalize()} Plugin: {plugin_name}")
        try:
            plugin_class, _ = load_plugin(f'plugins_{plugin_type}', plugin_name)
            plugin_instance = plugin_class(config)
            plugin_instance.set_params(**config)
            plugins[plugin_type] = plugin_instance
        except Exception as e:
            print(f"Failed to load or initialize {plugin_type.capitalize()} Plugin '{plugin_name}': {e}")
            sys.exit(1)

    # Second merge pass (with all plugin parameters)
    print("Merging configuration (second pass, with plugin params)...")
    for plugin_type, plugin_instance in plugins.items():
        config = merge_config(config, plugin_instance.plugin_params, {}, file_config, cli_args, unknown_args_dict)

    # 3. Start Pipeline Plugin
    pipeline_plugin = plugins.get('pipeline')
    if not pipeline_plugin:
        print("Fatal: Pipeline plugin not found. Cannot start application.")
        sys.exit(1)
    
    # Pass all loaded plugins to the pipeline
    pipeline_plugin.set_plugins(plugins)
    
    try:
        print("Starting Pipeline Plugin...")
        pipeline_plugin.start()
    except Exception as e:
        print(f"An unexpected error occurred while starting the pipeline plugin: {e}")
        pipeline_plugin.stop()
        sys.exit(1)

if __name__ == "__main__":
    main()
