"""Reproducible integration demo: healthy -> outage -> recovery, no external targets."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import healthcheck as hc


class Endpoint(BaseHTTPRequestHandler):
    healthy = True

    def do_GET(self):
        self.send_response(200 if type(self).healthy else 503)
        self.end_headers()
        self.wfile.write(b'demo health endpoint')

    def log_message(self, *args):
        pass


def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Endpoint)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    evidence = Path('evidence')
    evidence.mkdir(exist_ok=True)
    try:
        with tempfile.TemporaryDirectory() as folder:
            backup = Path(folder) / 'backup.tar'
            backup.write_bytes(b'demo backup contents; not production data')
            config = {'checks': [
                {'name': 'Demo disk', 'type': 'disk', 'path': folder, 'warn_percent': 99, 'critical_percent': 100},
                {'name': 'Demo TCP', 'type': 'tcp', 'host': '127.0.0.1', 'port': server.server_port},
                {'name': 'Demo HTTP', 'type': 'http', 'url': f'http://127.0.0.1:{server.server_port}/health'},
                {'name': 'Demo backup', 'type': 'backup', 'path': str(backup), 'warn_hours': 24, 'critical_hours': 48},
            ]}
            for phase in ['healthy', 'outage', 'recovered']:
                Endpoint.healthy = phase != 'outage'
                timestamp = time.time() - (72 * 3600 if phase == 'outage' else 0)
                os.utime(backup, (timestamp, timestamp))
                report = hc.collect(config)
                expected = 'CRITICAL' if phase == 'outage' else 'OK'
                assert report['status'] == expected, report
                hc.atomic_write(evidence / f'{phase}.json', json.dumps(report, ensure_ascii=False, indent=2) + '\n')
                hc.atomic_write(evidence / f'{phase}.md', hc.markdown(report))
                print(f'{phase}: {report["status"]}')
                if phase == 'outage':
                    assert [r['status'] for r in report['checks']] == ['OK', 'OK', 'CRITICAL', 'CRITICAL']
            # Exercise the real Linux service query when a running systemd is available.
            if Path('/run/systemd/system').exists():
                service = hc.run_check({'name': 'systemd manager', 'type': 'service', 'unit': 'dbus.service'}, 3)
                hc.atomic_write(evidence / 'systemd.json', json.dumps(service, indent=2) + '\n')
                assert service['status'] == 'OK', service
                print('real dbus.service: OK')
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == '__main__':
    main()
