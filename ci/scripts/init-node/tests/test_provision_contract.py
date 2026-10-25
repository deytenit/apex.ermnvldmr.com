import copy
import unittest

from engine.provisioning.contract import ContractError, normalize_user_data, parse_bootstrap_env


def example():
    return {
        'preserve_hostname': False,
        'hostname': 'metis.a1.example.com',
        'users': [{
            'name': 'adam',
            'sudo': 'ALL=(ALL) NOPASSWD:ALL',
            'groups': ['sudo', 'docker'],
            'shell': '/bin/bash',
            'ssh_authorized_keys': ['ssh-ed25519 PUBLIC_TEST_KEY operator'],
        }],
        'write_files': [{
            'path': '/etc/apex/bootstrap.env',
            'owner': 'root:root',
            'permissions': '0600',
            'content': 'APEX_REPO_NAME="metis.example.com"\n'
                       'APEX_REPO_URL="https://example.com/team/metis.git"\n',
        }],
        'runcmd': ['/usr/local/bin/apex-bootstrap'],
    }


def add_env(doc, text):
    doc['write_files'][0]['content'] += text
    return doc


class TestBootstrapAssignments(unittest.TestCase):
    def test_literal_quotes_comments_and_empty_values(self):
        values = parse_bootstrap_env(
            '# enrollment\nAPEX_REPO_NAME=metis.example.com\n'
            "APEX_REPO_REF='release-1'\nAPEX_TIER1_DEVICE=\n")
        self.assertEqual(values['APEX_REPO_REF'], 'release-1')
        self.assertEqual(values['APEX_TIER1_DEVICE'], '')

    def test_rejects_shell_forms_without_repeating_input(self):
        payloads = [
            'APEX_REPO_NAME=$(touch /tmp/SECRET_SENTINEL)',
            'APEX_REPO_NAME="${SECRET_SENTINEL}"',
            'APEX_REPO_NAME=`SECRET_SENTINEL`',
            'APEX_REPO_NAME=x;SECRET_SENTINEL',
            'export APEX_REPO_NAME=SECRET_SENTINEL',
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ContractError) as caught:
                    parse_bootstrap_env(payload)
                self.assertNotIn('SECRET_SENTINEL', str(caught.exception))

    def test_rejects_duplicate_and_unknown_assignments(self):
        for text in ('APEX_REPO_NAME=a\nAPEX_REPO_NAME=b\n', 'OTHER_SECRET=value\n'):
            with self.subTest(text=text):
                with self.assertRaises(ContractError):
                    parse_bootstrap_env(text)

    def test_rejects_unquoted_parentheses_and_inline_comments(self):
        for value in ('(main)', 'main(', 'main)', ' #comment', 'main #comment'):
            with self.subTest(value=value):
                with self.assertRaises(ContractError):
                    parse_bootstrap_env('APEX_REPO_REF=' + value)

    def test_parentheses_and_hashes_remain_quoted_literals(self):
        for quote in ('"', "'"):
            for value in ('(main)', '#comment', 'main #comment'):
                with self.subTest(quote=quote, value=value):
                    values = parse_bootstrap_env('APEX_REPO_REF=' + quote + value + quote)
                    self.assertEqual(values['APEX_REPO_REF'], value)


class TestUserDataContract(unittest.TestCase):
    def test_public_repository_and_directory_defaults(self):
        spec = normalize_user_data(example())
        self.assertEqual(spec.hostname, 'metis.a1.example.com')
        self.assertEqual(spec.repository.name, 'metis.example.com')
        self.assertIsNone(spec.repository.ref)
        self.assertEqual([tier.mode for tier in spec.tiers], ['directory'] * 3)
        self.assertEqual(spec.tiers[0].number, 1)

    def test_ref_and_explicit_storage_intent(self):
        doc = add_env(example(),
            'APEX_REPO_REF="refs/tags/v1"\n'
            'APEX_TIER1_MODE=create\nAPEX_TIER1_DEVICE=/dev/disk/by-id/disk-a\n'
            'APEX_TIER2_MODE=import\nAPEX_TIER2_DEVICE=/dev/disk/by-id/disk-b\n'
            'APEX_TIER2_POOL_GUID=1234567\n')
        spec = normalize_user_data(doc)
        self.assertEqual(spec.repository.ref, 'refs/tags/v1')
        self.assertEqual(spec.tiers[0].mode, 'create')
        self.assertEqual(spec.tiers[1].pool_guid, '1234567')

    def test_legacy_device_requires_explicit_mode(self):
        with self.assertRaisesRegex(ContractError, 'explicit mode'):
            normalize_user_data(add_env(example(), 'APEX_TIER1_DEVICE=/dev/vdb\n'))

    def test_storage_conflicts_are_rejected(self):
        cases = [
            'APEX_TIER1_MODE=create\n',
            'APEX_TIER1_MODE=import\nAPEX_TIER1_DEVICE=/dev/vdb\n',
            'APEX_TIER1_MODE=directory\nAPEX_TIER1_DEVICE=/dev/vdb\n',
            'APEX_TIER1_MODE=create\nAPEX_TIER1_DEVICE=/dev/vdb\nAPEX_TIER1_POOL_GUID=1\n',
            'APEX_TIER1_MODE=erase\n',
            'APEX_TIER1_MODE=create\nAPEX_TIER1_DEVICE=/dev/vdb\n'
            'APEX_TIER2_MODE=create\nAPEX_TIER2_DEVICE=/dev/vdb\n',
        ]
        for assignment in cases:
            with self.subTest(assignment=assignment):
                with self.assertRaises(ContractError):
                    normalize_user_data(add_env(example(), assignment))

    def test_arbitrary_cloud_init_operations_are_rejected(self):
        for field in ('packages', 'bootcmd', 'disk_setup'):
            doc = example()
            doc[field] = []
            with self.subTest(field=field):
                with self.assertRaises(ContractError):
                    normalize_user_data(doc)
        doc = example()
        doc['runcmd'] = ['touch /tmp/unapproved']
        with self.assertRaises(ContractError):
            normalize_user_data(doc)

    def test_standard_list_form_of_bootstrap_command(self):
        doc = example()
        doc['runcmd'] = [['/usr/local/bin/apex-bootstrap']]
        self.assertEqual(normalize_user_data(doc).hostname, doc['hostname'])

    def test_invalid_administrator_and_missing_keys(self):
        changes = [
            ('name', 'root'), ('shell', '/bin/sh'), ('groups', ['sudo']),
            ('sudo', 'ALL=(ALL) ALL'), ('ssh_authorized_keys', []),
        ]
        for key, value in changes:
            doc = example()
            doc['users'][0][key] = value
            with self.subTest(key=key):
                with self.assertRaises(ContractError):
                    normalize_user_data(doc)

    def test_unapproved_or_duplicate_file_writes(self):
        for mutate in ('path', 'owner', 'permissions', 'append', 'duplicate'):
            doc = example()
            item = doc['write_files'][0]
            if mutate == 'path':
                item['path'] = '/etc/shadow'
            elif mutate == 'owner':
                item['owner'] = 'adam:adam'
            elif mutate == 'permissions':
                item['permissions'] = '0644'
            elif mutate == 'append':
                item['append'] = True
            else:
                doc['write_files'].append(copy.deepcopy(item))
            with self.subTest(mutate=mutate):
                with self.assertRaises(ContractError):
                    normalize_user_data(doc)

    def test_rejects_wrong_types_and_missing_required_fields(self):
        cases = [None, [], {'hostname': 'x.example.com'}]
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ContractError):
                    normalize_user_data(value)
        doc = example()
        doc['preserve_hostname'] = 'false'
        with self.assertRaises(ContractError):
            normalize_user_data(doc)

    def test_repository_paths_and_embedded_passwords(self):
        for name, url in [
            ('../escape', 'https://example.com/repo.git'),
            ('metis', 'https://user:SECRET_SENTINEL@example.com/repo.git'),
            ('metis', 'file:///etc/shadow'),
            ('metis', '-uSECRET_SENTINEL'),
        ]:
            doc = example()
            doc['write_files'][0]['content'] = (
                f'APEX_REPO_NAME="{name}"\nAPEX_REPO_URL="{url}"\n')
            with self.subTest(name=name, url=url):
                with self.assertRaises(ContractError) as caught:
                    normalize_user_data(doc)
                self.assertNotIn('SECRET_SENTINEL', str(caught.exception))

    def test_runtime_secrets_are_excluded_from_repr_and_identity(self):
        doc = example()
        for path, content, permission in [
            ('/home/adam/.ssh/id_ed25519', 'SECRET_SENTINEL', '0600'),
            ('/home/adam/.ssh/known_hosts', 'HOST_TRUST_SENTINEL', '0644'),
        ]:
            doc['write_files'].append({
                'path': path, 'owner': 'adam:adam', 'permissions': permission,
                'defer': True, 'content': content,
            })
        spec = normalize_user_data(doc)
        text = repr(spec) + repr(spec.identity())
        self.assertNotIn('SECRET_SENTINEL', text)
        self.assertNotIn('PUBLIC_TEST_KEY', text)
        self.assertNotIn('HOST_TRUST_SENTINEL', text)
        self.assertEqual(spec.files[1].content, 'SECRET_SENTINEL')

    def test_administrator_files_require_defer(self):
        doc = example()
        doc['write_files'].append({
            'path': '/home/adam/.ssh/known_hosts', 'owner': 'adam:adam',
            'permissions': '0644', 'content': 'example.com ssh-ed25519 VALUE',
        })
        with self.assertRaises(ContractError):
            normalize_user_data(doc)

    def test_contract_does_not_mutate_caller_input(self):
        doc = example()
        before = copy.deepcopy(doc)
        normalize_user_data(doc)
        self.assertEqual(doc, before)

    def test_identity_changes_for_target_but_not_credentials(self):
        doc = example()
        before = normalize_user_data(doc).identity()
        doc['users'][0]['ssh_authorized_keys'] = ['ssh-ed25519 ANOTHER_PUBLIC_KEY operator']
        self.assertEqual(normalize_user_data(doc).identity(), before)
        doc['hostname'] = 'other.a1.example.com'
        self.assertNotEqual(normalize_user_data(doc).identity(), before)


if __name__ == '__main__':
    unittest.main()
