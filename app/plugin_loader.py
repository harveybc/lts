#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
plugin_loader.py

Module for loading plugins using the importlib.metadata entry points API updated for Python 3.12.
Provides functions to load a specific plugin and retrieve its parameters.
"""

import os as _os
_QUIET = _os.environ.get('LTS_QUIET', '0') == '1'

from importlib.metadata import entry_points, EntryPoint

def load_plugin(plugin_group: str, plugin_name: str):
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
        ImportError: If the plugin is not found in the specified group.
        Exception: For any other errors during the plugin loading process.
    """
    if not _QUIET: print(f"Attempting to load plugin: {plugin_name} from group: {plugin_group}")
    try:
        # Filter entry points for the specified group using the new .select() method.
        group_entries = entry_points().select(group=plugin_group)
        # Find the entry point that matches the plugin name.
        entry_point = next(ep for ep in group_entries if ep.name == plugin_name)
        # Load the plugin class using the entry point's load method.
        plugin_class = entry_point.load()
        # Extract the keys from the plugin's plugin_params attribute as required parameters.
        required_params = list(plugin_class.plugin_params.keys())
        if not _QUIET: print(f"Successfully loaded plugin: {plugin_name} with params: {plugin_class.plugin_params}")
        return plugin_class, required_params
    except StopIteration:
        if not _QUIET: print(f"Failed to find plugin {plugin_name} in group {plugin_group}")
        raise ImportError(f"Plugin {plugin_name} not found in group {plugin_group}.")
    except Exception as e:
        if not _QUIET: print(f"Failed to load plugin {plugin_name} from group {plugin_group}, Error: {e}")
        raise

def get_plugin_params(plugin_group: str, plugin_name: str):
    """
    Get the required parameters for a specific plugin without loading it.

    Args:
        plugin_group (str): The entry point group of the plugin.
        plugin_name (str): The name of the plugin.

    Returns:
        list: A list of required parameter keys for the plugin.
    
    Raises:
        ImportError: If the plugin is not found.
    """
    try:
        group_entries = entry_points().select(group=plugin_group)
        entry_point = next(ep for ep in group_entries if ep.name == plugin_name)
        # Access the plugin's parameters directly from the loaded entry point object
        # This assumes the plugin class has a `plugin_params` attribute.
        plugin_class = entry_point.load()
        return list(plugin_class.plugin_params.keys())
    except (StopIteration, AttributeError):
        raise ImportError(f"Could not get params for plugin {plugin_name} in group {plugin_group}.")

def get_available_plugins(plugin_group: str = None):
    """
    List all available plugins, optionally filtered by a specific group.

    Args:
        plugin_group (str, optional): The entry point group to filter by. Defaults to None.

    Returns:
        dict: A dictionary where keys are group names and values are lists of plugin names.
    """
    all_plugins = {}
    discovered_entry_points = entry_points()

    if plugin_group:
        # If a group is specified, only process that group
        entries = discovered_entry_points.select(group=plugin_group)
        all_plugins[plugin_group] = [entry.name for entry in entries]
    else:
        # Otherwise, process all groups
        for entry in discovered_entry_points:
            if entry.group not in all_plugins:
                all_plugins[entry.group] = []
            all_plugins[entry.group].append(entry.name)
            
    return all_plugins
