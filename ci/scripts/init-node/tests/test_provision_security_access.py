import tempfile
import unittest
from pathlib import Path

from engine.provisioning.access import AccessError, Transition
from engine.provisioning.security import SecurityError
from test_provision_access import FakeHost, TOKEN


from provision_fakes import SecurityBoundary


class SecurityAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = FakeHost(Path(self.temp.name))
        self.security = SecurityBoundary()
        self.clock = 1000
        self.transition = Transition(self.host, now=lambda: self.clock)
        self.transition.security = self.security

    def begin(self, port=22):
        return self.transition.execute('begin', TOKEN, port, '192.0.2.10')

    def test_activation_failure_restores_original_access_and_security(self):
        self.security.fail_activation = True
        with self.assertRaises((AccessError, SecurityError)):
            self.begin()
        self.assertEqual(self.host.ports, [22])
        self.assertFalse(self.security.active)
        self.assertFalse(self.host.armed)
        self.assertEqual(self.transition.load()['status'], 'rolled_back')

    def test_security_cleanup_failure_does_not_prevent_ssh_recovery(self):
        self.begin()
        self.transition.execute('finalize', TOKEN)
        self.security.fail_restore = True
        self.clock += 301
        with self.assertRaises(AccessError):
            self.transition.execute('tick')
        self.assertEqual(self.host.ports, [22])
        self.assertTrue(self.host.armed)
        self.assertEqual(self.transition.load()['status'], 'recovery_pending')
        self.security.fail_restore = False
        self.transition.execute('tick')
        self.assertEqual(self.transition.load()['status'], 'rolled_back')
        self.assertFalse(self.host.armed)

    def test_committed_retry_cannot_accept_stopped_security(self):
        self.begin()
        self.transition.execute('finalize', TOKEN)
        self.transition.execute('commit', TOKEN)
        self.security.active = False
        with self.assertRaises((AccessError, SecurityError)):
            self.begin(2222)
        self.assertEqual(self.host.ports, [2222])

    def test_existing_final_ssh_still_activates_security_with_watchdog(self):
        self.host.existing_final = True
        self.host.ports = [2222]
        self.assertEqual(self.begin(2222)['status'], 'transition')
        self.assertTrue(self.security.active)
        self.assertTrue(self.host.armed)
        self.assertEqual(self.host.ports, [2222])
        self.transition.execute('finalize', TOKEN)
        self.transition.execute('commit', TOKEN)
        self.assertFalse(self.host.path('/etc/systemd/system/ssh.service.d/apex-init.conf').exists())
        self.assertFalse(self.host.armed)

    def test_legacy_committed_access_gains_security_without_reopening_root(self):
        self.begin()
        self.transition.execute('finalize', TOKEN)
        self.transition.execute('commit', TOKEN)
        state = self.transition.load()
        state.pop('security')
        self.transition.save(state)
        self.security.active = False
        self.assertEqual(self.begin(2222)['status'], 'transition')
        self.assertEqual(self.host.ports, [2222])
        self.assertTrue(self.security.active)
        self.assertTrue(self.host.armed)

    def test_failed_ssh_restore_still_restores_security_and_retries(self):
        self.begin()
        restore = self.host.restore
        self.host.restore = lambda state: (_ for _ in ()).throw(AccessError('restart failed'))
        self.clock += 301
        with self.assertRaises(AccessError):
            self.transition.execute('tick')
        self.assertFalse(self.security.active)
        self.assertTrue(self.host.armed)
        self.host.restore = restore
        self.transition.execute('tick')
        self.assertEqual(self.host.ports, [22])
        self.assertEqual(self.transition.load()['status'], 'rolled_back')

    def test_security_failure_at_commit_keeps_rollback_armed(self):
        self.begin()
        self.transition.execute('finalize', TOKEN)
        self.security.active = False
        with self.assertRaises(SecurityError):
            self.transition.execute('commit', TOKEN)
        self.assertTrue(self.host.armed)
        self.assertEqual(self.transition.load()['status'], 'final')
