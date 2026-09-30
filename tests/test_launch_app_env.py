import os
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
import agent

class TestLaunchAppEnvExpansion(unittest.TestCase):
    def setUp(self):
        self.agent = agent.PCControlAgent()

    def test_launch_app_expands_env_vars(self):
        userprofile = os.environ.get('USERPROFILE', 'C:\\Users\\Default')
        test_app_path = '%USERPROFILE%\\test_app_dummy_12345.exe'
        expected_expanded = os.path.expandvars(test_app_path)
        self.assertNotIn('%USERPROFILE%', expected_expanded)
        self.assertTrue(expected_expanded.startswith(userprofile))

        self.agent.config['apps'] = {
            'test_app': {
                'steps': [
                    {
                        'id': 'test_step',
                        'label': 'Test Step',
                        'command': [test_app_path],
                        'required': True,
                        'verification': {'type': 'process_name', 'targets': ['test_app_dummy_12345.exe']}
                    }
                ]
            }
        }

        with self.assertRaises(FileNotFoundError) as ctx:
            self.agent.launch_app('test_app')
        self.assertIn(userprofile, str(ctx.exception))
        self.assertNotIn('%USERPROFILE%', str(ctx.exception))

if __name__ == '__main__':
    unittest.main()
