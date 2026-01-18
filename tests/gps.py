#!/usr/bin/python3

import datetime as dt
import logging
import time

from gpsdclient import GPSDClient

logging.basicConfig(format='%(asctime)s %(levelname)s: %(message)s', datefmt='%Y-%m-%dT%H:%M:%S', level=logging.INFO)


def get_gpsd_position():
	"""Get position from GPSD."""
	logging.info('Trying to figure out position using GPS')
	max_retries = 5
	retry_delay = 1
	for attempt in range(max_retries):
		try:
			with GPSDClient(host='localhost', port=2947, timeout=15) as client:
				for result in client.dict_stream(convert_datetime=True, filter=['TPV']):
					if result['class'] == 'TPV':
						utc = result.get('time', dt.datetime.now(dt.timezone.utc))
						lat = result.get('lat', 0)
						lon = result.get('lon', 0)
						alt = result.get('alt', 0)
						spd = result.get('speed', 0)
						cse = result.get('magtrack', 0) or result.get('track', 0)
						acc = result.get('sep', 0) or result.get('cep', 0)
						return utc, lat, lon, alt, spd, cse, acc
		except OSError as e:
			logging.warning('GPSD connection error (attempt %d/%d): %s', attempt + 1, max_retries, e)
			if attempt < max_retries - 1:
				time.sleep(retry_delay)
				retry_delay *= 2
		except Exception as e:
			logging.error('Error getting GPS data: %s', e)
			break


def get_gpsd_sat():
	"""Get satellite from GPSD."""
	logging.info('Trying to figure out satellite using GPS')
	max_retries = 5
	retry_delay = 1
	for attempt in range(max_retries):
		try:
			with GPSDClient(host='localhost', port=2947, timeout=15) as client:
				for result in client.dict_stream(convert_datetime=True, filter=['SKY']):
					if result['class'] == 'SKY':
						utc = result.get('time', dt.datetime.now(dt.timezone.utc))
						uSat = result.get('uSat', 0)
						nSat = result.get('nSat', 0)
						sats = result.get('satellites', [])
						for sat in sats:
							if sat.get('used'):
								uSats = len(sat)
							else:
								nSats = len(sats)
						return utc, uSat, nSat, uSats, nSats
		except OSError as e:
			logging.warning('GPSD connection error (attempt %d/%d): %s', attempt + 1, max_retries, e)
			if attempt < max_retries - 1:
				time.sleep(retry_delay)
				retry_delay *= 2
		except Exception as e:
			logging.error('Error getting GPS data: %s', e)
			break


if __name__ == '__main__':
	print(get_gpsd_position())
	print(get_gpsd_sat())
