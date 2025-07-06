#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
plugin_loader.py

Module for loading plugins using the importlib.metadata entry points API updated for Python 3.12.
Provides functions to load a specific plugin and retrieve its parameters.
"""

from importlib.metadata import entry_points, EntryPoint
import importlib
import os
from typing import Any

class PluginLoader:
    """
    Loads plugins from specified directories and enforces required interface and provenance.
    """
    def __init__(self, plugin_dirs=None):
        """
        Initialize the plugin loader.

        :param plugin_dirs: List of plugin directories.
        :type plugin_dirs: list or None
        """
        self.plugin_dirs = plugin_dirs or ["app/plugins_broker", "app/plugins_pipeline", "app/plugins_portfolio", "app/plugins_strategy"]

    def load_plugin(self, name: str) -> Any:
        """
        Load a plugin by name from the configured directories.

        :param name: Name of the plugin to load.
        :type name: str
        :return: Plugin class if found and valid.
        :rtype: Any
        :raises ImportError: If plugin is not found or invalid.
        """
        for d in self.plugin_dirs:
            try:
                module_path = f"{d.replace('/', '.')}.{name}"
                module = importlib.import_module(module_path)
                if hasattr(module, "Plugin"):
                    plugin = getattr(module, "Plugin")
                    # Check for required interface
                    assert hasattr(plugin, "run")
                    assert hasattr(plugin, "plugin_params")
                    assert hasattr(plugin, "set_params")
                    # Version/provenance check (stub)
                    if hasattr(plugin, "__version__"):
                        assert isinstance(plugin.__version__, str)
                    # Simulate signature check (stub)
                    if hasattr(plugin, "signed"):
                        assert plugin.signed is True
                    return plugin
            except (ModuleNotFoundError, AssertionError):
                continue
        raise ImportError(f"Plugin {name} not found or invalid.")

def load_plugin_via_entry_points(plugin_group: str, plugin_name: str):
    """
    Load a plugin class from a specified entry point group using its name.
    
    This function uses the updated importlib.metadata API for Python 3.12 by filtering 
    entry points with the select() method.

    Args:
        plugin_group (str): The entry point group from which to load the plugin.
        plugin_name (str): The name of the plugin to load.

    Returns:
        tuple: A tuple containing the plugin class and a list of required parameter keys 
               extracted from the plugin's plugin_params attribute.

    Raises:
    except Exception as e:
        print(f"Failed to load plugin {plugin_name} from group {plugin_group}, Error: {e}")
        raise

def get_plugin_params(plugin_group: str, plugin_name: str):
    """
    Retrieve the plugin parameters from a specified entry point group using the plugin name.
    
    This function loads the plugin class using the updated importlib.metadata API and returns 
    its plugin_params attribute.

    Args:
        plugin_group (str): The entry point group from which to retrieve the plugin.
        plugin_name (str): The name of the plugin.

    Returns:
        dict: A dictionary representing the plugin parameters (plugin_params).

    Raises:
        ImportError: If the plugin is not found in the specified group.
        ImportError: For any errors encountered while retrieving the plugin parameters.
    """
    print(f"Getting plugin parameters for: {plugin_name} from group: {plugin_group}")
    try:
        # Filter entry points for the specified group using the new .select() method.
        group_entries = entry_points().select(group=plugin_group)
        # Find the entry point that matches the plugin name.
        entry_point = next(ep for ep in group_entries if ep.name == plugin_name)
        # Load the plugin class using the entry point's load method.
        plugin_class = entry_point.load()
        print(f"Retrieved plugin params: {plugin_class.plugin_params}")
        return plugin_class.plugin_params
    except StopIteration:
        print(f"Failed to find plugin {plugin_name} in group {plugin_group}")
        raise ImportError(f"Plugin {plugin_name} not found in group {plugin_group}.")
    except Exception as e:
        print(f"Failed to get plugin params for {plugin_name} from group {plugin_group}, Error: {e}")
        raise ImportError(f"Failed to get plugin params for {plugin_name} from group {plugin_group}, Error: {e}")

def load_plugin(*args, **kwargs):
    """
    Stub for legacy import compatibility. Do not use. Real plugin loading is handled elsewhere.
    :raises NotImplementedError: Always.
    """
    raise NotImplementedError("This is a stub. Use PluginLoader or load_plugin_via_entry_points instead.")
