import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from engine.provisioning.contract import Enrollment, Repository, Tier
from engine.provisioning import storage
from engine.provisioning.storage import StorageError, _persist_swap, reconcile_storage


def enrollment(*tiers):
    return Enrollment('node.example.com', Repository('node', 'https://example.com/node', None),
                      tuple(tiers) or (Tier(1, 'directory', None, None),), (), ())


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.srv = self.root / 'srv'
        self.srv.mkdir()
        self.swap = self.root / 'swapfile'
        self.fstab = self.root / 'fstab'
        self.fstab.write_text('# existing entries\n')
        self.meminfo = self.root / 'meminfo'
        self.meminfo.write_text('MemTotal:       8388608 kB\n')
        self.swaps = self.root / 'swaps'
        self.calls = []
        self.state = {}
        self.saved = []
        self.mounts = [{'target': '/', 'source': '/dev/root', 'fstype': 'ext4', 'maj:min': '8:0'}]
        self.devices = [self.device('/dev/root', '8:0', 'ROOT', ['/']),
                        self.device('/dev/spare', '8:16', 'SPARE', [])]
        self.signatures = []
        self.pool = None
        self.pool_member = '/dev/spare'
        self.pool_mountpoint = self.srv / 'tier-1.node'
        self.pool_owner = None
        self.import_listing = ''
        self.import_full_path_listing = None
        self.set_active_swaps(['/dev/existing-swap'])
        self.activate_swap = True
        self.create_hook = None
        for name, value in [('SRV', self.srv), ('SWAPFILE', self.swap),
                            ('FSTAB', self.fstab), ('MEMINFO', self.meminfo), ('SWAPS', self.swaps)]:
            patcher = patch('engine.provisioning.storage.' + name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch('engine.provisioning.storage.subprocess.run', side_effect=self.run_command)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch('engine.provisioning.storage._device_number', side_effect=self.device_number)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def device(name, number, serial, mounts):
        return {'name': name, 'maj:min': number, 'type': 'disk', 'pkname': None,
                'mountpoints': mounts, 'fstype': None, 'wwn': None,
                'serial': serial, 'size': 1024 ** 4}

    def device_number(self, path):
        aliases = {'/dev/alias': '/dev/spare'}
        path = aliases.get(str(path), str(path))
        for item in self.devices:
            if item['name'] == path:
                return item['maj:min']
        raise StorageError('selected device is missing or is not a block device')

    def save(self):
        self.saved.append(copy.deepcopy(self.state))

    def set_active_swaps(self, paths):
        rows = ['Filename\tType\tSize\tUsed\tPriority']
        for path in paths:
            escaped = path.replace('\\', r'\134').replace(' ', r'\040').replace('\t', r'\011').replace('\n', r'\012')
            rows.append(f'{escaped}\tfile\t2097148\t0\t-2')
        self.swaps.write_text('\n'.join(rows) + '\n')

    def run_command(self, argv, **kwargs):
        self.calls.append(argv)
        output = ''
        if argv[0] == 'findmnt':
            output = json.dumps({'filesystems': self.mounts})
        elif argv[0] == 'lsblk':
            output = json.dumps({'blockdevices': self.devices})
        elif argv[0] == 'wipefs':
            output = json.dumps({'signatures': self.signatures})
        elif argv[:2] == ['zpool', 'list']:
            if self.pool:
                output = f"tier-1\t{self.pool['guid']}\tONLINE\n"
        elif argv[:2] == ['zpool', 'status']:
            output = ('  pool: tier-1\n state: ONLINE\nconfig:\n\n'
                      '\tNAME STATE READ WRITE CKSUM\n'
                      '\ttier-1 ONLINE 0 0 0\n'
                      f'\t  {self.pool_member} ONLINE 0 0 0\n\nerrors: No known data errors\n')
        elif argv[:2] == ['zpool', 'create']:
            self.assertTrue(self.saved)
            self.assertEqual(self.saved[-1]['storage']['tiers']['1']['status'], 'intent')
            if self.create_hook:
                self.create_hook()
            self.pool = {'guid': '12345'}
        elif argv[:2] == ['zpool', 'import']:
            if '-N' in argv:
                self.pool = {'guid': argv[-1]}
            else:
                output = self.import_listing
                if self.import_full_path_listing is not None and kwargs['env'].get('ZPOOL_VDEV_NAME_PATH') == '1' \
                        and kwargs['env'].get('ZPOOL_VDEV_NAME_FOLLOW_LINKS') == '1' \
                        and 'ZPOOL_VDEV_NAME_GUID' not in kwargs['env']:
                    output = self.import_full_path_listing
        elif argv[:2] == ['zfs', 'list']:
            mounted = 'yes' if any(m['source'] == 'tier-1' for m in self.mounts) else 'no'
            output = f'tier-1\t{self.pool_mountpoint}\tfilesystem\t{mounted}\n'
        elif argv[:2] == ['zfs', 'get']:
            output = (self.pool_owner or self.state['storage']['identity']) + '\n'
        elif argv[:2] == ['zfs', 'set']:
            self.pool_owner = argv[2].split('=', 1)[1]
        elif argv[:2] == ['zfs', 'mount']:
            self.mounts.append({'target': str(self.srv / 'tier-1.node'), 'source': 'tier-1',
                                'fstype': 'zfs', 'maj:min': '0:77'})
        elif argv[0] == 'mkswap':
            self.assertTrue(self.swap.exists())
            self.assertEqual(self.saved[-1]['storage']['swap']['status'], 'intent')
        elif argv == ['swapon', str(self.swap)]:
            if self.activate_swap:
                self.set_active_swaps([str(self.swap)])
        else:
            self.fail('Unexpected command: ' + repr(argv))
        return subprocess.CompletedProcess(argv, 0, output, '')

    def test_directory_preserves_contents_and_only_reports_new_directory(self):
        path = self.srv / 'tier-1.node'
        path.mkdir()
        (path / 'keep').write_text('existing data')
        result = reconcile_storage(enrollment(Tier(1, 'directory', None, None),
                                              Tier(2, 'directory', None, None)), self.state, self.save)
        self.assertEqual((path / 'keep').read_text(), 'existing data')
        self.assertFalse(result['tiers'][0]['created'])
        self.assertTrue(result['tiers'][1]['created'])
        self.assertEqual(result['swap']['status'], 'skipped')

    def test_new_directory_mode_is_exact_under_restrictive_umask(self):
        self.srv.chmod(0o710)
        previous_umask = os.umask(0o077)
        try:
            reconcile_storage(enrollment(), self.state, self.save)
        finally:
            os.umask(previous_umask)
        self.assertEqual((self.srv / 'tier-1.node').stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.srv.stat().st_mode & 0o777, 0o710)

    def test_preexisting_directory_keeps_restrictive_mode(self):
        path = self.srv / 'tier-1.node'
        path.mkdir()
        path.chmod(0o700)
        result = reconcile_storage(enrollment(), self.state, self.save)
        self.assertFalse(result['tiers'][0]['created'])
        self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_directory_symlink_and_unexpected_mount_are_rejected(self):
        path = self.srv / 'tier-1.node'
        path.symlink_to(self.root)
        with self.assertRaises(StorageError):
            reconcile_storage(enrollment(), self.state, self.save)
        path.unlink()
        path.mkdir()
        self.mounts.append({'target': str(path), 'source': '/dev/other', 'fstype': 'ext4', 'maj:min': '8:32'})
        with self.assertRaises(StorageError):
            reconcile_storage(enrollment(), self.state, self.save)

    def test_pending_directory_ownership_survives_later_tier_failure_until_ack(self):
        spec = enrollment(Tier(1, 'directory', None, None), Tier(2, 'directory', None, None))
        first = self.srv / 'tier-1.node'
        second = self.srv / 'tier-2.node'
        second.symlink_to(self.root)
        with self.assertRaises(StorageError):
            reconcile_storage(spec, self.state, self.save)
        self.assertTrue(first.is_dir())
        self.state = copy.deepcopy(self.saved[-1])
        second.unlink()
        second.mkdir()
        result = reconcile_storage(spec, self.state, self.save)
        self.assertEqual(result['ownership_pending'], [str(first)])
        self.assertEqual(reconcile_storage(spec, self.state, self.save)['ownership_pending'], [str(first)])
        storage.acknowledge_storage_ownership(spec, self.state, [str(first)], self.save)
        self.state = copy.deepcopy(self.saved[-1])
        self.assertEqual(reconcile_storage(spec, self.state, self.save)['ownership_pending'], [])

    def test_replaced_pending_directory_is_never_adopted_or_acknowledged(self):
        spec = enrollment()
        path = self.srv / 'tier-1.node'
        reconcile_storage(spec, self.state, self.save)
        path.rename(self.srv / 'original')
        path.mkdir()
        with self.assertRaisesRegex(StorageError, 'identity'):
            reconcile_storage(spec, self.state, self.save)
        with self.assertRaisesRegex(StorageError, 'identity'):
            storage.acknowledge_storage_ownership(spec, self.state, [str(path)], self.save)

    def test_preexisting_directory_cannot_be_acknowledged(self):
        path = self.srv / 'tier-1.node'
        path.mkdir()
        spec = enrollment()
        result = reconcile_storage(spec, self.state, self.save)
        self.assertEqual(result['ownership_pending'], [])
        with self.assertRaises(StorageError):
            storage.acknowledge_storage_ownership(spec, self.state, [str(path)], self.save)

    def test_ownership_acknowledgement_rejects_retargeted_enrollment(self):
        spec = enrollment()
        result = reconcile_storage(spec, self.state, self.save)
        changed = Enrollment('other.example.com', spec.repository, spec.tiers, (), ())
        with self.assertRaisesRegex(StorageError, 'identity'):
            storage.acknowledge_storage_ownership(changed, self.state, result['ownership_pending'], self.save)
        self.assertEqual(reconcile_storage(spec, self.state, self.save)['ownership_pending'],
                         result['ownership_pending'])

    def test_missing_root_and_occupied_device_never_create_pool(self):
        for device in ('/dev/missing', '/dev/root', '/dev/spare'):
            with self.subTest(device=device):
                self.signatures = [{'type': 'ext4'}] if device == '/dev/spare' else []
                with self.assertRaises(StorageError):
                    reconcile_storage(enrollment(Tier(1, 'create', device, None)), {}, lambda: None)
        self.assertFalse(any(c[:2] == ['zpool', 'create'] for c in self.calls))

    def test_aliases_selected_by_two_tiers_are_rejected_before_creation(self):
        with self.assertRaises(StorageError):
            reconcile_storage(enrollment(Tier(1, 'create', '/dev/spare', None),
                                          Tier(2, 'create', '/dev/alias', None)), self.state, self.save)
        self.assertFalse(any(c[:2] == ['zpool', 'create'] for c in self.calls))

    def test_create_records_guid_and_retry_verifies_without_recreation(self):
        spec = enrollment(Tier(1, 'create', '/dev/spare', None))
        reconcile_storage(spec, self.state, self.save)
        self.assertEqual(self.state['storage']['tiers']['1']['guid'], '12345')
        reconcile_storage(spec, self.state, self.save)
        self.assertEqual(sum(c[:2] == ['zpool', 'create'] for c in self.calls), 1)
        self.devices[1]['serial'] = 'REPLACED'
        with self.assertRaises(StorageError):
            reconcile_storage(spec, self.state, self.save)

    def test_existing_pool_name_without_journal_is_not_ownership(self):
        self.pool = {'guid': '12345'}
        with self.assertRaises(StorageError):
            reconcile_storage(enrollment(Tier(1, 'create', '/dev/spare', None)), self.state, self.save)

    def test_import_requires_exact_guid_and_single_device(self):
        self.import_listing = ('   pool: tier-1\n     id: 54321\n  state: ONLINE\n'
                               ' config:\n\n\ttier-1 ONLINE\n\t  /dev/spare ONLINE\n')
        with self.assertRaises(StorageError):
            reconcile_storage(enrollment(Tier(1, 'import', '/dev/spare', '12345')), self.state, self.save)
        self.assertFalse(any(c[:2] == ['zpool', 'import'] and '-N' in c for c in self.calls))

    def test_unknown_swapfile_is_preserved(self):
        self.set_active_swaps([])
        self.swap.write_text('do not overwrite')
        result = reconcile_storage(enrollment(), self.state, self.save)
        self.assertEqual(self.swap.read_text(), 'do not overwrite')
        self.assertEqual(result['swap']['status'], 'skipped')
        self.assertIn('existing', result['swap']['reason'])

    def test_root_disk_descendant_is_protected(self):
        child = self.device('/dev/root1', '8:1', 'ROOT', ['/boot'])
        child.update(type='part', pkname='/dev/root', partuuid='root-partition')
        self.devices.append(child)
        for device in ('/dev/root', '/dev/root1'):
            with self.subTest(device=device):
                with self.assertRaisesRegex(StorageError, 'intersects'):
                    reconcile_storage(enrollment(Tier(1, 'create', device, None)), self.state, self.save)
                self.state.clear()
        self.assertFalse(any(c[:2] == ['zpool', 'create'] for c in self.calls))

    def test_missing_hardware_identity_stops_before_creation(self):
        self.devices[1]['serial'] = None
        with self.assertRaisesRegex(StorageError, 'stable'):
            reconcile_storage(enrollment(Tier(1, 'create', '/dev/spare', None)), self.state, self.save)
        self.assertFalse(any(c[:2] == ['zpool', 'create'] for c in self.calls))

    def test_identity_rechecked_after_intent_is_saved(self):
        def changed_device():
            self.save()
            self.devices[1]['serial'] = 'REPLACED'
        with self.assertRaisesRegex(StorageError, 'changed before'):
            reconcile_storage(enrollment(Tier(1, 'create', '/dev/spare', None)), self.state, changed_device)
        self.assertFalse(any(c[:2] == ['zpool', 'create'] for c in self.calls))

    def test_created_whole_disk_pool_can_use_a_new_partition(self):
        def partition_created():
            child = self.device('/dev/spare1', '8:17', 'SPARE', [])
            child.update(type='part', pkname='/dev/spare')
            self.devices.append(child)
            self.pool_member = '/dev/spare1'
        self.create_hook = partition_created
        result = reconcile_storage(enrollment(Tier(1, 'create', '/dev/spare', None)), self.state, self.save)
        self.assertEqual(result['tiers'][0]['guid'], '12345')

    def importable(self, members='\t  spare ONLINE\n'):
        self.import_listing = ('   pool: tier-1\n     id: 12345\n  state: ONLINE\n'
                               ' action: The pool can be imported using its name or numeric identifier.\n'
                               ' config:\n\n\ttier-1 ONLINE\n' + members)

    def test_imports_declared_guid_without_force_or_mountpoint_rewrite(self):
        self.importable()
        result = reconcile_storage(enrollment(Tier(1, 'import', '/dev/spare', '12345')), self.state, self.save)
        self.assertEqual(result['tiers'][0]['guid'], '12345')
        self.assertFalse(result['tiers'][0]['created'])
        self.assertEqual(result['ownership_pending'], [])
        with self.assertRaises(StorageError):
            storage.acknowledge_storage_ownership(
                enrollment(Tier(1, 'import', '/dev/spare', '12345')), self.state,
                [str(self.pool_mountpoint)], self.save)
        self.assertIn(['zpool', 'import', '-N', '-d', '/dev/spare', '12345'], self.calls)
        self.assertFalse(any('-f' in command for command in self.calls))
        self.assertFalse(any('mountpoint=' in arg for command in self.calls for arg in command))

    def test_multidevice_import_is_rejected(self):
        self.importable('\t  spare ONLINE\n\t  other ONLINE\n')
        with self.assertRaisesRegex(StorageError, 'exactly one'):
            reconcile_storage(enrollment(Tier(1, 'import', '/dev/spare', '12345')), self.state, self.save)
        self.assertFalse(any(c[:2] == ['zpool', 'import'] and '-N' in c for c in self.calls))

    def partitioned_import_fixture(self):
        for suffix, number in (('1', '8:17'), ('9', '8:25')):
            child = self.device('/dev/spare' + suffix, number, None, [])
            child.update(type='part', pkname='/dev/spare', partuuid='partition-' + suffix)
            self.devices.append(child)
        self.pool_member = '/dev/spare1'
        self.importable()
        self.import_full_path_listing = self.import_listing.replace('spare ONLINE', '/dev/spare1 ONLINE')

    def test_import_discovery_preserves_partition_path_instead_of_whole_disk_display(self):
        self.partitioned_import_fixture()
        with patch.dict(os.environ, {'ZPOOL_VDEV_NAME_GUID': '1'}):
            result = reconcile_storage(enrollment(Tier(1, 'import', '/dev/spare1', '12345')),
                                       self.state, self.save)
        self.assertEqual(result['tiers'][0]['guid'], '12345')
        self.assertIn(['zpool', 'import', '-N', '-d', '/dev/spare1', '12345'], self.calls)

    def test_import_discovery_never_accepts_a_sibling_partition(self):
        self.partitioned_import_fixture()
        with self.assertRaisesRegex(StorageError, 'membership differs'):
            reconcile_storage(enrollment(Tier(1, 'import', '/dev/spare9', '12345')), self.state, self.save)
        self.assertFalse(any(c[:2] == ['zpool', 'import'] and '-N' in c for c in self.calls))

    def test_import_discovery_parent_only_output_is_not_partition_proof(self):
        self.partitioned_import_fixture()
        self.import_full_path_listing = self.import_listing.replace('spare ONLINE', '/dev/spare ONLINE')
        with self.assertRaisesRegex(StorageError, 'membership differs'):
            reconcile_storage(enrollment(Tier(1, 'import', '/dev/spare1', '12345')), self.state, self.save)
        self.assertFalse(any(c[:2] == ['zpool', 'import'] and '-N' in c for c in self.calls))

    def test_import_does_not_mount_or_reassign_unrelated_mountpoint(self):
        self.importable()
        self.pool_mountpoint = '/important-data'
        with self.assertRaisesRegex(StorageError, 'mountpoint'):
            reconcile_storage(enrollment(Tier(1, 'import', '/dev/spare', '12345')), self.state, self.save)
        self.assertFalse(any(c[:2] in (['zfs', 'mount'], ['zfs', 'set']) for c in self.calls))

    def test_swap_skip_reports_low_space_and_unsupported_filesystems(self):
        self.set_active_swaps([])
        with patch('engine.provisioning.storage.os.statvfs', return_value=SimpleNamespace(f_bavail=1, f_frsize=4096)):
            result = reconcile_storage(enrollment(), self.state, self.save)
        self.assertIn('insufficient', result['swap']['reason'])
        self.mounts[0]['fstype'] = 'btrfs'
        result = reconcile_storage(enrollment(), self.state, self.save)
        self.assertIn('supported', result['swap']['reason'])
        self.assertFalse(self.swap.exists())

    def test_swap_sizes_allocation_and_records_verified_result(self):
        self.set_active_swaps([])
        with patch('engine.provisioning.storage.os.statvfs', return_value=SimpleNamespace(f_bavail=16, f_frsize=1024 ** 3)), \
                patch('engine.provisioning.storage.os.posix_fallocate', create=True) as allocate, \
                patch('engine.provisioning.storage._persist_swap') as persist:
            result = reconcile_storage(enrollment(), self.state, self.save)
        self.assertEqual(allocate.call_args.args[1:], (0, 4 * 1024 ** 3))
        self.assertEqual(result['swap'], {'status': 'complete', 'size': 4 * 1024 ** 3})
        persist.assert_called_once()

    def test_existing_active_swap_preserved_without_cli_inspection(self):
        before = self.swaps.read_text()
        result = reconcile_storage(enrollment(), self.state, self.save)
        self.assertEqual(result['swap'], {'status': 'skipped', 'reason': 'existing active swap preserved'})
        self.assertEqual(self.swaps.read_text(), before)
        self.assertFalse(any(command[0] in ('swapon', 'mkswap') for command in self.calls))
        self.assertFalse(self.swap.exists())

    def test_proc_swaps_empty_and_escaped_paths(self):
        self.set_active_swaps([])
        self.assertEqual(storage._active_swaps(), [])
        self.swaps.write_text('Filename\tType\tSize\tUsed\tPriority\n'
                              '/swap\\040file\\011name\\012with\\134slash\tfile\t2097148\t0\t-2\n')
        self.assertEqual(storage._active_swaps(), ['/swap file\tname\nwith\\slash'])

    def test_unobserved_swap_activation_is_not_persisted_or_reported_complete(self):
        self.set_active_swaps([])
        self.activate_swap = False
        with patch('engine.provisioning.storage.os.statvfs', return_value=SimpleNamespace(f_bavail=16, f_frsize=1024 ** 3)), \
                patch('engine.provisioning.storage.os.posix_fallocate', create=True), \
                patch('engine.provisioning.storage._persist_swap') as persist:
            result = reconcile_storage(enrollment(), self.state, self.save)
        self.assertEqual(result['swap']['status'], 'skipped')
        self.assertIn('activation did not become observable', result['swap']['reason'])
        persist.assert_not_called()

    def test_malformed_proc_swaps_stops_optional_allocation(self):
        for text in ('', 'wrong header\n', 'Filename Type Size Used Priority\n/swapfile file broken 0 -2\n'):
            with self.subTest(text=text):
                self.swaps.write_text(text)
                result = reconcile_storage(enrollment(), self.state, self.save)
                self.assertEqual(result['swap']['status'], 'skipped')
                self.assertIn('swap inventory', result['swap']['reason'])
                self.assertFalse(self.swap.exists())

    def test_small_memory_swap_is_two_gib(self):
        self.set_active_swaps([])
        self.meminfo.write_text('MemTotal:       4194304 kB\n')
        with patch('engine.provisioning.storage.os.statvfs', return_value=SimpleNamespace(f_bavail=16, f_frsize=1024 ** 3)), \
                patch('engine.provisioning.storage.os.posix_fallocate', create=True) as allocate, \
                patch('engine.provisioning.storage._persist_swap'):
            result = reconcile_storage(enrollment(), self.state, self.save)
        self.assertEqual(allocate.call_args.args[1:], (0, 2 * 1024 ** 3))
        self.assertEqual(result['swap']['size'], 2 * 1024 ** 3)

    def test_fstab_preserves_existing_content_and_does_not_duplicate_swap(self):
        actual_lstat = Path.lstat

        def root_owned(path, *args, **kwargs):
            info = actual_lstat(path, *args, **kwargs)
            if path == self.fstab:
                fields = list(info)
                fields[4] = 0
                return os.stat_result(fields)
            return info

        with patch('engine.provisioning.storage.Path.lstat', root_owned), \
                patch('engine.provisioning.storage.os.fchown'):
            _persist_swap()
            _persist_swap()
        self.assertEqual(self.fstab.read_text(), '# existing entries\n' +
                         f'{self.swap} none swap sw 0 0\n')

    def test_pool_guid_change_on_retry_is_rejected(self):
        spec = enrollment(Tier(1, 'create', '/dev/spare', None))
        reconcile_storage(spec, self.state, self.save)
        self.pool['guid'] = '67890'
        with self.assertRaisesRegex(StorageError, 'GUID'):
            reconcile_storage(spec, self.state, self.save)

    def test_interrupted_create_recovers_only_matching_pool_ownership(self):
        spec = enrollment(Tier(1, 'create', '/dev/spare', None))
        reconcile_storage(spec, self.state, self.save)
        record = self.state['storage']['tiers']['1']
        record['status'] = 'intent'
        del record['guid']
        reconcile_storage(spec, self.state, self.save)
        self.assertEqual(record['guid'], '12345')
        self.assertEqual(sum(c[:2] == ['zpool', 'create'] for c in self.calls), 1)
        self.pool_owner = 'other-enrollment'
        with self.assertRaisesRegex(StorageError, 'ownership'):
            reconcile_storage(spec, self.state, self.save)


if __name__ == '__main__':
    unittest.main()
