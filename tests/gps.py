#!/usr/bin/python3

# 	Copyright (C) 2026  HafiziRuslan
#
# 	This program is free software: you can redistribute it and/or modify
# 	it under the terms of the GNU General Public License as published by
# 	the Free Software Foundation, either version 3 of the License, or
# 	any later version.
#
# 	This program is distributed in the hope that it will be useful,
# 	but WITHOUT ANY WARRANTY; without even the implied warranty of
# 	MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# 	GNU General Public License for more details.
#
# 	You should have received a copy of the GNU General Public License
# 	along with this program. If not, see <https://www.gnu.org/licenses/>.

import json
import socket
import logging
from gpsdclient import GPSDClient


class MockConfig:
	gpsd_host = 'localhost'
	gpsd_port = 2947
	gpsd_sock = None


def fetch_from_gpsd(cfg, filter_class):
	"""Latest GPSD fetch logic from main.py."""
	try:
		host = cfg.gpsd_host or 'localhost'
		port = cfg.gpsd_port or 2947
		sock_path = cfg.gpsd_sock
		if sock_path:
			sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
			sock.settimeout(5)
			sock.connect(sock_path)
			sock.sendall(b'?WATCH={"enable":true,"json":true}\n')
			sock.sendall(b'?POLL;\n')
			lines = sock.makefile('r', encoding='utf-8')
		else:
			client = GPSDClient(host=host, port=port, timeout=5)
			lines = client.gpsd_lines()

		for i, line in enumerate(lines):
			# For TCP client, send POLL after version header
			if not sock_path and i == 1:
				if hasattr(client, 'sock') and client.sock:
					client.sock.sendall(b'?POLL;\n')

			answ = line.strip()
			if not answ or answ.startswith('{"class":"VERSION"'):
				continue

			result = json.loads(answ)
			res_class = result.get('class')

			if res_class == filter_class:
				if filter_class == 'TPV' and result.get('mode', 0) > 1:
					return result
				if filter_class == 'SKY' and result.get('satellites'):
					return result
				if filter_class not in ('TPV', 'SKY'):
					return result
			elif res_class == 'POLL' and filter_class in ('TPV', 'SKY'):
				if filter_class == 'TPV' and 'tpv' in result:
					for tpv in result['tpv']:
						if tpv.get('mode', 0) > 1:
							return tpv
				if filter_class == 'SKY' and 'sky' in result:
					for sky in result['sky']:
						if sky.get('satellites'):
							return sky
		return None
	except Exception as e:
		return e
	finally:
		if sock_path and 'sock' in locals():
			sock.close()
		elif 'client' in locals():
			client.close()


if __name__ == '__main__':
	print(
		'        RasPiAPRS  Copyright (C) 2026  HafiziRuslan'
		'      This program comes with ABSOLUTELY NO WARRANTY.'
		'      This is free software, and you are welcome to redistribute it under certain conditions.'
	)
	logging.basicConfig(level=logging.DEBUG)
	cfg = MockConfig()
	print('Fetching TPV...')
	print(fetch_from_gpsd(cfg, 'TPV'))
	print('Fetching SKY...')
	print(fetch_from_gpsd(cfg, 'SKY'))
