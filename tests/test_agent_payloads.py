import json
import unittest

import boot_agent
import agent

class TestPayloadStructures(unittest.TestCase):
    def test_boot_agent_payload(self):
        ag = boot_agent.SystemAgent()
        payload = ag.status_payload()
        
        self.assertIn('ts', payload)
        self.assertIn('boot_id', payload)
        self.assertIn('version', payload)
        self.assertEqual(payload['version'], '2.0')
        self.assertTrue(payload['boot_id'].startswith('sys-'))
        self.assertIn('state', payload)
        self.assertEqual(payload['state'], 'online')

    def test_desktop_agent_payload(self):
        ag = agent.PCControlAgent()
        
        captured_payloads = []
        # Mock mqtt.publish to intercept the payload
        ag.mqtt.publish = lambda topic, payload, **kwargs: captured_payloads.append(json.loads(payload))
        ag.mqtt_online = True
        ag.publish_status(force=True)
        
        self.assertEqual(len(captured_payloads), 1)
        payload = captured_payloads[0]
        
        self.assertIn('ts', payload)
        self.assertIn('boot_id', payload)
        self.assertIn('version', payload)
        self.assertEqual(payload['version'], '2.0')
        self.assertTrue(payload['boot_id'].startswith('desktop-'))
        self.assertIn('state', payload)
        self.assertEqual(payload['state'], 'online')

if __name__ == '__main__':
    unittest.main()
