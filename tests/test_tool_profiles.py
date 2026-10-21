"""Startup tool selection must match registration, diagnostics and compatibility config."""
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from arcgis_mcp.core.config import ConfigError, Settings, load_settings
from arcgis_mcp.core.tool import Services
from arcgis_mcp.tools import CORE_TOOLS, LEGACY_TOOLS, register_all, tool_names


class ToolProfiles(unittest.TestCase):
    def test_default_catalog_is_core_and_legacy_is_opt_in(self):
        settings = Settings(config_path=ROOT / 'config/datasources.json')
        self.assertEqual(len(CORE_TOOLS), 24)
        self.assertEqual(len(LEGACY_TOOLS), 60)
        self.assertEqual(len(set(tool_names())), 24)
        self.assertNotIn('run_arcpy_tool', tool_names(settings))
        self.assertIn('run_saved_workflow', tool_names(settings))
        settings.tool_profile = 'legacy'
        self.assertEqual(len(set(tool_names(settings))), 84)
        self.assertIn('run_arcpy_tool', tool_names(settings))
        self.assertIn('intersect', tool_names(settings))

    def test_registration_matches_selected_catalog(self):
        for profile in ('core', 'legacy'):
            settings = Settings(config_path=ROOT / 'config/datasources.json', tool_profile=profile)
            names = []
            server = types.SimpleNamespace(tool=lambda **kwargs: lambda fn: names.append(kwargs['name']))
            instances = register_all(server, Services(settings, types.SimpleNamespace(), types.SimpleNamespace()))
            self.assertEqual(names, tool_names(settings))
            self.assertEqual(names, [t.name for t in instances])

    def test_config_validation_and_environment_override(self):
        with tempfile.TemporaryDirectory(prefix='.safety-profile-', dir=ROOT) as directory:
            path = Path(directory) / 'config.json'
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop('ARCGIS_MCP_TOOL_PROFILE', None)
                path.write_text('{}')
                self.assertEqual(load_settings(path).tool_profile, 'core')
                path.write_text(json.dumps({'tool_profile': 'legacy'}))
                self.assertEqual(load_settings(path).to_dict()['tool_profile'], 'legacy')
                os.environ['ARCGIS_MCP_TOOL_PROFILE'] = 'core'
                self.assertEqual(load_settings(path).tool_profile, 'core')
                os.environ.pop('ARCGIS_MCP_TOOL_PROFILE')
                for value in (True, None, 'unknown'):
                    path.write_text(json.dumps({'tool_profile': value}))
                    with self.assertRaises(ConfigError):
                        load_settings(path)

    def test_disabled_code_compatibility_requires_explicit_old_flag(self):
        settings = Settings(config_path=ROOT / 'config/datasources.json')
        self.assertNotIn('run_arcpy_code', tool_names(settings))
        settings.allow_arcpy_code = True
        self.assertEqual(len(tool_names(settings)), 25)
        self.assertIn('run_arcpy_code', tool_names(settings))


if __name__ == '__main__':
    unittest.main()
