import importlib.util
import unittest

from engine.provisioning.contract import ContractError
from engine.provisioning.userdata import decode_user_data


@unittest.skipUnless(importlib.util.find_spec('yaml'), 'target-only python3-yaml is unavailable')
class UserDataTests(unittest.TestCase):
    def test_duplicate_mapping_keys_rejected_before_normalization(self):
        with self.assertRaisesRegex(ContractError, 'unique'):
            decode_user_data(b'hostname: one.example.com\nhostname: two.example.com\n')

    def test_alias_and_unsafe_yaml_do_not_execute_or_echo_content(self):
        for content in (b'a: &a [*a]', b'!!python/object/apply:os.system [SECRET_SENTINEL]',
                        b'a: [SECRET_SENTINEL'):
            with self.subTest(content=content):
                with self.assertRaises(ContractError) as error:
                    decode_user_data(content)
                self.assertNotIn('SECRET_SENTINEL', str(error.exception))

    def test_normal_supported_document(self):
        import yaml
        from tests.test_provision_contract import example
        spec = decode_user_data(yaml.safe_dump(example()).encode())
        self.assertEqual(spec.hostname, 'metis.a1.example.com')

    def test_yaml_boolean_mapping_key_rejected(self):
        with self.assertRaisesRegex(ContractError, 'unique strings'):
            decode_user_data(b'true: value\n')
