"""Plugin manifest and adapter support for agent configuration loading."""

from .adapter import PluginAdapter, PluginConfigError
from .manifest import Plugin, PluginManifest

__all__ = ["Plugin", "PluginAdapter", "PluginConfigError", "PluginManifest"]
