from engine.provisioning.security import SecurityError


class SecurityBoundary:
    """Isolate firewall/service effects while exercising the durable coordinator."""
    def __init__(self):
        self.active = False
        self.fail_activation = False
        self.fail_restore = False

    def preflight(self):
        return {}

    def snapshot(self, plan, port, address):
        return {'phase': 'prepared', 'was_active': self.active}

    def activate(self, state, persist):
        self.active = True
        state['phase'] = 'active'
        persist(state)
        if self.fail_activation:
            raise SecurityError('activation failed')

    def finalize(self, state, persist):
        self.verify(state)
        state['phase'] = 'final'
        persist(state)

    def verify(self, state):
        if not self.active:
            raise SecurityError('security stopped')

    def restore(self, state, persist):
        self.active = state['was_active']
        state['cleanup'] = 'pending' if self.fail_restore else 'complete'
        persist(state)
        if self.fail_restore:
            raise SecurityError('cleanup pending')
