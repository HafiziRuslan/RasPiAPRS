#!/usr/bin/python3
"""RasPiAPRS: Send APRS position and telemetry from Raspberry Pi to APRS-IS."""

import asyncio
import datetime as dt
import json
import logging
import logging.handlers
import math
import os
import pickle
import random
import re
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from collections import UserDict
from collections import deque
from dataclasses import dataclass
from typing import Callable
from typing import NamedTuple

import aiohttp
import aprslib
import dotenv
import humanize
import psutil
import symbols
import telegram
from aprslib.exceptions import ConnectionError as APRSConnectionError
from aprslib.exceptions import ParseError as APRSParseError
from geopy.geocoders import Nominatim
from gpsdclient import GPSDClient

# Default directory
ETC_DIR = '/etc'
TMP_DIR = '/var/tmp/RasPiAPRS'
LIB_DIR = '/var/lib/RasPiAPRS'
LOG_DIR = '/var/log/RasPiAPRS'
# Default paths for system files
OS_RELEASE_FILE = f'{ETC_DIR}/os-release'
MMDVMHOST_FILE = f'{ETC_DIR}/mmdvmhost'
# Temporary files path
GPS_FILE = f'{TMP_DIR}/gps.json'
LOCATION_ID_FILE = f'{TMP_DIR}/location_id.tmp'
STATUS_FILE = f'{TMP_DIR}/status.tmp'
# Library files path
MSG_TRACKING_FILE = f'{LIB_DIR}/msg_tracking.pkl'
NOMINATIM_CACHE_FILE = f'{LIB_DIR}/nominatim_cache.pkl'


def get_app_metadata():
	repo_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
	git_sha = ''
	if shutil.which('git'):
		try:
			git_sha = subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD^'], cwd=repo_path).decode('ascii').strip()
		except Exception:
			pass
	meta = {'name': 'RasPiAPRS', 'version': '0.1', 'github': 'https://git.new/RasPiAPRS'}
	try:
		with open(os.path.join(repo_path, 'pyproject.toml'), 'rb') as f:
			data = tomllib.load(f).get('project', {})
			meta.update({k: data.get(k, meta[k]) for k in ['name', 'version']})
			meta['github'] = data.get('urls', {}).get('github', meta['github'])
	except Exception as e:
		logging.warning('Failed to load project metadata: %s', e)
	return f'{"-".join(filter(None, [meta["name"], meta["version"], git_sha]))}', meta['github']


APP_NAME, PROJECT_URL = get_app_metadata()
FROMCALL = 'N0CALL'
TOCALL = 'APP642'


def configure_logging():
	"""Sets up logging."""
	global LOG_DIR
	dotenv.load_dotenv('.env')

	if not os.path.exists(LOG_DIR) or not os.access(LOG_DIR, os.W_OK):
		LOG_DIR = 'logs'
	os.makedirs(LOG_DIR, exist_ok=True)

	log_level_map = {
		0: 100,  # OFF
		1: logging.DEBUG,
		2: logging.INFO,
		3: logging.WARNING,
		4: logging.ERROR,
		5: logging.CRITICAL,
	}
	log_level = log_level_map.get(_env_get_int('LOG_LEVEL', 2))

	logger = logging.getLogger()
	logger.setLevel(log_level)

	for name in ['aiohttp', 'aprslib', 'asyncio', 'geopy', 'gpsdclient', 'hpack', 'httpx', 'telegram', 'urllib3']:
		logging.getLogger(name).setLevel(max(log_level, logging.WARNING))

	class ISO8601Formatter(logging.Formatter):
		def formatTime(self, record, datefmt=None):
			return dt.datetime.fromtimestamp(record.created, dt.timezone.utc).astimezone().isoformat(timespec='milliseconds')

	class LevelFilter(logging.Filter):
		def __init__(self, level):
			self.level = level

		def filter(self, record):
			return record.levelno == self.level

	formatter = ISO8601Formatter('%(asctime)s | %(levelname)-8s | %(threadName)-12s | %(name)s.%(funcName)s:%(lineno)d | %(message)s')

	console = logging.StreamHandler()
	console.setLevel(logging.WARNING)
	console.setFormatter(formatter)
	logger.addHandler(console)

	class NumberedRotatingFileHandler(logging.handlers.RotatingFileHandler):
		"""RotatingFileHandler with backup number before the extension."""

		def doRollover(self):
			"""Do a rollover, with numbering before the extension."""
			if self.stream:
				self.stream.close()
				self.stream = None
			if self.backupCount > 0:
				name, ext = os.path.splitext(self.baseFilename)
				for i in range(self.backupCount - 1, 0, -1):
					sfn, dfn = f'{name}{i}{ext}', f'{name}{i + 1}{ext}'
					if os.path.exists(sfn):
						if os.path.exists(dfn):
							os.remove(dfn)
						os.rename(sfn, dfn)
				dfn = f'{name}1{ext}'
				if os.path.exists(dfn):
					os.remove(dfn)
				self.rotate(self.baseFilename, dfn)
			if not self.delay:
				self.stream = self._open()

	log_files = {
		logging.DEBUG: '1-debug.log',
		logging.INFO: '2-info.log',
		logging.WARNING: '3-warning.log',
		logging.ERROR: '4-error.log',
		logging.CRITICAL: '5-critical.log',
	}

	max_bytes = _env_get_float('LOG_MAX_BYTES', 1) * 1024 * 1024
	max_count = _env_get_int('LOG_MAX_COUNT', 3)

	for level, filename in log_files.items():
		if level < log_level:
			continue
		try:
			path = os.path.join(LOG_DIR, filename)
			handler = NumberedRotatingFileHandler(path, maxBytes=max_bytes, backupCount=max_count)
			handler.setLevel(level)
			handler.addFilter(LevelFilter(level))
			handler.setFormatter(formatter)
			logger.addHandler(handler)
		except (OSError, PermissionError) as e:
			logging.error('Failed to create %s: %s', filename, e)


def _env_get_bool(key: str, default: str = 'False') -> bool:
	return os.getenv(key, default).lower() in ('true', '1', 't', 'y', 'yes')


def _env_get_float(key: str, default: float) -> float:
	val = os.getenv(key)
	if val is None:
		return default
	try:
		return float(val)
	except (ValueError, TypeError):
		return default


def _env_get_int(key: str, default: int, warning_msg: str | None = None) -> int:
	val = os.getenv(key)
	if val is None:
		return default
	try:
		return int(val)
	except (ValueError, TypeError):
		if warning_msg:
			logging.warning('%s, using %d', warning_msg, default)
		return default


def _env_get_int_or_none(key: str) -> int | None:
	val = os.getenv(key)
	if val is None:
		return None
	try:
		return int(val)
	except (ValueError, TypeError):
		return None


@dataclass
class Config:
	sleep: int = 600
	symbol_table: str = '/'
	symbol: str = 'n'
	symbol_overlay: str | None = None
	latitude: float = 0.0
	longitude: float = 0.0
	altitude: float = 0.0
	server: str = 'rotate.aprs2.net'
	port: int = 14580
	# filter: str = 'm/50'
	passcode: int = 0
	gpsd_enabled: bool = False
	gpsd_host: str = 'localhost'
	gpsd_port: int = 2947
	smartbeaconing_enabled: bool = False
	smartbeaconing_fast_speed: int = 100
	smartbeaconing_slow_speed: int = 10
	smartbeaconing_fast_rate: int = 60
	smartbeaconing_slow_rate: int = 600
	smartbeaconing_min_turn_angle: int = 28
	smartbeaconing_turn_slope: int = 255
	smartbeaconing_min_turn_time: int = 5
	telegram_enabled: bool = False
	telegram_token: str | None = None
	telegram_chat_id: str | None = None
	telegram_topic_id: int | None = None
	telegram_msg_topic_id: int | None = None
	telegram_loc_topic_id: int | None = None
	aprsthursday_enabled: bool = False
	aprsmysunday_enabled: bool = False
	additional_sender: list[str] | None = None

	def __post_init__(self):
		self.reload()

	def reload(self):
		"""Reload configuration from environment variables."""
		global FROMCALL
		dotenv.load_dotenv('.env', override=True)
		call_base = os.getenv('APRS_CALL', 'N0CALL')
		ssid = os.getenv('APRS_SSID', '0')
		FROMCALL = call_base if ssid == '0' else f'{call_base}-{ssid}'
		self.sleep = _env_get_int('SLEEP', 600, 'Sleep value error')
		self.symbol_table = os.getenv('APRS_SYMBOL_TABLE', '/')
		self.symbol = os.getenv('APRS_SYMBOL', 'n')
		if self.symbol_table not in ['/', '\\']:
			self.symbol_overlay = self.symbol_table
		else:
			self.symbol_overlay = None
		self.latitude = _env_get_float('APRS_LATITUDE', 0.0)
		self.longitude = _env_get_float('APRS_LONGITUDE', 0.0)
		self.altitude = _env_get_float('APRS_ALTITUDE', 0.0)
		self.server = os.getenv('APRSIS_SERVER', 'rotate.aprs2.net')
		self.port = _env_get_int('APRSIS_PORT', 14580, 'Port value error')
		# self.filter = os.getenv('APRSIS_FILTER', 'm/10')
		passcode = os.getenv('APRS_PASSCODE')
		if passcode:
			self.passcode = passcode
		else:
			logging.warning('Generating passcode')
			self.passcode = aprslib.passcode(call_base)
		self.gpsd_enabled = _env_get_bool('GPSD_ENABLE')
		if self.gpsd_enabled:
			self.gpsd_host = os.getenv('GPSD_HOST', 'localhost')
			self.gpsd_port = _env_get_int('GPSD_PORT', 2947)
		self.smartbeaconing_enabled = _env_get_bool('SMARTBEACONING_ENABLE')
		if self.smartbeaconing_enabled:
			self.smartbeaconing_fast_speed = _env_get_int('SMARTBEACONING_FASTSPEED', 100)
			self.smartbeaconing_slow_speed = _env_get_int('SMARTBEACONING_SLOWSPEED', 10)
			self.smartbeaconing_fast_rate = _env_get_int('SMARTBEACONING_FASTRATE', 60)
			self.smartbeaconing_slow_rate = _env_get_int('SMARTBEACONING_SLOWRATE', 600)
			self.smartbeaconing_min_turn_angle = _env_get_int('SMARTBEACONING_MINTURNANGLE', 28)
			self.smartbeaconing_turn_slope = _env_get_int('SMARTBEACONING_TURNSLOPE', 255)
			self.smartbeaconing_min_turn_time = _env_get_int('SMARTBEACONING_MINTURNTIME', 5)
		self.telegram_enabled = _env_get_bool('TELEGRAM_ENABLE')
		if self.telegram_enabled:
			self.telegram_token = os.getenv('TELEGRAM_TOKEN')
			self.telegram_chat_id = os.getenv('TELEGRAM_CHAT_ID')
			self.telegram_topic_id = _env_get_int_or_none('TELEGRAM_TOPIC_ID')
			self.telegram_msg_topic_id = _env_get_int_or_none('TELEGRAM_MSG_TOPIC_ID')
			self.telegram_loc_topic_id = _env_get_int_or_none('TELEGRAM_LOC_TOPIC_ID')
		self.aprsthursday_enabled = _env_get_bool('APRSTHURSDAY_ENABLE')
		self.aprsmysunday_enabled = _env_get_bool('APRSMYSUNDAY_ENABLE')
		self.aprshamfinity_enabled = _env_get_bool('APRSHAMFINITY_ENABLE')
		self.additional_sender = None
		if self.aprsthursday_enabled or self.aprsmysunday_enabled or self.aprshamfinity_enabled:
			senders_str = os.getenv('ADDITIONAL_SENDER')
			if senders_str:
				valid_senders = []
				for sender in senders_str.split(','):
					sender = sender.strip().upper()
					if re.match(r'^[A-Z0-9]+(-[A-Z0-9]+)?$', sender):
						valid_senders.append(sender)
					else:
						logging.warning('Invalid ADDITIONAL_SENDER format: %s. Ignoring.', sender)
				if valid_senders:
					self.additional_sender = valid_senders


class PersistentCounter:
	"""Base class for persistent counters that read/write a value from/to a file."""

	def __init__(self, path, modulo):
		self.file_path = path
		self.modulo = modulo
		self._count = 0
		self._load()

	def _load(self):
		try:
			with open(self.file_path) as fds:
				self._count = int(fds.readline())
			if self._count >= self.modulo:
				self._count = 0
		except (IOError, ValueError):
			self._count = 0

	def _save(self):
		try:
			with open(self.file_path, 'w') as fds:
				fds.write(f'{self._count:d}')
		except IOError:
			pass

	def __iter__(self):
		return self

	def __next__(self):
		self._count = (1 + self._count) % self.modulo
		if self._count == 0:
			self._count = 1
		self._save()
		return self._count


class PersistentDict(UserDict):
	"""A dictionary that is persisted to a JSON or pickle file."""

	def __init__(self, path):
		self.file_path = path
		self._is_pickle = self.file_path.endswith('.pkl')
		super().__init__(self._load())

	def _load(self):
		if not os.path.exists(self.file_path):
			return {}

		if self._is_pickle:
			try:
				with open(self.file_path, 'rb') as f:
					data = pickle.load(f)
			except (IOError, ValueError, pickle.UnpicklingError) as e:
				logging.warning('Failed to load persistent dict from %s: %s', self.file_path, e)
				return {}
		else:
			try:
				with open(self.file_path, 'r') as f:
					data = json.load(f)
			except (IOError, ValueError, json.JSONDecodeError) as e:
				logging.warning('Failed to load persistent dict from %s: %s', self.file_path, e)
				return {}
		if isinstance(data, dict):
			return data
		logging.warning('Data in %s is not a dictionary, ignoring.', self.file_path)
		return {}

	def _save(self):
		if self._is_pickle:
			try:
				with open(self.file_path, 'wb') as f:
					pickle.dump(self.data, f)
			except (IOError, OSError) as e:
				logging.error('Failed to save persistent dict to %s: %s', self.file_path, e)
		else:
			try:
				with open(self.file_path, 'w') as f:
					json.dump(self.data, f)
			except (IOError, OSError) as e:
				logging.error('Failed to save persistent dict to %s: %s', self.file_path, e)

	def reload(self):
		"""Reload data from disk."""
		self.data = self._load()

	def flush(self):
		"""Force save to disk."""
		self._save()


class Sequence(PersistentCounter):
	"""Class to manage APRS sequence."""

	def __init__(self, name='sequence', modulo=100):
		super().__init__(f'{LIB_DIR}/{name}.seq', modulo)


class Timer(PersistentCounter):
	"""Class to manage persistent timer."""

	def __init__(self, name='timer', modulo=86400):
		super().__init__(f'{TMP_DIR}/{name}.tmr', modulo)


def _alt_to_aprs(alt):
	"""Format altitude for APRS (meters to feet)."""
	alt_ft = alt / 0.3048 if alt else 0
	alt_ft = max(-999999, alt_ft)
	alt_ft = min(999999, alt_ft)
	return f'/A={alt_ft:06.0f}'


def _cse_to_aprs(cse):
	"""Format course for APRS."""
	cse = cse % 360 if cse else 0
	cse = max(0, cse)
	cse = min(359, cse)
	return f'{cse:03.0f}'


def _to_aprs_coord(val, pos_char, neg_char, width):
	"""Format coordinate for APRS."""
	direction = pos_char if val >= 0 else neg_char
	val = abs(val)
	deg = int(val)
	minutes = (val - deg) * 60
	return f'{deg:0{width}d}{minutes:05.2f}{direction}'


def _lat_to_aprs(lat):
	"""Format latitude for APRS."""
	return _to_aprs_coord(lat, 'N', 'S', 2)


def _lon_to_aprs(lon):
	"""Format longitude for APRS."""
	return _to_aprs_coord(lon, 'E', 'W', 3)


def _format_speed(val):
	"""Format speed for APRS with clamping."""
	val = max(0, min(999, val))
	return f'{val:03.0f}'


def _spd_to_kmh(spd):
	"""Format speed for APRS (mps to kmh)."""
	val = spd * 3.6 if spd else 0
	return _format_speed(val)


def _spd_to_knots(spd):
	"""Format speed for APRS (mps to knots)."""
	val = spd / 0.51444 if spd else 0
	return _format_speed(val)


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


class GPSFix(NamedTuple):
	"""Named structure for GPS position data."""

	timestamp: dt.datetime
	lat: float
	lon: float
	alt: float
	spd: float
	cse: float


class SATFix(NamedTuple):
	"""Named structure for GPS position data."""

	timestamp: dt.datetime
	uSat: float
	nSat: float


class GPSHandler:
	"""Class to handle GPS data retrieval and management."""

	def __init__(self, cfg):
		self.cfg = cfg
		self.healthy = True
		self.unhealthy_warning_sent = False

		# Initialize state with fallback data to avoid blocking I/O in the main loop
		fallback_lat, fallback_lon, fallback_alt = self._get_fallback_location()
		self._current_fix = GPSFix(dt.datetime.now(dt.timezone.utc), fallback_lat, fallback_lon, fallback_alt, 0.0, 0.0)
		self._current_sat = SATFix(dt.datetime.now(dt.timezone.utc), 0, 0)
		self.last_valid_fix = None

	def _fetch_from_gpsd(self, filter_class):
		"""Worker function to fetch data from GPSD synchronously."""
		try:
			with GPSDClient(host=self.cfg.gpsd_host, port=self.cfg.gpsd_port, timeout=5) as client:
				for result in client.dict_stream(convert_datetime=True, filter=[filter_class]):
					if filter_class == 'TPV' and result.get('mode', 0) > 1:
						return result
					if filter_class == 'SKY' and result.get('satellites'):
						return result
					if filter_class not in ('TPV', 'SKY'):
						return result
					return None
		except Exception as e:
			return e

	async def _retrieve_data(self, filter_class, log_name):
		"""Retrieve data from GPSD via executor to prevent blocking."""
		if not self.cfg.gpsd_enabled or not self.healthy:
			return None

		loop = asyncio.get_running_loop()
		try:
			result = await loop.run_in_executor(None, self._fetch_from_gpsd, filter_class)
			if isinstance(result, Exception):
				raise result

			if result:
				self.healthy = True
				self.unhealthy_warning_sent = False
				return result

			logging.warning('GPS %s unavailable.', log_name)
		except Exception as e:
			if not self.unhealthy_warning_sent:
				logging.error('GPSD (%s) connection error: %s', log_name, e)
				self.unhealthy_warning_sent = True
			self.healthy = False
		return None

	async def run_polling(self):
		"""Continuously poll GPSD for data in the background."""
		if not self.cfg.gpsd_enabled:
			return

		while True:
			# Update Position
			pos_res = await self._retrieve_data('TPV', 'position')
			if pos_res:
				self._current_fix = GPSFix(
					timestamp=pos_res.get('time', dt.datetime.now(dt.timezone.utc)),
					lat=pos_res.get('lat', 0.0),
					lon=pos_res.get('lon', 0.0),
					alt=pos_res.get('alt', 0.0),
					spd=pos_res.get('speed', 0.0),
					cse=pos_res.get('magtrack', 0.0) or pos_res.get('track', 0.0),
				)
				self.last_valid_fix = self._current_fix
				self._save_cache(self._current_fix.lat, self._current_fix.lon, self._current_fix.alt)

			# Update Satellites
			sat_res = await self._retrieve_data('SKY', 'satellite')
			if sat_res:
				self._current_sat = SATFix(
					timestamp=sat_res.get('time', dt.datetime.now(dt.timezone.utc)),
					uSat=sat_res.get('uSat', 0),
					nSat=sat_res.get('nSat', 0)
				)

			await asyncio.sleep(1)

	def _get_fallback_location(self):
		"""Retrieve location from cache or config (Static I/O)."""
		try:
			gps_cache = PersistentDict(GPS_FILE)
			lat = float(gps_cache.get('lat', self.cfg.latitude))
			lon = float(gps_cache.get('lon', self.cfg.longitude))
			alt = float(gps_cache.get('alt', self.cfg.altitude))
			return lat, lon, alt
		except (ValueError, TypeError):
			return self.cfg.latitude, self.cfg.longitude, self.cfg.altitude

	def _save_cache(self, lat, lon, alt):
		"""Save GPS location to cache file."""
		try:
			cache = PersistentDict(GPS_FILE)
			cache.update({'lat': lat, 'lon': lon, 'alt': alt})
			cache.flush()
		except Exception as e:
			logging.debug('Failed to save GPS cache: %s', e)

	async def get_loc_and_sat(self, gps_data=None):
		"""Returns current data immediately from memory."""
		# If external data is provided (e.g. from a task), use it
		if gps_data:
			return gps_data

		# Return internal memory state - no I/O or network calls here
		logging.debug(
			'GPSD data: pos: [time: %s, lat: %f, lon: %f, alt: %0.1f, spd: %0.0f, cse: %0.0f], sat: [time: %s, uSat: %0.0f, nSat: %0.0f]',
			self._current_fix.timestamp.astimezone().isoformat(timespec='seconds'),
			self._current_fix.lat,
			self._current_fix.lon,
			self._current_fix.alt,
			self._current_fix.spd,
			self._current_fix.cse,
			self._current_sat.timestamp.astimezone().isoformat(timespec='seconds'),
			self._current_sat.uSat,
			self._current_sat.nSat,
		)
		return self._current_fix, self._current_sat

	async def run_health_check(self):
		"""Periodically check the health of the GPSD service."""
		if not self.cfg.gpsd_enabled:
			self.healthy = False
			return
		loop = asyncio.get_running_loop()
		check_interval = 30
		while True:
			start_time = time.monotonic()
			try:
				result = await loop.run_in_executor(None, self._fetch_from_gpsd, 'VERSION')
				if isinstance(result, Exception):
					raise result
				if result:
					if not self.healthy:
						logging.info('GPSD connection restored.')
						self.unhealthy_warning_sent = False
					self.healthy = True
				else:
					if self.healthy:
						logging.warning('GPSD connection lost.')
					self.healthy = False
			except Exception as e:
				if self.healthy:
					logging.warning('GPSD connection lost: %s', e)
				self.healthy = False
			elapsed = time.monotonic() - start_time
			await asyncio.sleep(max(0, check_interval - elapsed))

	@staticmethod
	async def get_coordinates():
		"""Get approximate latitude and longitude using IP address lookup."""
		url = 'http://ip-api.com/json/'
		try:
			async with aiohttp.ClientSession() as session:
				async with session.get(url) as response:
					data = await response.json()
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

	@staticmethod
	def calculate_distance(lat1, lon1, lat2, lon2):
		"""Calculate distance between two coordinates in meters using Haversine formula."""
		R = 6371000
		phi1 = math.radians(lat1)
		phi2 = math.radians(lat2)
		delta_phi = math.radians(lat2 - lat1)
		delta_lambda = math.radians(lon2 - lon1)
		a = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
		c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
		return R * c


# Geolocation
_GEOLOCATOR = None
_NOMINATIM_CACHE = PersistentDict(NOMINATIM_CACHE_FILE)


def get_add_from_pos(lat, lon):
	"""Get address from coordinates, using a local cache."""
	global _GEOLOCATOR
	coord_key = f'{lat:.4f},{lon:.4f}'
	if coord_key in _NOMINATIM_CACHE:
		return _NOMINATIM_CACHE[coord_key]
	if _GEOLOCATOR is None:
		_GEOLOCATOR = Nominatim(user_agent=APP_NAME, timeout=10)
	try:
		location = _GEOLOCATOR.reverse((lat, lon), exactly_one=True, namedetails=True, addressdetails=True)
		if location:
			address = location.raw['address']
			_NOMINATIM_CACHE[coord_key] = address
			_NOMINATIM_CACHE.flush()
			logging.debug(f'Address cached for requested coordinates: {coord_key}')
			return address
		else:
			logging.warning(f'No address found for provided coordinates: {coord_key}')
			return None
	except Exception as e:
		logging.error('Error getting address: %s', e)
		return None


def format_address(address, include_flag=False):
	"""Format address dictionary into a string."""
	if not address:
		return ''
	area = address.get('suburb') or address.get('town') or address.get('city') or address.get('district') or ''
	cc_str = ''
	if cc := address.get('country_code'):
		cc = cc.upper()
		if include_flag:
			flag = ''.join(chr(ord(c) + 127397) for c in cc)
			cc_str = f'{cc}{flag}'
		else:
			cc_str = f'{cc}'
	return ' '.join([area, cc_str])


class SmartBeaconing(object):
	"""Class to handle SmartBeaconing logic."""

	def __init__(self, cfg):
		self.cfg = cfg
		self.last_beacon_time = 0
		self.last_course = 0
		self.is_moving = False
		self.initialized = False

	def _calculate_rate(self, spd_kmh):
		"""Calculate beacon rate based on speed."""
		if spd_kmh > self.cfg.smartbeaconing_fast_speed:
			return self.cfg.smartbeaconing_fast_rate
		if spd_kmh < self.cfg.smartbeaconing_slow_speed:
			return self.cfg.smartbeaconing_slow_rate
		return int(
			self.cfg.smartbeaconing_slow_rate
			- (
				(spd_kmh - self.cfg.smartbeaconing_slow_speed)
				* (self.cfg.smartbeaconing_slow_rate - self.cfg.smartbeaconing_fast_rate)
				/ (self.cfg.smartbeaconing_fast_speed - self.cfg.smartbeaconing_slow_speed)
			)
		)

	def _check_turn(self, cse, spd_kmh):
		"""Check if a turn is detected."""
		if spd_kmh < 5:
			return False, 0.0, 0.0
		heading_change = abs(cse - self.last_course)
		if heading_change > 180:
			heading_change = 360 - heading_change
		turn_threshold = self.cfg.smartbeaconing_min_turn_angle + (self.cfg.smartbeaconing_turn_slope / (spd_kmh if spd_kmh > 0 else 1))
		return heading_change > turn_threshold, heading_change, turn_threshold

	def should_send(self, gps_data):
		"""Determine if a beacon should be sent based on GPS data."""
		if not gps_data:
			return False
		_, _, _, _, spd, cse = gps_data
		now = time.time()
		if not self.initialized:
			self.initialized = True
			self.last_beacon_time = now
			self.last_course = cse
			return False
		spd_kmh = spd * 3.6 if spd else 0
		if self.is_moving:
			if spd_kmh <= 3:
				self.is_moving = False
				logging.info('SmartBeaconing disabled: Stopped moving.')
				return False
			rate = self._calculate_rate(spd_kmh)
			turn_detected, heading_change, turn_threshold = self._check_turn(cse, spd_kmh)
			time_since_last = now - self.last_beacon_time
			should_send = False
			if turn_detected and time_since_last > self.cfg.smartbeaconing_min_turn_time:
				logging.debug('SmartBeaconing: Turn detected (Heading difference: %.1f, Threshold: %.1f)', heading_change, turn_threshold)
				should_send = True
			elif time_since_last > rate:
				logging.debug('SmartBeaconing: Rate expired (Rate: %d, Speed: %d)', rate, spd_kmh)
				should_send = True
			if should_send:
				self.last_beacon_time = now
				self.last_course = cse
			return should_send
		else:
			if spd_kmh > 3:
				self.is_moving = True
				logging.info('SmartBeaconing enabled: Movement detected.')
				self.last_beacon_time = now
				self.last_course = cse
				return True
			return False


class SystemStats(object):
	"""Class to handle system statistics."""

	def __init__(self, cfg):
		self.cfg = cfg
		self._cache = {}
		self._temp_history = deque()
		self._mem_history = deque()
		self._cpu_history = deque()

	def _get_cached(self, key, func, ttl=10, default=None):
		"""Get cached data."""
		now = time.time()
		if key in self._cache:
			val, ts = self._cache[key]
			if now - ts < ttl:
				return val
		try:
			val = func()
		except Exception as e:
			logging.error('Unexpected error in %s: %s', key, e)
			val = default
		self._cache[key] = (val, now)
		return val

	def _fetch_raw_cpu_temp(self):
		"""Fetch raw CPU temperature."""
		return psutil.sensors_temperatures()['cpu_thermal'][0].current

	def _fetch_raw_cpu_load(self):
		"""Fetch raw CPU load."""
		return psutil.cpu_percent()

	def _fetch_raw_vram_used(self):
		"""Fetch raw memory usage."""
		return psutil.virtual_memory().used

	def _prune_history(self, history, now, window=None):
		"""Prune old entries from history."""
		window = window or self.cfg.sleep
		while history and history[0][0] < now - window:
			history.popleft()

	def _record_history(self, history, value, now, window=None):
		"""Records historical data points."""
		window = window or self.cfg.sleep
		history.append((now, value))
		self._prune_history(history, now, window)

	def _update_history(self, history, fetch_func, now):
		"""Update historical data points."""
		try:
			self._record_history(history, fetch_func(), now)
		except Exception:
			pass

	def _calculate_average(self, history):
		"""Calculate average of historical data points."""
		if history:
			return sum(v for _, v in history) / len(history)
		return None

	def check_stats(self):
		"""Check and record current temperature, load and memory."""
		now = time.time()
		self._update_history(self._temp_history, self._fetch_raw_cpu_temp, now)
		self._update_history(self._cpu_history, self._fetch_raw_cpu_load, now)
		self._update_history(self._mem_history, self._fetch_raw_vram_used, now)

	def cleanup(self):
		"""Clean up old history entries to ensure memory efficiency."""
		now = time.time()
		self._prune_history(self._temp_history, now)
		self._prune_history(self._cpu_history, now)
		self._prune_history(self._mem_history, now)

	def _get_stat_property(self, history, fetch_func, cache_key, scale=1):
		"""Get historical data property."""
		avg = self._calculate_average(history)
		if avg is not None:
			return int(avg * scale)

		def _fetch():
			return int(fetch_func() * scale)

		return self._get_cached(cache_key, _fetch, ttl=5, default=0)

	@property
	def avg_temp(self):
		"""Get CPU temperature in degC."""
		return self._get_stat_property(self._temp_history, self._fetch_raw_cpu_temp, 'avg_temp', 10)

	@property
	def avg_cpu(self):
		"""Get CPU load in percent."""
		return self._get_stat_property(self._cpu_history, self._fetch_raw_cpu_load, 'avg_cpu', 10)

	@property
	def avg_vram(self):
		"""Get used memory in bits."""
		return self._get_stat_property(self._mem_history, self._fetch_raw_vram_used, 'avg_vram')

	@property
	def storage_used(self):
		"""Get used disk space in bits."""

		def _fetch():
			return psutil.disk_usage('/').used

		return self._get_cached('storage_used', _fetch, ttl=60, default=0)

	@property
	def uptime(self):
		"""Get system uptime in a human-readable format."""

		def _fetch():
			uptime_seconds = dt.datetime.now(dt.timezone.utc).timestamp() - psutil.boot_time()
			uptime = dt.timedelta(seconds=uptime_seconds)
			u_str = humanize.precisedelta(uptime, minimum_unit='minutes', format='%0.0f')
			for unit, abbr in [
				(' years', 'y'),
				(' year', 'y'),
				(' months', 'mo'),
				(' month', 'mo'),
				(' days', 'd'),
				(' day', 'd'),
				(' hours', 'h'),
				(' hour', 'h'),
				(' minutes', 'm'),
				(' minute', 'm'),
				(' and', ''),
				(',', ''),
			]:
				u_str = u_str.replace(unit, abbr)
			return f'up: {u_str}'

		return self._get_cached('uptime', _fetch, ttl=60, default='')

	@property
	def os_info(self):
		"""Get operating system information."""

		def _fetch():
			osname = ''
			try:
				os_info = {}
				with open(OS_RELEASE_FILE) as osr:
					for line in osr:
						line = line.strip()
						if '=' in line:
							key, value = line.split('=', 1)
							os_info[key] = value.strip().replace('"', '')
				id_like = os_info.get('ID_LIKE', '').title()
				version_codename = os_info.get('VERSION_CODENAME', '')
				debian_version_full = os_info.get('DEBIAN_VERSION_FULL') or os_info.get('VERSION_ID', '')
				osname = f'{id_like}{debian_version_full}-{version_codename}'
			except (IOError, OSError):
				logging.warning('OS release file not found: %s', OS_RELEASE_FILE)
			kernelver = ''
			try:
				kernel = os.uname()
				kernelver = f'[{kernel.sysname}{kernel.release}{kernel.version.split(" ", 1)[0]}-{kernel.machine}]'
			except Exception as e:
				logging.error('Unexpected error: %s', e)
			return f'{" ".join(filter(None, [osname, kernelver]))}'

		return self._get_cached('os_info', _fetch, ttl=3600, default='')

	@property
	def mmdvm_info(self):
		"""Get MMDVM configured frequency and color code."""

		def _fetch():
			mmdvm_info = {}
			dmr_enabled = False
			try:
				with open(MMDVMHOST_FILE, 'r') as mmh:
					for line in mmh:
						if '[DMR]' in line:
							dmr_enabled = 'Enable=1' in next(mmh, '')
						elif '=' in line:
							key, value = line.split('=', 1)
							mmdvm_info[key.strip()] = value.strip()
			except (IOError, OSError):
				logging.warning('MMDVMHost file not found: %s', MMDVMHOST_FILE)
			rx_freq = int(mmdvm_info.get('RXFrequency', 0))
			tx_freq = int(mmdvm_info.get('TXFrequency', 0))
			color_code = int(mmdvm_info.get('ColorCode', 0))
			slot1 = int(mmdvm_info.get('Slot1', 0))
			slot2 = int(mmdvm_info.get('Slot2', 0))
			tx = humanize.metric(tx_freq, 'Hz', precision=6)
			offset = rx_freq - tx_freq
			shift = f'({"+" if offset > 0 else ""}{humanize.metric(offset, "Hz", precision=2)})' if offset != 0 else None
			if dmr_enabled:
				cc = f'C{color_code}'
				ts = ''
				if slot1 == 1 and slot2 == 1:
					ts = 'S1S2'
				elif slot1 == 1:
					ts = 'S1'
				elif slot2 == 1:
					ts = 'S2'
			return f'{", ".join(filter(None, [tx, shift, cc, ts]))}'

		return self._get_cached('mmdvm_info', _fetch, ttl=3600, default='')

	@property
	def traffic_info(self):
		"""Get network traffic info from vnstat."""

		def _fetch():
			try:
				output = subprocess.check_output(['vnstat', '--json', 'f', '1'], text=True)
				data = json.loads(output)
				best_rx = 0
				best_tx = 0
				max_total = -1
				found = False
				if data.get('interfaces'):
					for iface in data['interfaces']:
						fiveminute_traffic = iface.get('traffic', {}).get('fiveminute')
						if fiveminute_traffic:
							last_entry = fiveminute_traffic[-1]
							rx_bytes = last_entry.get('rx', 0)
							tx_bytes = last_entry.get('tx', 0)
							total = rx_bytes + tx_bytes
							if total > max_total:
								max_total = total
								best_rx = rx_bytes
								best_tx = tx_bytes
								found = True
				if found:
					rxtx = humanize.naturalsize(best_rx + best_tx, binary=True).replace(' ', '')
					return f'net: {rxtx}'
			except (FileNotFoundError, subprocess.CalledProcessError, IndexError, json.JSONDecodeError, KeyError) as e:
				logging.warning('Could not fetch or parse vnstat 5-min traffic: %s', e)
			return ''

		return self._get_cached('traffic_info', _fetch, ttl=300, default='')


class ScheduledMessageHandler:
	"""Class to handle sending scheduled messages."""

	def __init__(self, cfg, gps_handler):
		self.cfg = cfg
		self.gps_handler = gps_handler
		self.tracking = PersistentDict(MSG_TRACKING_FILE)
		self.messages = []
		self.sequences = {}
		self._init_messages()
		self._init_sequences()

	def _init_messages(self):
		"""Initialize scheduled messages."""
		self.messages = []
		definitions = [
			('aprsthursday_enabled', 'APRSThursday', 3, 'ANSRVR', 'CQ HOTG #{}', dt.timezone.utc),
			('aprshamfinity_enabled', 'APRSHamfinity', 6, '9M4GKS', 'CQ HAMFINITY #{}', dt.timezone.utc),
			('aprsmysunday_enabled', 'APRSMYSunday', 6, 'APRSMY', 'CHECK #{}', dt.timezone(dt.timedelta(hours=8))),
		]
		for attr, name, weekday, addrcall, template_fmt, tz in definitions:
			if getattr(self.cfg, attr, False):
				senders = [None]
				if self.cfg.additional_sender:
					senders.extend(self.cfg.additional_sender)
				for sender in senders:
					self.messages.append(
						{'name': name, 'weekday': weekday, 'addrcall': addrcall, 'template': template_fmt.format(name), 'from_call': sender, 'tz': tz}
					)

	def _init_sequences(self):
		"""Initialize sequence counters for each message type."""
		for msg_info in self.messages:
			source = msg_info['from_call'] or FROMCALL
			addrcall = msg_info['addrcall']
			seq_name = f'msg_sequence_{source}_{addrcall}'
			if seq_name not in self.sequences:
				self.sequences[seq_name] = Sequence(name=seq_name, modulo=100000)

	async def _is_due(self, msg_info) -> bool:
		"""Return True if the message described by *msg_info* should be sent now."""
		now = dt.datetime.now(msg_info['tz'])
		if now.weekday() != msg_info['weekday']:
			return False
		today = now.strftime('%Y-%m-%d')
		source = msg_info['from_call'] or FROMCALL
		tracking_key = f'{msg_info["name"]},{source},{msg_info["addrcall"]}'
		last_sent = self.tracking.get(tracking_key)
		if last_sent and last_sent.startswith(today):
			return False
		return True

	async def _send_one_with_delay(self, aprs_sender, gps_data=None, **msg_info):
		"""Perform ``_send_one`` after a random pause"""
		await asyncio.sleep(random.uniform(1, 15))
		await self._send_one(aprs_sender, gps_data=gps_data, **msg_info)

	async def send_all(self, aprs_sender, gps_data=None):
		"""Send all due scheduled messages."""
		sent_any = False
		for msg_info in self.messages:
			if await self._is_due(msg_info):
				asyncio.create_task(self._send_one_with_delay(aprs_sender, gps_data=gps_data, **msg_info))
				sent_any = True
		return sent_any

	async def _send_one(self, aprs_sender, name, weekday, addrcall, template, from_call=None, tz=dt.timezone.utc, gps_data=None):
		"""Send a single scheduled message to APRS-IS if it's due."""
		now = dt.datetime.now(tz)
		if now.weekday() != weekday:
			return False
		today = now.strftime('%Y-%m-%d')
		source = from_call or FROMCALL
		tracking_key = f'{name},{source},{addrcall}'
		last_sent = self.tracking.get(tracking_key)
		if last_sent and last_sent.startswith(today):
			return False
		loc_data, _ = gps_data if gps_data else await self.gps_handler.get_loc_and_sat()
		_, lat, lon, _, _, _ = loc_data
		gridsquare = latlon_to_grid(lat, lon)
		seq_name = f'msg_sequence_{source}_{addrcall}'
		seq = next(self.sequences[seq_name])
		message = f'{template} from {gridsquare} via {APP_NAME}'[:67]
		path_str = ''
		if from_call:
			path_str = f',{FROMCALL}*,qAR,{FROMCALL}'
		payload = f'{source}>{TOCALL}{path_str}::{addrcall:9s}:{message}{{{seq}'
		try:
			parsed = aprslib.parse(payload)
		except APRSParseError as err:
			logging.error('APRS packet parsing error at %s: %s', name, err)
			return False
		await aprs_sender.send_packet(payload, name)
		tg_msg = f'<u>Message {name}</u>\n\nFrom: <b>{parsed["from"]}</b>'
		if parsed.get('via'):
			tg_msg += f'\nvia: <b>{parsed["via"]}</b>'
		path_list = parsed.get('path')
		if path_list:
			tg_msg += f'\nPath: <b>{", ".join(path_list)}</b>'
		tg_msg += (
			f'\nTo: <b>{parsed["addresse"]}</b>\n\nMessage{"-" + parsed["msgNo"] if parsed.get("msgNo") else ""}: <b>{parsed["message_text"]}</b>'
		)
		await aprs_sender.tg_logger.log(tg_msg, topic_id=self.cfg.telegram_msg_topic_id)
		self.tracking[tracking_key] = now.isoformat()
		self.tracking.flush()
		return True


class TelegramLogger(object):
	"""Class to handle logging to Telegram."""

	def __init__(self, cfg, location_id_file=LOCATION_ID_FILE):
		self.cfg = cfg
		self.enabled = cfg.telegram_enabled
		self.bot = None
		self.location_id_file = location_id_file
		if not self.enabled:
			return
		self.token = cfg.telegram_token
		self.chat_id = cfg.telegram_chat_id
		if not self.token or not self.chat_id:
			logging.error('Telegram token or chat ID is missing. Disabling Telegram logging.')
			self.enabled = False
			return
		self.bot = telegram.Bot(self.token)
		self.topic_id = cfg.telegram_topic_id
		self.loc_topic_id = cfg.telegram_loc_topic_id

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

	async def log(self, tg_message: str, lat: float = 0.0, lon: float = 0.0, cse: float = 0.0, topic_id: int | None = None):
		"""Send log message and optionally location to Telegram channel."""
		if not self.enabled or not self.bot:
			return
		try:
			message = f'{tg_message}\n\n<code>{APP_NAME}</code>'
			msg_kwargs = {
				'chat_id': self.chat_id,
				'text': message,
				'parse_mode': 'HTML',
				'link_preview_options': {'is_disabled': True, 'prefer_small_media': True, 'show_above_text': True},
			}
			current_topic_id = topic_id if topic_id is not None else self.topic_id
			if current_topic_id:
				msg_kwargs['message_thread_id'] = current_topic_id
			msg = await self._call_with_retry(self.bot.send_message, **msg_kwargs)
			logging.info('Sent message to Telegram: %s/%s/%s', msg.chat_id, msg.message_thread_id, msg.message_id)
			if lat != 0 and lon != 0:
				await self._update_location(lat, lon, cse)
		except Exception as e:
			logging.error('Failed to send message to Telegram: %s', e)

	def _read_location_id(self):
		"""Reads location message ID and start time from file."""
		if not os.path.exists(self.location_id_file):
			return None, None
		try:
			with open(self.location_id_file, 'r') as f:
				parts = f.read().split(':')
				msg_id = int(parts[0])
				start_time = float(parts[1]) if len(parts) > 1 else time.time()
				return msg_id, start_time
		except (IOError, ValueError, IndexError) as e:
			logging.warning('Could not read or parse location ID file: %s', e)
			return None, None

	def _write_location_id(self, msg_id, start_time):
		"""Writes location message ID and start time to file."""
		try:
			with open(self.location_id_file, 'w') as f:
				f.write(f'{msg_id}:{start_time}')
		except IOError as e:
			logging.error('Failed to save location ID: %s', e)

	def _remove_location_id_file(self):
		"""Removes the location ID file."""
		if os.path.exists(self.location_id_file):
			try:
				os.remove(self.location_id_file)
			except OSError as e:
				logging.error('Failed to remove location ID file: %s', e)

	async def _update_location(self, lat, lon, cse):
		"""Update or send live location."""
		loc_msg_id, start_time = self._read_location_id()
		if loc_msg_id and start_time:
			if await self._try_edit_live_location(loc_msg_id, start_time, lat, lon, cse):
				return
		await self._send_new_live_location(lat, lon, cse)

	async def _try_edit_live_location(self, msg_id, start_time, lat, lon, cse):
		"""Attempt to edit an existing live location message."""
		try:
			edit_kwargs = {
				'chat_id': self.chat_id,
				'message_id': msg_id,
				'latitude': lat,
				'longitude': lon,
				'heading': cse if cse > 0 else None,
				'live_period': int(time.time() - start_time + 86400),
			}
			eloc = await self._call_with_retry(self.bot.edit_message_live_location, **edit_kwargs)
			logging.info('Edited location in Telegram: %s/%s', eloc.chat_id, eloc.message_id)
			return True
		except Exception as e:
			if 'message is not modified' in str(e):
				logging.debug('Live location not modified.')
				return True
			logging.warning('Failed to edit location in Telegram: %s. Sending a new one.', e)
			return False

	async def _send_new_live_location(self, lat, lon, cse):
		"""Send a new live location message."""
		try:
			loc_kwargs = {'chat_id': self.chat_id, 'latitude': lat, 'longitude': lon, 'heading': cse if cse > 0 else None, 'live_period': 86400}
			if self.loc_topic_id:
				loc_kwargs['message_thread_id'] = self.loc_topic_id
			elif self.topic_id:
				loc_kwargs['message_thread_id'] = self.topic_id
			loc = await self._call_with_retry(self.bot.send_location, **loc_kwargs)
			logging.info('Sent location to Telegram: %s/%s/%s', loc.chat_id, loc.message_thread_id, loc.message_id)
			self._write_location_id(loc.message_id, time.time())
		except Exception as e:
			logging.error('Failed to send new location to Telegram: %s', e)

	async def stop_location(self):
		"""Stop live location sharing."""
		if not self.enabled or not self.bot:
			return
		location_id, _ = self._read_location_id()
		if not location_id:
			return
		try:
			await self._call_with_retry(self.bot.stop_message_live_location, chat_id=self.chat_id, message_id=location_id)
			logging.info('Stopped live location in Telegram: %s/%s', self.chat_id, location_id)
		except Exception as e:
			logging.warning('Failed to stop live location in Telegram: %s', e)
		finally:
			self._remove_location_id_file()


class APRSSender:
	"""Class to handle APRS connection and packet sending."""

	def __init__(self, cfg, tg_logger, sys_stats, gps_handler, telem_seq):
		self.cfg = cfg
		self.tg_logger = tg_logger
		self.sys_stats = sys_stats
		self.gps_handler = gps_handler
		self.ais = None
		self.telem_seq = telem_seq

	def _get_timestamps(self, source_time: dt.datetime | None = None) -> tuple[str, str]:
		"""Generate APRS and ISO8601 timestamps."""
		ctime = source_time or dt.datetime.now(dt.timezone.utc)
		return ctime.strftime('%d%H%Mz'), ctime.astimezone().isoformat(timespec='seconds')

	async def connect(self):
		"""Establish connection to APRS-IS with retries."""
		logging.info('Connecting to APRS-IS server %s:%d as %s', self.cfg.server, self.cfg.port, FROMCALL)
		self.ais = aprslib.IS(FROMCALL, passwd=self.cfg.passcode, host=self.cfg.server, port=self.cfg.port)
		loop = asyncio.get_running_loop()
		max_retries = 5
		retry_delay = 5
		for attempt in range(max_retries):
			try:
				await loop.run_in_executor(None, self.ais.connect)
				# self.ais.set_filter(self.cfg.filter)
				logging.info('Connected to APRS-IS server %s:%d as %s', self.cfg.server, self.cfg.port, FROMCALL)
				return
			except APRSConnectionError as err:
				logging.warning('APRS connection error (attempt %d/%d): %s', attempt + 1, max_retries, err)
				if attempt < max_retries - 1:
					await asyncio.sleep(retry_delay)
					retry_delay = min(retry_delay * 2, 60)
		logging.error('Connection error, exiting')
		sys.exit(getattr(os, 'EX_NOHOST', 1))

	async def send_packet(self, payload, log_context='packet'):
		"""Send a packet with random delay and retry logic."""
		while True:
			try:
				await asyncio.sleep(random.uniform(0, 5))
				self.ais.sendall(payload)
				logging.info(payload)
				return
			except APRSConnectionError as err:
				logging.error('APRS connection error at %s: %s', log_context, err)
				await self.connect()

	async def send_position(self, gps_data=None):
		"""Send APRS position packet to APRS-IS."""
		loc_data, _ = gps_data if gps_data else await self.gps_handler.get_loc_and_sat()
		cur_time, cur_lat, cur_lon, cur_alt, cur_spd, cur_cse = loc_data
		latstr = _lat_to_aprs(cur_lat)
		lonstr = _lon_to_aprs(cur_lon)
		altstr = _alt_to_aprs(cur_alt)
		spdstr = _spd_to_knots(cur_spd)
		csestr = _cse_to_aprs(cur_cse)
		spdkmh = _spd_to_kmh(cur_spd)
		mmdvminfo = self.sys_stats.mmdvm_info
		osinfo = self.sys_stats.os_info
		comment = '; '.join(filter(None, [mmdvminfo, osinfo, PROJECT_URL]))
		timestamp, tg_timestamp = self._get_timestamps(cur_time)
		symbt = self.cfg.symbol_table
		symb = self.cfg.symbol
		if self.cfg.symbol_overlay:
			symbt = self.cfg.symbol_overlay
		tgposmoving = ''
		extdatstr = ''
		if cur_spd >= 1:
			extdatstr = f'{csestr}/{spdstr}'
			tgposmoving = f'\n\tSpeed: <b>{int(cur_spd)}m/s</b> | <b>{int(spdkmh)}km/h</b> | <b>{int(spdstr)}kn</b>\n\tCourse: <b>{int(cur_cse)}°</b>'
			if self.cfg.smartbeaconing_enabled:
				sspd = self.cfg.smartbeaconing_slow_speed
				fspd = self.cfg.smartbeaconing_fast_speed
				kmhspd = int(spdkmh)
				if kmhspd > fspd:
					symbt, symb = '\\', '>'
				elif sspd < kmhspd <= fspd:
					symbt, symb = '/', '>'
				elif 0 < kmhspd <= sspd:
					symbt, symb = '/', '('
		lookup_table = symbt if symbt in ['/', '\\'] else '\\'
		sym_desc = symbols.get_desc(lookup_table, symb).split('(')[0].strip()
		payload = f'{FROMCALL}>{TOCALL}:/{timestamp}{latstr}{symbt}{lonstr}{symb}{extdatstr}{altstr}{comment}'
		tg_pos = f'<u>{FROMCALL} Position</u>\n\nTime: <b>{tg_timestamp}</b>\nSymbol: {symbt}{symb} ({sym_desc})\nPosition:\n\tLatitude: <b>{cur_lat}</b>\n\tLongitude: <b>{cur_lon}</b>\n\tAltitude: <b>{cur_alt}m</b>{tgposmoving}\nComment: <b>{comment}</b>'
		await self.send_packet(payload, 'position')
		await self.tg_logger.log(tg_pos, cur_lat, cur_lon, int(csestr))

	async def send_header(self):
		"""Send APRS header information to APRS-IS."""
		caller = f'{FROMCALL}>{TOCALL}::{FROMCALL:9s}:'
		params = ['Temp', 'Load', 'RAM', 'ROM']
		units = ['deg.C', '%', 'GB', 'GB']
		eqns = ['0,0.1,0', '0,0.1,0', '0,0.001,0', '0,0.001,0']
		if self.cfg.gpsd_enabled:
			params.append('GPS')
			units.append('sats')
			eqns.append('0,1,0')
		payload = f'{caller}PARM.{",".join(params)}\r\n{caller}UNIT.{",".join(units)}\r\n{caller}EQNS.{",".join(eqns)}'
		tg_msg = f'<u>{FROMCALL} Header</u>\n\nParameters: <b>{",".join(params)}</b>\nUnits: <b>{",".join(units)}</b>\nEquations: <b>{",".join(eqns)}</b>\n\nValue: <code>[a,b,c]=(a×v²)+(b×v)+c</code>'
		await self.send_packet(payload, 'header')
		await self.tg_logger.log(tg_msg)

	async def send_telemetry(self, gps_data=None):
		"""Send APRS telemetry information to APRS-IS."""
		seq = next(self.telem_seq)
		cputemp = self.sys_stats.avg_temp
		cpuload = self.sys_stats.avg_cpu
		memused = self.sys_stats.avg_vram
		diskused = self.sys_stats.storage_used
		telemmemused = int(memused / 1.0000e6)
		telemdiskused = int(diskused / 1.0000e6)
		payload = f'{FROMCALL}>{TOCALL}:T#{seq:03d},{cputemp:d},{cpuload:d},{telemmemused:d},{telemdiskused:d}'
		tgtel = (
			f'<u>{FROMCALL} Telemetry</u>\n\n'
			f'Sequence: <b>#{seq}</b>\n'
			f'CPU Temp: <b>{cputemp / 10:.1f} °C</b>\n'
			f'CPU Load: <b>{cpuload / 10:.1f} %</b>\n'
			f'RAM Used: <b>{humanize.naturalsize(memused, binary=True)}</b>\n'
			f'ROM Used: <b>{humanize.naturalsize(diskused, binary=True)}</b>'
		)
		if self.cfg.gpsd_enabled:
			_, sat_data = gps_data if gps_data else await self.gps_handler.get_loc_and_sat()
			_, uSat, nSat = sat_data
			payload += f',{uSat:d}'
			if uSat > 0:
				tgtel += f'\nGPS Lock: <b>{uSat}</b>\nGPS Avail: <b>{nSat}</b>'
		await self.send_packet(payload, 'telemetry')
		await self.tg_logger.log(tgtel)

	async def send_status(self, gps_data=None):
		"""Send APRS status information to APRS-IS."""
		loc_data, sat_data = gps_data if gps_data else await self.gps_handler.get_loc_and_sat()
		cur_time, lat, lon, _, _, _ = loc_data
		timestamp, tg_timestamp = self._get_timestamps(cur_time)
		gridsquare = f'{latlon_to_grid(lat, lon)}'
		address = get_add_from_pos(lat, lon)
		near_add = format_address(address)
		near_add_tg = format_address(address, True)
		uptime = self.sys_stats.uptime
		traffic = self.sys_stats.traffic_info
		sats_info = ''
		if self.cfg.gpsd_enabled:
			_, u_sat, n_sat = sat_data
			if u_sat > 0:
				sats_info = f'gps: {u_sat}/{n_sat}'
		stat_text = f'{timestamp}{"; ".join(filter(None, [gridsquare, near_add, uptime, traffic, sats_info]))}'
		tele_text = f'Time: <b>{tg_timestamp}</b>\nText: <b>{"; ".join(filter(None, [gridsquare, near_add_tg, uptime, traffic, sats_info]))}</b>'
		payload = f'{FROMCALL}>{TOCALL}:>{stat_text}'
		tg_msg = f'<u>{FROMCALL} Status</u>\n\n<b>{tele_text}</b>'
		if os.path.exists(STATUS_FILE):
			try:
				with open(STATUS_FILE, 'r') as f:
					if f.read() == payload:
						return
			except (IOError, OSError):
				pass
		await self.send_packet(payload, 'status')
		try:
			with open(STATUS_FILE, 'w') as f:
				f.write(payload)
		except (IOError, OSError):
			pass
		await self.tg_logger.log(tg_msg)

	def close(self):
		"""Close the APRS-IS connection."""
		if self.ais:
			try:
				self.ais.close()
			except Exception:
				pass


def setup_signal_handling(reload_event):
	"""Setup signal handlers for reloading configuration."""
	loop = asyncio.get_running_loop()

	def signal_handler():
		logging.info('SIGHUP received. Reloading configuration...')
		reload_event.set()

	try:
		loop.add_signal_handler(signal.SIGHUP, signal_handler)
	except (AttributeError, NotImplementedError):
		logging.debug('Signal handling not supported on this platform.')


async def initialize_session(cfg):
	"""Initialize the APRS session components."""
	cfg.reload()
	if cfg.latitude == 0 and cfg.longitude == 0:
		cfg.latitude, cfg.longitude = await GPSHandler.get_coordinates()
	gps_handler = GPSHandler(cfg)
	if cfg.gpsd_enabled:
		loc_data, _ = await gps_handler.get_loc_and_sat()
		_, cfg.latitude, cfg.longitude, cfg.altitude, _, _ = loc_data
	tg_logger = TelegramLogger(cfg)
	sys_stats = SystemStats(cfg)
	telem_seq = Sequence(name='telem_sequence', modulo=1000)
	aprs_sender = APRSSender(cfg, tg_logger, sys_stats, gps_handler, telem_seq)
	await aprs_sender.connect()
	timer = Timer()
	sb = SmartBeaconing(cfg)
	scheduled_msg_handler = ScheduledMessageHandler(cfg, gps_handler)
	return aprs_sender, tg_logger, timer, sb, sys_stats, scheduled_msg_handler, gps_handler


def should_send_position(cfg, timer_tick, sb, gps_data):
	"""Determine if a position update is needed."""
	return (cfg.gpsd_enabled and cfg.smartbeaconing_enabled and sb.should_send(gps_data)) or (timer_tick % 1200 == 1)


def _get_tasks(cfg, timer_tick, sb, gps_data, aprs_sender, scheduled_msg_handler):
	class Task(NamedTuple):
		condition: bool
		func: Callable
		args: tuple
		kwargs: dict

	loc_data, _ = gps_data if gps_data else None

	return [
		Task(should_send_position(cfg, timer_tick, sb, loc_data), aprs_sender.send_position, (), {'gps_data': gps_data}),
		Task(timer_tick % 21600 == 1, aprs_sender.send_header, (), {}),
		Task(timer_tick % cfg.sleep == 1, aprs_sender.send_telemetry, (), {'gps_data': gps_data}),
		Task(True, scheduled_msg_handler.send_all, (aprs_sender,), {'gps_data': gps_data}),
	]


async def process_loop(cfg, aprs_sender, timer, sb, sys_stats, reload_event, scheduled_msg_handler, gps_handler):
	"""Run the main processing loop."""
	while True:
		timer_tick = next(timer)
		if reload_event.is_set():
			break
		if timer_tick % 20 == 0:
			sys_stats.check_stats()
		gps_data = await gps_handler.get_loc_and_sat()
		packet_sent = False
		tasks = _get_tasks(cfg, timer_tick, sb, gps_data, aprs_sender, scheduled_msg_handler)
		for task in tasks:
			if task.condition:
				try:
					res = await task.func(*task.args, **task.kwargs)
					sent = res if isinstance(res, bool) else True
					if sent:
						packet_sent = True
				except Exception as e:
					logging.error('Error executing task %s: %s', task.func.__name__, e, exc_info=True)
		if packet_sent:
			await aprs_sender.send_status(gps_data=gps_data)
		await asyncio.sleep(1)


async def main():
	"""Main function to run the APRS reporting loop."""
	reload_event = asyncio.Event()
	setup_signal_handling(reload_event)
	cfg = Config()
	health_check_task = None
	gps_polling_task = None
	while True:
		reload_event.clear()
		aprs_sender, tg_logger, timer, sb, sys_stats, scheduled_msg_handler, gps_handler = await initialize_session(cfg)
		if cfg.gpsd_enabled:
			health_check_task = asyncio.create_task(gps_handler.run_health_check())
			gps_polling_task = asyncio.create_task(gps_handler.run_polling())
		async with tg_logger:
			await tg_logger.log(f'🚀 {APP_NAME.split("-")[0]} Started')
			try:
				await process_loop(cfg, aprs_sender, timer, sb, sys_stats, reload_event, scheduled_msg_handler, gps_handler)
			finally:
				if reload_event.is_set():
					await tg_logger.log(f'🔄 {APP_NAME.split("-")[0]} Reloaded')
				else:
					await tg_logger.log(f'🛑 {APP_NAME.split("-")[0]} Stopped')
				await tg_logger.stop_location()
				if health_check_task:
					health_check_task.cancel()
				if gps_polling_task:
					gps_polling_task.cancel()
				aprs_sender.close()
		if not reload_event.is_set():
			break


if __name__ == '__main__':
	configure_logging()
	exit_code = 0
	try:
		logging.info('Starting the application...')
		asyncio.run(main())
	except KeyboardInterrupt:
		logging.info('Stopping application...')
	except Exception as e:
		logging.critical('Critical error occurred: %s', e, exc_info=True)
		exit_code = 1
	finally:
		logging.info('Exiting script...')
		sys.exit(exit_code)
