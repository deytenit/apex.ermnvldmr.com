"""Authenticated readiness endpoint for a fixed, bounded application probe."""
import argparse
import hmac
import http.server
import json
import os
import pathlib
import re
import signal
import subprocess
import threading
import time


class ReadinessServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(self, address, *, token, path, command, timeout=1.5, slots=4, single_flight=False):
        if not isinstance(token, str) or not re.fullmatch(r'[!-~]{16,512}', token):
            raise ValueError('A private visible-ASCII token is required')
        if not isinstance(path, str) or not re.fullmatch(r'/[A-Za-z0-9._~/-]+', path) or any(part in ('', '.', '..') for part in path[1:].split('/')):
            raise ValueError('A canonical absolute readiness path is required')
        if not isinstance(command, list) or not command or not all(isinstance(arg, str) and '\0' not in arg for arg in command) or not os.path.isabs(command[0]):
            raise ValueError('A fixed absolute executable and argv are required')
        if isinstance(timeout, bool) or not 0 < timeout <= 3 or isinstance(slots, bool) or not isinstance(slots, int) or not 1 <= slots <= 16:
            raise ValueError('Invalid probe bounds')
        if not isinstance(single_flight, bool):
            raise ValueError('single_flight must be a boolean')
        self.token = token.encode('ascii')
        self.ready_path = path
        self.command = command
        self.timeout = timeout
        self.single_flight = single_flight
        self.flight = None
        self.probe_slots = threading.BoundedSemaphore(slots)
        self.connection_slots = threading.BoundedSemaphore(slots * 2 + 4)
        self.active = set()
        self.active_lock = threading.Lock()
        self.closing = False
        super().__init__(address, Handler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(2)
        return connection, address

    def process_request(self, request, address):
        if not self.connection_slots.acquire(blocking=False):
            try:
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nCache-Control: no-store\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.connection_slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.connection_slots.release()

    def handle_error(self, request, address):
        pass

    @staticmethod
    def stop_probe(process):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()

    def probe(self):
        if not self.single_flight:
            return self.run_probe()
        with self.active_lock:
            if self.closing:
                return False
            flight = self.flight
            leader = flight is None
            if leader:
                flight = {'deadline': time.monotonic() + self.timeout, 'done': threading.Event(), 'ready': False}
                self.flight = flight
        if not leader:
            flight['done'].wait(max(0, flight['deadline'] - time.monotonic()))
            with self.active_lock:
                return flight['ready'] and not self.closing and time.monotonic() < flight['deadline']
        ready = False
        try:
            ready = self.run_probe(flight['deadline'])
        finally:
            with self.active_lock:
                flight['ready'] = ready and not self.closing and time.monotonic() < flight['deadline']
                self.flight = None
                flight['done'].set()
        return flight['ready']

    def run_probe(self, deadline=None):
        if not self.probe_slots.acquire(blocking=False):
            return False
        process = None
        try:
            with self.active_lock:
                if self.closing:
                    return False
                process = subprocess.Popen(self.command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
                self.active.add(process)
            try:
                timeout = self.timeout if deadline is None else max(0, deadline - time.monotonic())
                return process.wait(timeout=timeout) == 0
            except subprocess.TimeoutExpired:
                self.stop_probe(process)
                return False
        except (OSError, ValueError):
            return False
        finally:
            if process is not None:
                with self.active_lock:
                    self.active.discard(process)
            self.probe_slots.release()

    def server_close(self):
        with self.active_lock:
            self.closing = True
            if self.flight is not None:
                self.flight['ready'] = False
                self.flight['done'].set()
            active = list(self.active)
        for process in active:
            self.stop_probe(process)
        super().server_close()


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'APEX'
    sys_version = ''

    def log_message(self, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        self.respond(code)

    def respond(self, status):
        if self.request_version == 'HTTP/0.9':
            self.request_version = 'HTTP/1.0'
        self.send_response(status)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', '0')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True

    def dispatch(self):
        target = self.requestline.split()[1]
        if target != self.server.ready_path or self.path != self.server.ready_path:
            return self.respond(404)
        if self.command != 'GET':
            return self.respond(405)
        headers = self.headers.get_all('X-APEX-Ready-Token', [])
        supplied = headers[0].encode('latin-1') if len(headers) == 1 else b''
        if len(headers) != 1 or not hmac.compare_digest(supplied, self.server.token):
            return self.respond(403)
        return self.respond(204 if self.server.probe() else 503)

    do_GET = dispatch
    do_HEAD = dispatch
    do_POST = dispatch
    do_PUT = dispatch
    do_DELETE = dispatch
    do_OPTIONS = dispatch
    do_PATCH = dispatch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config = json.loads(pathlib.Path(args.config).read_text())
    token = pathlib.Path(config.pop('token_file')).read_text().strip()
    address = (config.pop('host', '127.0.0.1'), config.pop('port', 9092))
    server = ReadinessServer(address, token=token, **config)
    def terminate(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
