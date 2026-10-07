import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import healthcheck as hc


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status = 200 if self.path == '/health' else 503
        self.send_response(status)
        self.end_headers()
        self.wfile.write(b'health')

    def log_message(self, *args):
        pass


class Checks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def config(self, checks=None):
        return {'checks': checks or [{'name': 'health', 'type': 'http', 'url': self.url + '/health'}]}

    def test_real_http_success(self):
        result = hc.collect(self.config())
        self.assertEqual(result['status'], 'OK')
        self.assertIn('HTTP 200', result['checks'][0]['message'])

    def test_real_http_failure(self):
        self.assertEqual(hc.collect(self.config([
            {'name': 'health', 'type': 'http', 'url': self.url + '/failed'}]))['status'], 'CRITICAL')

    def test_redirect_is_not_followed(self):
        with patch('healthcheck.http.client.HTTPConnection') as connection:
            connection.return_value.getresponse.return_value.status = 302
            check = {'name': 'http', 'type': 'http', 'url': self.url + '/health'}
            self.assertEqual(hc.run_check(check, 1)['status'], 'CRITICAL')
            self.assertEqual(connection.return_value.request.call_count, 1)

    def test_real_tcp_open_and_closed(self):
        check = {'name': 'port', 'type': 'tcp', 'host': '127.0.0.1', 'port': self.server.server_port}
        self.assertEqual(hc.run_check(check, 1)['status'], 'OK')
        with socket.socket() as closed:
            closed.bind(('127.0.0.1', 0))
            # Bound but not listening: the OS rejects the connection.
            check['port'] = closed.getsockname()[1]
            self.assertEqual(hc.run_check(check, 1)['status'], 'CRITICAL')

    def test_real_disk(self):
        with tempfile.TemporaryDirectory() as folder:
            check = {'name': 'disk', 'type': 'disk', 'path': folder,
                     'warn_percent': 99, 'critical_percent': 100}
            result = hc.run_check(check, 1)
            self.assertNotEqual(result['status'], 'UNKNOWN')
            self.assertIn('GiB free', result['message'])

    def test_disk_threshold_boundaries(self):
        check = {'name': 'disk', 'type': 'disk', 'path': str(Path.cwd()),
                 'warn_percent': 80, 'critical_percent': 90}
        for used, expected in [(79, 'OK'), (80, 'WARN'), (89, 'WARN'), (90, 'CRITICAL')]:
            with self.subTest(used=used), patch('healthcheck.shutil.disk_usage',
                                               return_value=shutil._ntuple_diskusage(100, used, 100-used)):
                self.assertEqual(hc.run_check(check, 1)['status'], expected)

    def test_backup_lifecycle(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'backup.tar'
            check = {'name': 'backup', 'type': 'backup', 'path': str(path),
                     'warn_hours': 24, 'critical_hours': 48}
            self.assertEqual(hc.run_check(check, 1)['status'], 'CRITICAL')
            path.write_bytes(b'')
            self.assertEqual(hc.run_check(check, 1)['status'], 'CRITICAL')
            path.write_bytes(b'real test backup')
            self.assertEqual(hc.run_check(check, 1)['status'], 'OK')
            for hours, expected in [(25, 'WARN'), (49, 'CRITICAL'), (-1, 'UNKNOWN')]:
                stamp = time.time() - hours * 3600
                os.utime(path, (stamp, stamp))
                self.assertEqual(hc.run_check(check, 1)['status'], expected)

    def test_service_states_and_safe_arguments(self):
        check = {'name': 'service', 'type': 'service', 'unit': 'demo.service'}
        for state, code, expected in [('active', 0, 'OK'), ('failed', 3, 'CRITICAL'),
                                       ('inactive', 3, 'CRITICAL'), ('unknown', 4, 'UNKNOWN')]:
            with self.subTest(state=state), patch('healthcheck.shutil.which', return_value='/bin/systemctl'), \
                    patch('healthcheck.subprocess.run', return_value=subprocess.CompletedProcess([], code, state, '')) as run:
                self.assertEqual(hc.run_check(check, 1)['status'], expected)
                self.assertEqual(run.call_args.args[0], ['systemctl', 'is-active', '--', 'demo.service'])
                self.assertFalse(run.call_args.kwargs.get('shell', False))

    def test_missing_systemctl(self):
        with patch('healthcheck.shutil.which', return_value=None):
            self.assertEqual(hc.run_check({'name': 's', 'type': 'service', 'unit': 'sshd.service'}, 1)['status'], 'UNKNOWN')

    def test_timeout_and_tls_errors(self):
        import ssl
        for error in [socket.timeout(), subprocess.TimeoutExpired('systemctl', 1),
                      ssl.SSLCertVerificationError('test certificate failure')]:
            with self.subTest(error=type(error).__name__), patch('healthcheck.probe', side_effect=error):
                self.assertEqual(hc.run_check({'name': 'timeout', 'type': 'http'}, 1)['status'], 'CRITICAL')

    def test_permission_failure(self):
        with patch('healthcheck.probe', side_effect=PermissionError('private path')):
            result = hc.run_check({'name': 'disk', 'type': 'disk'}, 1)
            self.assertEqual(result['status'], 'UNKNOWN')
            self.assertNotIn('private path', result['message'])

    def test_invalid_configuration(self):
        invalid = [
            {}, {'checks': []}, {'checks': [], 'typo': 1},
            {'checks': [{'name': 'bad', 'type': 'command', 'command': 'rm'}]},
            {'checks': [{'name': 's', 'type': 'service', 'unit': '--help'}]},
            {'checks': [{'name': 's', 'type': 'service', 'unit': 'x.service;id'}]},
            {'checks': [{'name': 'd', 'type': 'disk', 'path': '.', 'warn_percent': 80, 'critical_percent': 90}]},
            {'checks': [{'name': 'd', 'type': 'disk', 'path': str(Path.cwd()), 'warn_percent': 90, 'critical_percent': 80}]},
            {'checks': [{'name': 'p', 'type': 'tcp', 'host': 'localhost', 'port': True}]},
            {'checks': [{'name': 'p', 'type': 'tcp', 'host': 'localhost', 'port': 65536}]},
        ]
        for url in ['file:///etc/passwd', 'https://user:password@example.com/',
                    'https://example.com/?token=secret', 'https://example.com:99999/', 'http://example.com/a b']:
            invalid.append({'checks': [{'name': 'url', 'type': 'http', 'url': url}]})
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(ValueError):
                hc.validate(config)
        for value in [0, 17, 1.5, True, float('nan')]:
            with self.subTest(workers=value), self.assertRaises(ValueError):
                hc.validate(dict(self.config(), workers=value))

    def test_invalid_type_cli_returns_unknown(self):
        for value in ['["http"]', '{ value = "http" }', '1', 'true']:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / "invalid.toml"
                config.write_text('[[checks]]\nname="bad"\ntype=' + value + '\n', encoding="utf-8")
                result = subprocess.run([sys.executable, str(Path(hc.__file__)),
                                         "--config", str(config)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 3)
                self.assertIn("Configuration/report error:", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_duplicate_names_and_unknown_fields(self):
        first = self.config()['checks'][0]
        with self.assertRaises(ValueError):
            hc.validate(self.config([first, dict(first)]))
        with self.assertRaises(ValueError):
            hc.validate(self.config([dict(first, typo=1)]))

    def test_order_and_overall_state(self):
        checks = [dict(self.config()['checks'][0], name=str(i)) for i in range(4)]
        with patch('healthcheck.probe', side_effect=lambda c, t: (['OK', 'WARN', 'CRITICAL', 'UNKNOWN'][int(c['name'])], 'test')):
            report = hc.collect(self.config(checks))
        self.assertEqual(report['status'], 'UNKNOWN')
        self.assertEqual([r['name'] for r in report['checks']], ['0', '1', '2', '3'])

    def test_atomic_reports_and_markdown(self):
        report = hc.collect(self.config())
        report['checks'][0]['name'] = '<script>|check'
        text = hc.markdown(report)
        self.assertIn('&lt;script&gt;&#124;check', text)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'report.json'
            hc.atomic_write(path, 'old')
            hc.atomic_write(path, json.dumps(report))
            self.assertEqual(json.loads(path.read_text())['status'], 'OK')
            self.assertEqual(len(list(Path(folder).iterdir())), 1)

    def test_cli_actual_process_and_exit_codes(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'config.toml'
            output = Path(folder) / 'reports'
            for route, expected in [('health', 0), ('failed', 2)]:
                config.write_text(f'[[checks]]\nname="web"\ntype="http"\nurl="{self.url}/{route}"\n')
                result = subprocess.run([sys.executable, str(Path(hc.__file__)), '--config', str(config),
                                         '--output', str(output)], capture_output=True, timeout=10)
                self.assertEqual(result.returncode, expected)
                self.assertTrue((output / 'report.md').is_file())
                self.assertIn('web', json.loads((output / 'report.json').read_text())['checks'][0]['name'])
            config.write_text('broken TOML [')
            result = subprocess.run([sys.executable, str(Path(hc.__file__)), '--config', str(config)], capture_output=True)
            self.assertEqual(result.returncode, 3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
