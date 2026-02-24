#!/usr/bin/python3

import datetime as dt
import logging
import time

from gpsdclient import GPSDClient

logging.basicConfig(format='%(asctime)s %(levelname)s: %(message)s', datefmt='%Y-%m-%dT%H:%M:%S', level=logging.INFO)


class GPSDPoller:
	"""A class to poll GPSD for position and satellite data."""

	def __init__(self, host='localhost', port=2947, timeout=5, max_retries=5):
		"""
		Initialize the GPSDPoller.

		Args:
			host (str): GPSD host.
			port (int): GPSD port.
			timeout (int): Connection timeout in seconds.
			max_retries (int): Maximum number of connection retries.
		"""
		self.host = host
		self.port = port
		self.timeout = timeout
		self.max_retries = max_retries
		self.logger = logging.getLogger(__name__)

	def _get_data(self, data_filter, data_processor):
		"""
		Generic method to get data from GPSD with retry logic.

		Args:
			data_filter (list): A list of GPSD report classes to filter for.
			data_processor (function): A function to process a GPSD report dictionary.

		Returns:
			The processed data, or None if it fails.
		"""
		self.logger.info('Trying to get %s data from GPSD', data_filter)
		retry_delay = 1
		for attempt in range(self.max_retries):
			try:
				with GPSDClient(host=self.host, port=self.port, timeout=self.timeout) as client:
					for result in client.dict_stream(convert_datetime=True, filter=data_filter):
						processed_data = data_processor(result)
						if processed_data:
							return processed_data
					self.logger.info('No valid data received yet, retrying...')
			except (OSError, ConnectionRefusedError) as e:
				self.logger.warning('GPSD connection error (attempt %d/%d): %s', attempt + 1, self.max_retries, e)
			except Exception as e:
				self.logger.error('Error getting GPS data: %s', e)
				break
			if attempt < self.max_retries - 1:
				time.sleep(retry_delay)
				retry_delay *= 2
		self.logger.error('Failed to get %s data after %d attempts.', data_filter, self.max_retries)
		return None

	def get_position(self):
		"""Get position from GPSD."""

		def process_tpv(result):
			if result['class'] == 'TPV' and result.get('mode', 0) > 1:
				utc = result.get('time', dt.datetime.now(dt.timezone.utc))
				lat = result.get('lat', 0.0)
				lon = result.get('lon', 0.0)
				alt = result.get('alt', 0.0)
				spd = result.get('speed', 0)
				cse = result.get('magtrack', 0) or result.get('track', 0)
				acc = result.get('sep', 0) or result.get('cep', 0)
				return utc, lat, lon, alt, spd, cse, acc
			self.logger.info('No GPS fix yet, waiting for TPV mode > 1...')
			return None

		return self._get_data(['TPV'], process_tpv)

	def get_satellites(self):
		"""Get satellite from GPSD."""

		def process_sky(result):
			if result['class'] == 'SKY':
				utc = result.get('time', dt.datetime.now(dt.timezone.utc))
				uSat = result.get('uSat', 0)
				nSat = result.get('nSat', 0)
				return utc, uSat, nSat
			return None

		return self._get_data(['SKY'], process_sky)


if __name__ == '__main__':
	gps_poller = GPSDPoller()
	print(gps_poller.get_position())
	print(gps_poller.get_satellites())
