import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, unquote, urlsplit


ROOT = pathlib.Path(__file__).resolve().parents[1]
GENERATOR = ROOT / 'assets/happ-subscription-generator/generator.py'


class TestHappSubscriptions(unittest.TestCase):
    def generate(self, scheme, params, country='NL'):
        data = {
            'routing': {'name': 'test-routing'},
            'templates': {'edge': {
                'country': country, 'nodes': ['metis'], 'scheme': scheme,
                'host': 'edge.example.com', 'port': 443, 'params': params,
            }},
            'users': [{'name': 'test-user', 'psub': 'test.psub', 'configs': [
                {'template': 'edge', 'id': 'test-credential', 'params': {'sni': 'tls.example.com'}},
            ]}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            source = pathlib.Path(tmp) / 'input.json'
            source.write_text(json.dumps(data))
            result = subprocess.run(
                [sys.executable, str(GENERATOR), str(source), tmp],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = (pathlib.Path(tmp) / 'test.psub').read_text().splitlines()
        self.assertEqual(len(lines), 2)
        return urlsplit(lines[1])

    def test_hysteria_names_follow_protocol_without_changing_link_parameters(self):
        for scheme in ('hysteria2', 'hy2'):
            for params in ({}, {'type': 'tcp'}):
                with self.subTest(scheme=scheme, params=params):
                    link = self.generate(scheme, params)
                    self.assertEqual(unquote(link.fragment),
                                     '🇳🇱 Netherlands [hysteria] | metis | test-user')
                    self.assertEqual(link.scheme, scheme)
                    self.assertEqual(link.netloc, 'test-credential@edge.example.com:443')
                    self.assertEqual(parse_qs(link.query),
                                     {**{k: [v] for k, v in params.items()}, 'sni': ['tls.example.com']})

    def test_vless_keeps_transport_label_and_user_overrides(self):
        link = self.generate('vless', {'type': 'xhttp', 'sni': 'old.example.com'})
        self.assertEqual(unquote(link.fragment),
                         '🇳🇱 Netherlands [xhttp] | metis | test-user')
        self.assertEqual(parse_qs(link.query), {'type': ['xhttp'], 'sni': ['tls.example.com']})

    def test_existing_country_and_tcp_label_are_preserved(self):
        link = self.generate('vless', {'type': 'tcp'}, country='LV')
        self.assertEqual(unquote(link.fragment), '🇱🇻 Latvia [tcp] | metis | test-user')


if __name__ == '__main__':
    unittest.main()
