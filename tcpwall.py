import socket
import threading
import queue
import logging
import json
import random
import time
from dataclasses import dataclass
from typing import Dict
import argparse

logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

@dataclass
class Tunnel:
    queue: queue.Queue
    client_socket: socket.socket
    last_active: float

class TunnelServer:
    def __init__(self, host='37.114.53.135', control_port=8080, min_port=10000, max_port=20000):
        self.host = host
        self.control_port = control_port
        self.min_port = min_port
        self.max_port = max_port
        self.tunnels: Dict[int, Tunnel] = {}
        self.used_ports = set()

    def _create_socket(self, port: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, port))
        sock.listen(5)
        return sock

    def _get_random_port(self) -> int:
        while True:
            port = random.randint(self.min_port, self.max_port)
            if port not in self.used_ports:
                return port

    def start(self):
        control_socket = self._create_socket(self.control_port)
        logger.info(f"Tunnel server started on {self.host}:{self.control_port}")
        logger.info("Ready to accept tunnel connections")

        cleanup_thread = threading.Thread(target=self._cleanup_inactive_tunnels)
        cleanup_thread.daemon = True
        cleanup_thread.start()

        while True:
            client_socket, addr = control_socket.accept()
            thread = threading.Thread(target=self._handle_client, args=(client_socket, addr))
            thread.daemon = True
            thread.start()

    def _cleanup_inactive_tunnels(self):
        while True:
            now = time.time()
            inactive_ports = []
            for port, tunnel in self.tunnels.items():
                if now - tunnel.last_active > 3600:
                    inactive_ports.append(port)
            for port in inactive_ports:
                logger.info(f"Cleaning up inactive tunnel on port {port}")
                self._cleanup_tunnel(port)
            time.sleep(60)

    def _cleanup_tunnel(self, port: int):
        if port in self.tunnels:
            try:
                self.tunnels[port].client_socket.close()
            except:
                pass
            del self.tunnels[port]
            self.used_ports.remove(port)

    def _handle_client(self, client_socket: socket.socket, addr):
        try:
            try:
                data = json.loads(client_socket.recv(1024).decode('utf-8'))
                requested_port = data.get('port')
            except:
                requested_port = None

            if requested_port and requested_port not in self.used_ports and self.min_port <= requested_port <= self.max_port:
                port = requested_port
            else:
                port = self._get_random_port()

            self.used_ports.add(port)
            tunnel_socket = self._create_socket(port)

            tunnel = Tunnel(
                queue=queue.Queue(),
                client_socket=client_socket,
                last_active=time.time()
            )
            self.tunnels[port] = tunnel

            tunnel_info = {'port': port}
            client_socket.send(json.dumps(tunnel_info).encode('utf-8'))

            thread = threading.Thread(target=self._handle_tunnel_traffic, args=(tunnel_socket, port))
            thread.daemon = True
            thread.start()

            self._handle_tunnel_client(port)
        except Exception as e:
            logger.error(f"Error handling client: {e}")
            if 'port' in locals():
                self._cleanup_tunnel(port)

    def _handle_tunnel_traffic(self, tunnel_socket: socket.socket, port: int):
        while port in self.tunnels:
            try:
                client_socket, _ = tunnel_socket.accept()
                request_data = client_socket.recv(4096)
                if port in self.tunnels:
                    self.tunnels[port].queue.put((request_data, client_socket))
                    self.tunnels[port].last_active = time.time()
                else:
                    client_socket.close()
            except Exception as e:
                logger.error(f"Error handling tunnel traffic: {e}")
                break
        tunnel_socket.close()

    def _handle_tunnel_client(self, port: int):
        tunnel = self.tunnels[port]
        try:
            while True:
                request_data, web_client_socket = tunnel.queue.get()
                tunnel.client_socket.send(request_data)
                response = tunnel.client_socket.recv(4096)
                if not response:
                    break
                web_client_socket.send(response)
                web_client_socket.close()
                tunnel.last_active = time.time()
        except Exception as e:
            logger.error(f"Error in tunnel client handler: {e}")
        finally:
            self._cleanup_tunnel(port)

class TunnelClient:
    def __init__(self, server_host: str, server_port: int, local_port: int, remote_port: int = None):
        self.server_host = server_host
        self.server_port = server_port
        self.local_port = local_port
        self.remote_port = remote_port

    def start(self):
        while True:
            try:
                self._connect()
            except Exception as e:
                logger.error(f"Connection error: {e}")
                logger.info("Reconnecting in 5 seconds...")
                time.sleep(5)

    def _connect(self):
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.connect((self.server_host, self.server_port))

        if self.remote_port:
            server_socket.send(json.dumps({'port': self.remote_port}).encode('utf-8'))
        else:
            server_socket.send(json.dumps({}).encode('utf-8'))

        tunnel_info = json.loads(server_socket.recv(1024).decode('utf-8'))
        port = tunnel_info['port']
        logger.info("Tunnel established!")
        logger.info(f"Forwarding localhost:{self.local_port} -> {self.server_host}:{port}")

        while True:
            try:
                data = server_socket.recv(4096)
                if not data:
                    break

                local_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                local_socket.connect(('localhost', self.local_port))
                local_socket.send(data)

                response = local_socket.recv(4096)
                server_socket.send(response)
                local_socket.close()
            except Exception as e:
                logger.error(f"Error forwarding traffic: {e}")
                break
        server_socket.close()

def main():
    parser = argparse.ArgumentParser(description='TCPWall')
    parser.add_argument('mode', choices=['server', 'client'])
    parser.add_argument('--host', default='0.0.0.0', help='Host to bind to')
    parser.add_argument('--port', type=int, default=8080, help='Control port')
    parser.add_argument('--local-port', type=int, help='Local port to forward (client only)')
    parser.add_argument('--remote-port', type=int, help='Desired remote port (client only)')
    parser.add_argument('--server-host', default="37.114.53.135", help='Tunnel server host (client only)')
    args = parser.parse_args()

    if args.mode == 'server':
        server = TunnelServer(host=args.host, control_port=args.port)
        server.start()
    else:
        if not args.local_port or not args.server_host:
            parser.error('Client mode requires --local-port and --server-host')
        client = TunnelClient(
            server_host=args.server_host,
            server_port=args.port,
            local_port=args.local_port,
            remote_port=args.remote_port
        )
        client.start()

if __name__ == "__main__":
    main()
