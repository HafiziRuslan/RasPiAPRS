#!/usr/bin/python3
"""RasPiAPRS: Send APRS position and telemetry from Raspberry Pi to APRS-IS."""

import asyncio
import datetime as dt
import json
import logging
import logging.handlers
import os
import pickle
import sys
import time
from urllib.request import urlopen

import aprslib
import dotenv
import humanize
import psutil
import telegram
from aprslib.exceptions import ConnectionError as APRSConnectionError
from dotenv import set_key
from geopy.geocoders import Nominatim
from gpsdclient import GPSDClient

import symbols

# Default paths for system files
OS_RELEASE_FILE = '/etc/os-release'
PISTAR_RELEASE_FILE = '/etc/pistar-release'
WPSD_RELEASE_FILE = '/etc/WPSD-release'
MMDVMHOST_FILE = '/etc/mmdvmhost'
# Temporary files path
SEQUENCE_FILE = '/tmp/raspiaprs/sequence.tmp'
TIMER_FILE = '/tmp/raspiaprs/timer.tmp'
CACHE_FILE = '/tmp/raspiaprs/nominatim_cache.pkl'
LOCATION_ID_FILE = '/tmp/raspiaprs/location_id.tmp'
STATUS_FILE = '/tmp/raspiaprs/status.tmp'


# Set up logging
def configure_logging():
	log_dir = '/var/log/raspiaprs'
	if not os.path.exists(log_dir) or not os.access(log_dir, os.W_OK):
		log_dir = 'logs'
	if not os.path.exists(log_dir):
		os.makedirs(log_dir)

	logging.getLogger('aprslib').setLevel(logging.DEBUG)
	logging.getLogger('asyncio').setLevel(logging.DEBUG)
	logging.getLogger('hpack').setLevel(logging.DEBUG)
	logging.getLogger('httpx').setLevel(logging.DEBUG)
	logging.getLogger('telegram').setLevel(logging.DEBUG)
	logging.getLogger('urllib3').setLevel(logging.DEBUG)

	logger = logging.getLogger()
	logger.setLevel(logging.DEBUG)
	formatter = logging.Formatter('%(asctime)s | %(levelname)s | %(name)s.%(funcName)s | %(message)s', datefmt='%Y-%m-%dT%H:%M:%S')
	console_handler = logging.StreamHandler()
	console_handler.setLevel(logging.WARNING)
	console_handler.setFormatter(formatter)
	logger.addHandler(console_handler)

	class LevelFilter(logging.Filter):
		def __init__(self, level):
			self.level = level

		def filter(self, record):
			return record.levelno == self.level

	levels = {
		logging.DEBUG: 'debug.log',
		logging.INFO: 'info.log',
		logging.WARNING: 'warning.log',
		logging.ERROR: 'error.log',
		logging.CRITICAL: 'critical.log',
	}
	for level, filename in levels.items():
		try:
			handler = logging.handlers.RotatingFileHandler(os.path.join(log_dir, filename), maxBytes=3 * 1024 * 1024, backupCount=5)
			handler.setLevel(level)
			handler.addFilter(LevelFilter(level))
			handler.setFormatter(formatter)
			logger.addHandler(handler)
		except (OSError, PermissionError) as e:
			logging.error('Failed to create %s: %s', filename, e)


# Handle environment configuration
class Config(object):
	def __init__(self):
		dotenv.load_dotenv('.env')
		call = os.getenv('APRS_CALL', 'N0CALL')
		ssid = os.getenv('APRS_SSID', '0')
		self.call = call if ssid == '0' else f'{call}-{ssid}'
		self.sleep = int(os.getenv('SLEEP', 600))
		self.symbol_table = os.getenv('APRS_SYMBOL_TABLE', '/')
		self.symbol = os.getenv('APRS_SYMBOL', 'n')
		if self.symbol_table not in ['/', '\\']:
			self.symbol_overlay = self.symbol_table
		else:
			self.symbol_overlay = None
		lat = os.getenv('APRS_LATITUDE', 0)
		lon = os.getenv('APRS_LONGITUDE', 0)
		alt = os.getenv('APRS_ALTITUDE', 0)
		if lat == 0 and lon == 0:
			self.latitude, self.longitude = get_coordinates()
			self.altitude = alt
		else:
			self.latitude, self.longitude, self.altitude = lat, lon, alt
		self.server = os.getenv('APRSIS_SERVER', 'rotate.aprs2.net')
		self.port = int(os.getenv('APRSIS_PORT', 14580))
		self.filter = os.getenv('APRSIS_FILTER', 'm/10')
		passcode = os.getenv('APRS_PASSCODE')
		if passcode:
			self.passcode = passcode
		else:
			logging.warning('Generating passcode')
			self.passcode = aprslib.passcode(call)

	def __repr__(self):
		return ('<Config> call: {0.call}, passcode: {0.passcode} - {0.latitude}/{0.longitude}/{0.altitude}').format(self)

	@property
	def call(self):
		return self._call

	@call.setter
	def call(self, val):
		self._call = str(val)

	@property
	def sleep(self):
		return self._sleep

	@sleep.setter
	def sleep(self, val):
		try:
			self._sleep = int(val)
		except ValueError:
			logging.warning('Sleep value error, using 600')
			self._sleep = 600

	@property
	def latitude(self):
		return self._latitude

	@latitude.setter
	def latitude(self, val):
		self._latitude = val

	@property
	def longitude(self):
		return self._longitude

	@longitude.setter
	def longitude(self, val):
		self._longitude = val

	@property
	def altitude(self):
		return self._altitude

	@altitude.setter
	def altitude(self, val):
		self._altitude = val

	@property
	def symbol(self):
		return self._symbol

	@symbol.setter
	def symbol(self, val):
		self._symbol = str(val)

	@property
	def symbol_table(self):
		return self._symbol_table

	@symbol_table.setter
	def symbol_table(self, val):
		self._symbol_table = str(val)

	@property
	def symbol_overlay(self):
		return self._symbol_overlay

	@symbol_overlay.setter
	def symbol_overlay(self, val):
		self._symbol_overlay = str(val) if val else None

	@property
	def server(self):
		return self._server

	@server.setter
	def server(self, val):
		self._server = str(val)

	@property
	def port(self):
		return self._port

	@port.setter
	def port(self, val):
		try:
			self._port = int(val)
		except ValueError:
			logging.warning('Port value error, using 14580')
			self._port = 14580

	@property
	def passcode(self):
		return self._passcode

	@passcode.setter
	def passcode(self, val):
		self._passcode = str(val)


class Sequence(object):
	"""Class to manage APRS sequence."""

	_count = 0

	def __init__(self):
		self.sequence_file = SEQUENCE_FILE
		try:
			with open(self.sequence_file) as fds:
				self._count = int(fds.readline())
		except (IOError, ValueError):
			self._count = 0

	def flush(self):
		try:
			with open(self.sequence_file, 'w') as fds:
				fds.write('{0:d}'.format(self._count))
		except IOError:
			pass

	def __iter__(self):
		return self

	def next(self):
		return self.__next__()

	def __next__(self):
		self._count = (1 + self._count) % 999
		self.flush()
		return self._count


class Timer(object):
	"""Class to manage APRS timer."""

	_count = 0

	def __init__(self):
		self.timer_file = TIMER_FILE
		try:
			with open(self.timer_file) as fds:
				self._count = int(fds.readline())
		except (IOError, ValueError):
			self._count = 0

	def flush(self):
		try:
			with open(self.timer_file, 'w') as fds:
				fds.write('{0:d}'.format(self._count))
		except IOError:
			pass

	def __iter__(self):
		return self

	def next(self):
		return self.__next__()

	def __next__(self):
		self._count = (1 + self._count) % 86400
		self.flush()
		return self._count


class SmartBeaconing(object):
	"""Class to handle SmartBeaconing logic."""

	def __init__(self):
		self.last_beacon_time = 0
		self.last_course = 0
		self._load_config()

	def _load_config(self):
		self.fast_speed = int(os.getenv('SMARTBEACONING_FASTSPEED', 100))
		self.slow_speed = int(os.getenv('SMARTBEACONING_SLOWSPEED', 10))
		self.fast_rate = int(os.getenv('SMARTBEACONING_FASTRATE', 60))
		self.slow_rate = int(os.getenv('SMARTBEACONING_SLOWRATE', 600))
		self.min_turn_angle = int(os.getenv('SMARTBEACONING_MINTURNANGLE', 28))
		self.turn_slope = int(os.getenv('SMARTBEACONING_TURNSLOPE', 255))
		self.min_turn_time = int(os.getenv('SMARTBEACONING_MINTURNTIME', 5))

	def should_send(self, gps_data):
		"""Determine if a beacon should be sent based on GPS data."""
		if not gps_data:
			return False
		cur_spd = gps_data[4]
		cur_cse = gps_data[5]
		spd_kmh = int(cur_spd * 3.6) if cur_spd else 0
		if spd_kmh > self.fast_speed:
			rate = self.fast_rate
		elif spd_kmh < self.slow_speed:
			rate = self.slow_rate
		else:
			rate = int(self.slow_rate - ((spd_kmh - self.slow_speed) * (self.slow_rate - self.fast_rate) / (self.fast_speed - self.slow_speed)))
		turn_threshold = self.min_turn_angle + (self.turn_slope / (spd_kmh if spd_kmh > 0 else 1))
		heading_change = abs(cur_cse - self.last_course)
		if heading_change > 180:
			heading_change = 360 - heading_change
		turn_detected = spd_kmh > 5 and heading_change > turn_threshold
		time_since_last = time.time() - self.last_beacon_time
		should_send = False
		if turn_detected and time_since_last > self.min_turn_time:
			logging.debug('SmartBeaconing: Turn detected (Heading difference: %d, Threshold: %d)', heading_change, turn_threshold)
			should_send = True
		elif time_since_last > rate:
			logging.debug('SmartBeaconing: Rate expired (Rate: %d, Speed: %d)', rate, spd_kmh)
			should_send = True
		if should_send:
			self.last_beacon_time = time.time()
			self.last_course = cur_cse
		return should_send


class TelegramLogger(object):
	"""Class to handle logging to Telegram."""

	def __init__(self):
		self.enabled = os.getenv('TELEGRAM_ENABLE')
		self.bot = None
		if self.enabled:
			self.token = os.getenv('TELEGRAM_TOKEN')
			self.chat_id = os.getenv('TELEGRAM_CHAT_ID')
			self.topic_id = os.getenv('TELEGRAM_TOPIC_ID')
			self.loc_topic_id = os.getenv('TELEGRAM_LOC_TOPIC_ID')
			if not self.token or not self.chat_id:
				logging.error('Telegram token or chat ID is missing. Disabling Telegram logging.')
				self.enabled = False
			else:
				self.bot = telegram.Bot(self.token)
			if self.topic_id:
				try:
					self.topic_id = int(self.topic_id)
				except (ValueError, TypeError):
					logging.error('Invalid TELEGRAM_TOPIC_ID. It should be an integer.')
					self.topic_id = None
			if self.loc_topic_id:
				try:
					self.loc_topic_id = int(self.loc_topic_id)
				except (ValueError, TypeError):
					logging.error('Invalid TELEGRAM_LOC_TOPIC_ID. It should be an integer.')
					self.loc_topic_id = None

	async def __aenter__(self):
		if self.bot:
			await self.bot.initialize()
		return self

	async def __aexit__(self, exc_type, exc_val, exc_tb):
		if self.bot:
			await self.bot.shutdown()

	async def _call_with_retry(self, func, *args, **kwargs):
		"""Retry Telegram API calls with exponential backoff."""
		max_retries = 3
		delay = 1
		for attempt in range(max_retries):
			try:
				return await func(*args, **kwargs)
			except (telegram.error.NetworkError, telegram.error.TimedOut) as e:
				if attempt == max_retries - 1:
					raise e
				logging.warning('Telegram API error (attempt %d/%d): %s. Retrying in %ds...', attempt + 1, max_retries, e, delay)
				await asyncio.sleep(delay)
				delay *= 2

	async def log(self, tg_message: str, lat: float = 0, lon: float = 0, cse: float = 0):
		"""Send log message and optionally location to Telegram channel."""
		if not self.enabled or not self.bot:
			return
		try:
			msg_kwargs = {
				'chat_id': self.chat_id,
				'text': tg_message,
				'parse_mode': 'HTML',
				'link_preview_options': {'is_disabled': True, 'prefer_small_media': True, 'show_above_text': True},
			}
			if self.topic_id:
				msg_kwargs['message_thread_id'] = self.topic_id
			msg = await self._call_with_retry(self.bot.send_message, **msg_kwargs)
			logging.info('Sent message to Telegram: %s/%s/%s', msg.chat_id, msg.message_thread_id, msg.message_id)
			if lat != 0 and lon != 0:
				sent_location = False
				if os.path.exists(LOCATION_ID_FILE):
					try:
						with open(LOCATION_ID_FILE, 'r') as f:
							parts = f.read().split(':')
							loc_msg_id = int(parts[0])
							start_time = float(parts[1]) if len(parts) > 1 else time.time()
						edit_kwargs = {
							'chat_id': self.chat_id,
							'message_id': loc_msg_id,
							'latitude': lat,
							'longitude': lon,
							'heading': cse if cse > 0 else None,
							'live_period': int(time.time() - start_time + 86400),
						}
						eloc = await self._call_with_retry(self.bot.edit_message_live_location, **edit_kwargs)
						logging.info('Edited location in Telegram: %s/%s', eloc.chat_id, eloc.message_id)
						sent_location = True
					except Exception as e:
						if 'message is not modified' in str(e):
							sent_location = True
						else:
							logging.warning('Failed to edit location in Telegram: %s', e)
				if not sent_location:
					loc_kwargs = {
						'chat_id': self.chat_id,
						'latitude': lat,
						'longitude': lon,
						'heading': cse if cse > 0 else None,
						'live_period': 86400,
					}
					if self.loc_topic_id:
						loc_kwargs['message_thread_id'] = self.loc_topic_id
					elif self.topic_id:
						loc_kwargs['message_thread_id'] = self.topic_id
					loc = await self._call_with_retry(self.bot.send_location, **loc_kwargs)
					logging.info('Sent location to Telegram: %s/%s/%s', loc.chat_id, loc.message_thread_id, loc.message_id)
					try:
						with open(LOCATION_ID_FILE, 'w') as f:
							f.write(f'{loc.message_id}:{time.time()}')
					except Exception as e:
						logging.error('Failed to save location ID: %s', e)
		except Exception as e:
			logging.error('Failed to send message to Telegram: %s', e)

	async def stop_location(self):
		"""Stop live location sharing."""
		if not self.enabled or not self.bot:
			return
		if os.path.exists(LOCATION_ID_FILE):
			try:
				with open(LOCATION_ID_FILE, 'r') as f:
					parts = f.read().split(':')
					location_id = int(parts[0])
				try:
					await self._call_with_retry(self.bot.stop_message_live_location, chat_id=self.chat_id, message_id=location_id)
					logging.info('Stopped live location in Telegram: %s/%s', self.chat_id, location_id)
				except Exception as e:
					logging.warning('Failed to stop live location in Telegram: %s', e)
			except Exception as e:
				logging.error('Error stopping live location: %s', e)
			finally:
				if os.path.exists(LOCATION_ID_FILE):
					try:
						os.remove(LOCATION_ID_FILE)
					except OSError:
						pass


async def get_gpspos():
	"""Get position from GPSD."""
	if os.getenv('GPSD_ENABLE'):
		timestamp = dt.datetime.now(dt.timezone.utc)
		logging.debug('Trying to figure out position using GPS')
		max_retries = 5
		retry_delay = 1
		loop = asyncio.get_running_loop()

		try:
			env_lat = float(os.getenv('APRS_LATITUDE', 0))
			env_lon = float(os.getenv('APRS_LONGITUDE', 0))
			env_alt = float(os.getenv('APRS_ALTITUDE', 0))
		except ValueError:
			env_lat, env_lon, env_alt = 0, 0, 0

		def _gps_worker():
			try:
				with GPSDClient(os.getenv('GPSD_HOST', 'localhost'), int(os.getenv('GPSD_PORT', 2947)), 15) as client:
					for result in client.dict_stream(convert_datetime=True, filter=['TPV']):
						if result['class'] == 'TPV' and (result.get('mode', 0) != 0 or result.get('mode', 0) != 1):
							return result
						else:
							return None
			except Exception as e:
				return e

		for attempt in range(max_retries):
			try:
				result = await loop.run_in_executor(None, _gps_worker)
				if isinstance(result, Exception):
					raise result
				if result:
					logging.debug('GPS fix acquired')
					utc = result.get('time', timestamp)
					lat = result.get('lat', 0)
					lon = result.get('lon', 0)
					alt = result.get('alt', 0)
					spd = result.get('speed', 0)
					cse = result.get('magtrack', 0) or result.get('track', 0)
					if lat != 0 and lon != 0 and alt != 0:
						logging.debug('%s | GPS Position: %s, %s, %s, %s, %s', utc, lat, lon, alt, spd, cse)
						set_key('.env', 'APRS_LATITUDE', lat, quote_mode='never')
						set_key('.env', 'APRS_LONGITUDE', lon, quote_mode='never')
						set_key('.env', 'APRS_ALTITUDE', alt, quote_mode='never')
						Config.latitude = lat
						Config.longitude = lon
						Config.altitude = alt
						return utc, lat, lon, alt, spd, cse
				else:
					logging.warning('GPS Position unavailable')
					return timestamp, env_lat, env_lon, env_alt, 0, 0
			except Exception as e:
				logging.error('GPSD connection error (attempt %d/%d): %s', attempt + 1, max_retries, e)
				if attempt < max_retries - 1:
					await asyncio.sleep(retry_delay)
					retry_delay *= 2
				return timestamp, env_lat, env_lon, env_alt, 0, 0


def _mps_to_kmh(spd):
	spd *= 3.6 if spd else 0  # mps to kmh
	spd = max(0, spd)
	spd = min(999, spd)
	return '{0:03.0f}'.format(spd)


def get_coordinates():
	"""Get approximate latitude and longitude using IP address lookup."""
	logging.debug('Trying to figure out the coordinate using your IP address')
	url = 'http://ip-api.com/json/'
	try:
		with urlopen(url) as response:
			_data = response.read()
			data = json.loads(_data.decode())
	except Exception as err:
		logging.error('Failed to fetch coordinates from %s: %s', url, err)
		return 0, 0
	else:
		try:
			logging.debug('IP-Position: %f, %f', data['lat'], data['lon'])
			return data['lat'], data['lon']
		except (KeyError, TypeError) as err:
			logging.error('Unexpected response format: %s', err)
			return 0, 0


def latlon_to_grid(lat, lon, precision=6):
	"""Convert position to grid square."""
	lon += 180
	lat += 90
	field_lon = int(lon // 20)
	field_lat = int(lat // 10)
	grid = chr(field_lon + ord('A')) + chr(field_lat + ord('A'))
	if precision >= 4:
		square_lon = int((lon % 20) // 2)
		square_lat = int((lat % 10) // 1)
		grid += str(square_lon) + str(square_lat)
	if precision >= 6:
		subsq_lon = int(((lon % 2) / 2) * 24)
		subsq_lat = int(((lat % 1) / 1) * 24)
		grid += chr(subsq_lon + ord('A')) + chr(subsq_lat + ord('A'))
	return grid


def get_add_from_pos(lat, lon):
	"""Get address from coordinates, using a local cache."""
	if os.path.exists(CACHE_FILE):
		with open(CACHE_FILE, 'rb') as cache_file:
			cache = pickle.load(cache_file)
	else:
		cache = {}
	coord_key = f'{lat:.2f},{lon:.2f}'
	if coord_key in cache:
		logging.debug('Address found in cache for requested coordinates')
		return cache[coord_key]
	geolocator = Nominatim(user_agent='raspiaprs0.1b6')
	try:
		location = geolocator.reverse((lat, lon), exactly_one=True, namedetails=True, addressdetails=True)
		if location:
			address = location.raw['address']
			cache[coord_key] = address
			with open(CACHE_FILE, 'wb') as cache_file:
				pickle.dump(cache, cache_file)
			logging.debug('Address fetched and cached for requested coordinates')
			return address
		else:
			logging.warning('No address found for provided coordinates')
			return None
	except Exception as e:
		logging.error('Error getting address: %s', e)
		return None


def format_address(address, include_flag=False):
	"""Format address dictionary into a string."""
	if not address:
		return ''
	area = address.get('suburb') or address.get('town') or address.get('city') or address.get('district') or ''
	state = address.get('state') or address.get('region') or address.get('province') or ''
	full_area = ', '.join(filter(None, [area, state]))
	cc_str = ''
	if cc := address.get('country_code'):
		cc = cc.upper()
		if include_flag:
			flag = ''.join(chr(ord(c) + 127397) for c in cc)
			cc_str = f' [{flag} {cc}]'
		else:
			cc_str = f' ({cc})'
	return f' near {full_area}{cc_str},'


async def get_gpssat():
	"""Get satellite from GPSD."""
	if os.getenv('GPSD_ENABLE'):
		timestamp = dt.datetime.now(dt.timezone.utc)
		logging.debug('Trying to figure out satellite using GPS')
		max_retries = 5
		retry_delay = 1
		loop = asyncio.get_running_loop()

		def _gps_worker():
			try:
				with GPSDClient(os.getenv('GPSD_HOST', 'localhost'), int(os.getenv('GPSD_PORT', 2947)), 15) as client:
					for result in client.dict_stream(convert_datetime=True, filter=['SKY']):
						if result['class'] == 'SKY':
							return result
						else:
							return None
			except Exception as e:
				return e

		for attempt in range(max_retries):
			try:
				result = await loop.run_in_executor(None, _gps_worker)
				if isinstance(result, Exception):
					raise result

				if result:
					logging.debug('GPS Satellite acquired')
					utc = result.get('time', timestamp)
					uSat = result.get('uSat', 0)
					nSat = result.get('nSat', 0)
					return utc, uSat, nSat
				else:
					logging.warning('GPS Satellite unavailable')
					return timestamp, 0, 0
			except Exception as e:
				logging.error('GPSD connection error (attempt %d/%d): %s', attempt + 1, max_retries, e)
				if attempt < max_retries - 1:
					await asyncio.sleep(retry_delay)
					retry_delay *= 2
				return timestamp, 0, 0


def get_cpuload():
	"""Get CPU load as a percentage of total capacity."""
	try:
		load = psutil.getloadavg()[2]
		core = psutil.cpu_count()
		return int((load / core) * 100 * 1000)
	except Exception as e:
		logging.error('Unexpected error: %s', e)
		return 0


def get_memused():
	"""Get used memory in bits."""
	try:
		totalVmem = psutil.virtual_memory().total
		freeVmem = psutil.virtual_memory().free
		buffVmem = psutil.virtual_memory().buffers
		cacheVmem = psutil.virtual_memory().cached
		return totalVmem - freeVmem - buffVmem - cacheVmem
	except Exception as e:
		logging.error('Unexpected error: %s', e)
		return 0


def get_diskused():
	"""Get used disk space in bits."""
	try:
		diskused = psutil.disk_usage('/').used
		return diskused
	except Exception as e:
		logging.error('Unexpected error: %s', e)
		return 0


def get_temp():
	"""Get CPU temperature in degC."""
	try:
		temperature = psutil.sensors_temperatures()['cpu_thermal'][0].current
		return int(temperature * 10)
	except Exception as e:
		logging.error('Unexpected error: %s', e)
		return 0


def get_uptime():
	"""Get system uptime in a human-readable format."""
	try:
		uptime_seconds = dt.datetime.now(dt.timezone.utc).timestamp() - psutil.boot_time()
		uptime = dt.timedelta(seconds=uptime_seconds)
		return f'up: {humanize.naturaldelta(uptime)}'
	except Exception as e:
		logging.error('Unexpected error: %s', e)
		return ''


def get_osinfo():
	"""Get operating system information."""
	osname = ''
	try:
		with open(OS_RELEASE_FILE) as osr:
			for line in osr:
				if 'ID_LIKE=' in line:
					id_like = line.split('=', 1)[1].strip().title()
				if 'DEBIAN_VERSION_FULL=' in line:
					debian_version_full = line.split('=', 1)[1].strip()
				if 'VERSION_CODENAME=' in line:
					version_codename = line.split('=', 1)[1].strip()
			osname = f'{id_like}{debian_version_full} ({version_codename})'
	except (IOError, OSError):
		logging.warning('OS release file not found: %s', OS_RELEASE_FILE)
	kernelver = ''
	try:
		kernel = os.uname()
		kernelver = f'[{kernel.sysname} {kernel.release.split("+")[0]}]'
	except Exception as e:
		logging.error('Unexpected error: %s', e)
	return f' {osname} {kernelver}'


def get_mmdvminfo():
	"""Get MMDVM configured frequency and color code."""
	rx_freq, tx_freq, color_code, dmr_enabled = 0, 0, 0, False
	with open(MMDVMHOST_FILE, 'r') as mmh:
		for line in mmh:
			if line.startswith('RXFrequency='):
				rx_freq = int(line.strip().split('=')[1])
			elif line.startswith('TXFrequency='):
				tx_freq = int(line.strip().split('=')[1])
			elif line.startswith('ColorCode='):
				color_code = int(line.strip().split('=')[1])
			elif '[DMR]' in line:
				dmr_enabled = 'Enable=1' in next(mmh, '')
	rx = round(rx_freq / 1000000, 6)
	tx = round(tx_freq / 1000000, 6)
	shift = ''
	if tx > rx:
		shift = f' ({round(rx - tx, 6)}MHz)'
	elif tx < rx:
		shift = f' (+{round(rx - tx, 6)}MHz)'
	cc = f' CC{color_code}' if dmr_enabled else ''
	return (str(tx) + 'MHz' + shift + cc) + ','


async def send_position(ais, cfg, tg_logger, gps_data=None):
	"""Send APRS position packet to APRS-IS."""

	def _lat_to_aprs(lat):
		ns = 'N' if lat >= 0 else 'S'
		lat = abs(lat)
		deg = int(lat)
		minutes = (lat - deg) * 60
		return f'{deg:02d}{minutes:05.2f}{ns}'

	def _lon_to_aprs(lon):
		ew = 'E' if lon >= 0 else 'W'
		lon = abs(lon)
		deg = int(lon)
		minutes = (lon - deg) * 60
		return f'{deg:03d}{minutes:05.2f}{ew}'

	def _alt_to_aprs(alt):
		alt = alt / 0.3048 if alt else 0  # m to ft
		alt = max(-999999, alt)
		alt = min(999999, alt)
		return '/A={0:06.0f}'.format(alt)

	def _spd_to_aprs(spd):
		spd = spd / 0.51444 if spd else 0  # mps to knots
		spd = max(0, spd)
		spd = min(999, spd)
		return '{0:03.0f}'.format(spd)

	def _cse_to_aprs(cse):
		cse = cse % 360 if cse else 0
		cse = max(0, cse)
		cse = min(359, cse)
		return '{0:03.0f}'.format(cse)

	if gps_data:
		cur_time, cur_lat, cur_lon, cur_alt, cur_spd, cur_cse = gps_data
	elif os.getenv('GPSD_ENABLE'):
		cur_time, cur_lat, cur_lon, cur_alt, cur_spd, cur_cse = await get_gpspos()
	else:
		cur_time, cur_lat, cur_lon, cur_alt, cur_spd, cur_cse = None, 0, 0, 0, 0, 0
	if os.getenv('GPSD_ENABLE') or gps_data:
		if cur_lat == 0 and cur_lon == 0 and cur_alt == 0:
			cur_lat = os.getenv('APRS_LATITUDE', cfg.latitude)
			cur_lon = os.getenv('APRS_LONGITUDE', cfg.longitude)
			cur_alt = os.getenv('APRS_ALTITUDE', cfg.altitude)
	else:
		cur_lat = os.getenv('APRS_LATITUDE', cfg.latitude)
		cur_lon = os.getenv('APRS_LONGITUDE', cfg.longitude)
		cur_alt = os.getenv('APRS_ALTITUDE', cfg.altitude)
		cur_spd = 0
		cur_cse = 0
	latstr = _lat_to_aprs(float(cur_lat))
	lonstr = _lon_to_aprs(float(cur_lon))
	altstr = _alt_to_aprs(float(cur_alt))
	spdstr = _spd_to_aprs(float(cur_spd))
	csestr = _cse_to_aprs(float(cur_cse))
	spdkmh = _mps_to_kmh(float(cur_spd))
	mmdvminfo = get_mmdvminfo()
	osinfo = get_osinfo()
	comment = f'{mmdvminfo}{osinfo} https://github.com/HafiziRuslan/RasPiAPRS'
	ztime = dt.datetime.now(dt.timezone.utc)
	timestamp = cur_time.strftime('%d%H%Mz') if cur_time is not None else ztime.strftime('%d%H%Mz')
	symbt = cfg.symbol_table
	symb = cfg.symbol
	if cfg.symbol_overlay:
		symbt = cfg.symbol_overlay
	if int(cur_spd) > 0:
		if os.getenv('SMARTBEACONING_ENABLE'):
			sspd = int(os.getenv('SMARTBEACONING_SLOWSPEED'))
			fspd = int(os.getenv('SMARTBEACONING_FASTSPEED'))
			kmhspd = int(spdkmh)
			if kmhspd > fspd:
				symbt = '\\'
				symb = '>'
			if kmhspd > sspd and kmhspd <= fspd:
				symbt = '/'
				symb = '>'
			if kmhspd != 0 and kmhspd <= sspd:
				symbt = '/'
				symb = '('
		tgposmoving = f'\n\tSpeed: <b>{int(cur_spd)}m/s</b> | <b>{int(spdkmh)}km/h</b> | <b>{int(spdstr)}kn</b>\n\tCourse: <b>{int(cur_cse)}°</b>'
		extdatstr = f'{csestr}/{spdstr}'
	else:
		tgposmoving = ''
		extdatstr = ''
	lookup_table = symbt
	if symbt not in ['/', '\\']:
		lookup_table = '\\'
	sym_desc = symbols.get_desc(lookup_table, symb).split('(')[0].strip()
	payload = f'/{timestamp}{latstr}{symbt}{lonstr}{symb}{extdatstr}{altstr}{comment}'
	posit = f'{cfg.call}>APP642:{payload}'
	tgpos = f'<u>{cfg.call} Position</u>\n\nTime: <b>{timestamp}</b>\nSymbol: {symbt}{symb} ({sym_desc})\nPosition:\n\tLatitude: <b>{cur_lat}</b>\n\tLongitude: <b>{cur_lon}</b>\n\tAltitude: <b>{cur_alt}m</b>{tgposmoving}\nComment: <b>{comment}</b>'
	try:
		ais.sendall(posit)
		logging.info(posit)
		await tg_logger.log(tgpos, cur_lat, cur_lon, int(csestr))
		await send_status(ais, cfg, tg_logger)
	except APRSConnectionError as err:
		logging.error('APRS connection error at position: %s', err)
		ais = await ais_connect(cfg)
		ais = await send_position(ais, cfg, tg_logger, gps_data)
	return ais


async def send_header(ais, cfg, tg_logger):
	"""Send APRS header information to APRS-IS."""
	caller = '{0}>APP642::{0:9s}:'.format(cfg.call)
	parm = f'{caller}PARM.CPUTemp,CPULoad,RAMUsed,DiskUsed'
	unit = f'{caller}UNIT.deg.C,%,GB,GB'
	eqns = f'{caller}EQNS.0,0.1,0,0,0.001,0,0,0.001,0,0,0.001,0'

	def subfield(text):
		return f'{text.split(":")[-1].split(".", 1)[1]}'

	try:
		if os.getenv('GPSD_ENABLE'):
			parm += ',GPSUsed'
			unit += ',sats'
			eqns += ',0,1,0'
		head = f'{parm}\r\n{unit}\r\n{eqns}'
		tghead = f'<u>{cfg.call} Header</u>\n\nParameters: <b>{subfield(parm)}</b>\nUnits: <b>{subfield(unit)}</b>\nEquations: <b>{subfield(eqns)}</b>\n\nValue: <code>[a,b,c]=(a×v²)+(b×v)+c</code>'
		ais.sendall(head)
		logging.info(head)
		await tg_logger.log(tghead)
		await send_status(ais, cfg, tg_logger)
	except APRSConnectionError as err:
		logging.error('APRS connection error at header: %s', err)
		ais = await ais_connect(cfg)
		ais = await send_header(ais, cfg, tg_logger)
	return ais


async def send_telemetry(ais, cfg, tg_logger):
	"""Send APRS telemetry information to APRS-IS."""
	seq = Sequence().next()
	temp = get_temp()
	cpuload = get_cpuload()
	memused = get_memused()
	diskused = get_diskused()
	telemmemused = int(memused / 1.0000e6)
	telemdiskused = int(diskused / 1.0000e6)
	telem = '{}>APP642:T#{:03d},{:d},{:d},{:d},{:d}'.format(cfg.call, seq, temp, cpuload, telemmemused, telemdiskused)
	tgtel = f'<u>{cfg.call} Telemetry</u>\n\nSequence: <b>#{seq}</b>\nCPU Temp: <b>{temp / 10:.1f} °C</b>\nCPU Load: <b>{cpuload / 1000:.1f}%</b>\nRAM Used: <b>{humanize.naturalsize(memused, binary=True)}</b>\nDisk Used: <b>{humanize.naturalsize(diskused, binary=True)}</b>'
	if os.getenv('GPSD_ENABLE'):
		_, uSat, _ = await get_gpssat()
		telem += ',{:d}'.format(uSat)
		tgtel += f'\nGPS Used: <b>{uSat}</b>'
	try:
		ais.sendall(telem)
		logging.info(telem)
		await tg_logger.log(tgtel)
		await send_status(ais, cfg, tg_logger)
	except APRSConnectionError as err:
		logging.error('APRS connection error at telemetry: %s', err)
		ais = await ais_connect(cfg)
		ais = await send_telemetry(ais, cfg, tg_logger)
	return ais


async def send_status(ais, cfg, tg_logger):
	"""Send APRS status information to APRS-IS."""
	if os.getenv('GPSD_ENABLE'):
		_, lat, lon, *_ = await get_gpspos()
		if not (isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and lat != 0 and lon != 0):
			lat, lon = cfg.latitude, cfg.longitude
	else:
		lat, lon = cfg.latitude, cfg.longitude
	gridsquare = latlon_to_grid(lat, lon)
	address = get_add_from_pos(lat, lon)
	nearAdd = format_address(address)
	nearAddTg = format_address(address, True)
	ztime = dt.datetime.now(dt.timezone.utc)
	timestamp = ztime.strftime('%d%H%Mz')
	uptime = get_uptime()
	statustext = f'{timestamp}[{gridsquare}]{nearAdd} {uptime}'
	aprsstat = '{}>APP642:>{}'.format(cfg.call, statustext)
	statustextTg = f'{timestamp}[{gridsquare}]{nearAddTg} {uptime}'
	tgstat = f'<u>{cfg.call} Status</u>\n\n<b>{statustextTg}</b>'
	if os.getenv('GPSD_ENABLE'):
		sats = ', gps: '
		timez, uSat, nSat = await get_gpssat()
		if uSat != 0:
			timestamp = timez if timez is not None else ztime.strftime('%d%H%Mz')
			sats += f'{uSat}/{nSat}'
		else:
			sats += uSat
		aprsstat += sats
		tgstat += f'<b>{sats}</b>'
	if os.path.exists(STATUS_FILE):
		try:
			with open(STATUS_FILE, 'r') as f:
				if f.read() == aprsstat:
					return
		except (IOError, OSError):
			pass
	try:
		ais.sendall(aprsstat)
		logging.info(aprsstat)
		try:
			with open(STATUS_FILE, 'w') as f:
				f.write(aprsstat)
		except (IOError, OSError):
			pass
		await tg_logger.log(tgstat)
	except APRSConnectionError as err:
		logging.error('APRS connection error at status: %s', err)
		ais = await ais_connect(cfg)
		ais = await send_status(ais, cfg, tg_logger)
	return ais


async def ais_connect(cfg):
	"""Establish connection to APRS-IS with retries."""
	logging.info('Connecting to APRS-IS server %s:%d as %s', cfg.server, cfg.port, cfg.call)
	ais = aprslib.IS(cfg.call, passwd=cfg.passcode, host=cfg.server, port=cfg.port)
	loop = asyncio.get_running_loop()
	for _ in range(5):
		try:
			await loop.run_in_executor(None, ais.connect)
		except APRSConnectionError as err:
			logging.warning('APRS connection error: %s', err)
			await asyncio.sleep(20)
			continue
		else:
			# ais.set_filter(cfg.filter)
			logging.info('Connected to APRS-IS server %s:%d as %s', cfg.server, cfg.port, cfg.call)
			return ais
	logging.error('Connection error, exiting')
	sys.exit(getattr(os, 'EX_NOHOST', 1))


async def main():
	"""Main function to run the APRS reporting loop."""
	cfg = Config()
	if os.getenv('GPSD_ENABLE'):
		gps_data = await get_gpspos()
		cfg.timestamp, cfg.latitude, cfg.longitude, cfg.altitude, cfg.speed, cfg.course = gps_data
	ais = await ais_connect(cfg)
	tg_logger = TelegramLogger()
	sb = SmartBeaconing()
	async with tg_logger:
		try:
			for tmr in Timer():
				gps_data = None
				posUpdate = False
				if os.getenv('GPSD_ENABLE'):
					gps_data = await get_gpspos()
					if os.getenv('SMARTBEACONING_ENABLE'):
						if gps_data[4] < 1:
							if tmr % 900 == 1:
								posUpdate = True
						elif sb.should_send(gps_data):
							posUpdate = True
				else:
					if tmr % 1800 == 1:
						posUpdate = True
				if posUpdate:
					ais = await send_position(ais, cfg, tg_logger, gps_data=gps_data)
				if tmr % 14400 == 1:
					ais = await send_header(ais, cfg, tg_logger)
				if tmr % cfg.sleep == 1:
					ais = await send_telemetry(ais, cfg, tg_logger)
				await asyncio.sleep(1)
		finally:
			await tg_logger.stop_location()


if __name__ == '__main__':
	configure_logging()
	try:
		logging.info('Starting the application...')
		asyncio.run(main())
	except KeyboardInterrupt:
		logging.info('Stopping application...')
	except Exception as e:
		logging.critical('Critical error occurred: %s', e)
	finally:
		logging.info('Exiting script...')
		sys.exit(0)
