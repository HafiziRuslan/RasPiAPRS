#!/usr/bin/python3
"""RasPiAPRS: Send APRS position and telemetry from Raspberry Pi to APRS-IS."""

import asyncio
import datetime as dt
import json
import logging
import logging.handlers
import os
import pickle
import signal
import shutil
import subprocess
import sys
import re
import time
import tomllib
from dataclasses import dataclass
import aiohttp
import aprslib
import dotenv
import humanize
import psutil
import symbols
import telegram
from aprslib.exceptions import ConnectionError as APRSConnectionError, ParseError as APRSParseError
from geopy.geocoders import Nominatim
from gpsdclient import GPSDClient

# Default directory
ETC_DIR = '/etc'
TMP_DIR = '/var/tmp/raspiaprs'
# Default paths for system files
OS_RELEASE_FILE = f'{ETC_DIR}/os-release'
PISTAR_RELEASE_FILE = f'{ETC_DIR}/pistar-release'
WPSD_RELEASE_FILE = f'{ETC_DIR}/WPSD-release'
MMDVMHOST_FILE = f'{ETC_DIR}/mmdvmhost'
# Temporary files path
TIMER_FILE = f'{TMP_DIR}/timer.tmp'
SEQUENCE_FILE = f'{TMP_DIR}/sequence.tmp'
MSG_SEQUENCE_FILE = f'{TMP_DIR}/msg_sequence.tmp'
CACHE_FILE = f'{TMP_DIR}/nominatim_cache.pkl'
LOCATION_ID_FILE = f'{TMP_DIR}/location_id.tmp'
STATUS_FILE = f'{TMP_DIR}/status.tmp'
GPS_FILE = f'{TMP_DIR}/gps.json'


def get_app_metadata():
	repo_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
	git_sha = 'unknown'
	if shutil.which('git'):
		try:
			git_sha = subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD'], cwd=repo_path).decode('ascii').strip()
		except Exception:
			pass
	meta = {'name': 'RasPiAPRS', 'version': '0.0.0', 'github': 'https://github.com/HafiziRuslan/RasPiAPRS'}
	try:
		with open(os.path.join(repo_path, 'pyproject.toml'), 'rb') as f:
			data = tomllib.load(f).get('project', {})
			meta.update({k: data.get(k, meta[k]) for k in ['name', 'version']})
			meta['github'] = data.get('urls', {}).get('github', meta['github'])
	except Exception as e:
		logging.warning('Failed to load project metadata: %s', e)
	return f'{meta["name"]}-v{meta["version"]}-{git_sha}', meta['github']


APP_NAME, PROJECT_URL = get_app_metadata()
FROMCALL = 'N0CALL'
TOCALL = 'APP642'


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
	formatter = logging.Formatter(
		'%(asctime)s | %(levelname)s | %(threadName)s | %(name)s.%(funcName)s:%(lineno)d | %(message)s', datefmt='%Y-%m-%dT%H:%M:%S'
	)
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
			handler = logging.handlers.RotatingFileHandler(os.path.join(log_dir, filename), maxBytes=1 * 1024 * 1024, backupCount=5)
			handler.setLevel(level)
			handler.addFilter(LevelFilter(level))
			handler.setFormatter(formatter)
			logger.addHandler(handler)
		except (OSError, PermissionError) as e:
			logging.error('Failed to create %s: %s', filename, e)


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


def _env_get_float(key: str, default: float) -> float:
	val = os.getenv(key)
	if val is None:
		return default
	try:
		return float(val)
	except (ValueError, TypeError):
		return default


def _env_get_bool(key: str, default: str = 'False') -> bool:
	return os.getenv(key, default).lower() in ('true', '1', 't', 'y', 'yes')


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
		self.additional_sender = None
		if self.aprsthursday_enabled or self.aprsmysunday_enabled:
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

	def __enter__(self):
		try:
			with open(self.file_path) as fds:
				self._count = int(fds.readline())
		except (IOError, ValueError):
			self._count = 0
		return self

	def __exit__(self, exc_type, exc_val, exc_tb):
		try:
			with open(self.file_path, 'w') as fds:
				fds.write(f'{self._count:d}')
		except IOError:
			pass

	@property
	def count(self):
		self._count = (1 + self._count) % self.modulo
		return self._count


class Sequence(PersistentCounter):
	"""Class to manage APRS sequence."""

	def __init__(self, path=SEQUENCE_FILE, modulo=1000):
		super().__init__(path, modulo)


class Timer(PersistentCounter):
	"""Class to manage persistent timer."""

	def __init__(self):
		super().__init__(TIMER_FILE, 86400)


class SmartBeaconing(object):
	"""Class to handle SmartBeaconing logic."""

	def __init__(self, cfg):
		self.cfg = cfg
		self.last_beacon_time = 0
		self.last_course = 0

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

	def should_send(self, gps_data):
		"""Determine if a beacon should be sent based on GPS data."""
		if not gps_data:
			return False
		cur_spd = gps_data[4]
		cur_cse = gps_data[5]
		spd_kmh = cur_spd * 3.6 if cur_spd else 0
		if spd_kmh <= 3:
			return False
		rate = self._calculate_rate(spd_kmh)
		turn_threshold = self.cfg.smartbeaconing_min_turn_angle + (self.cfg.smartbeaconing_turn_slope / (spd_kmh if spd_kmh > 0 else 1))
		heading_change = abs(cur_cse - self.last_course)
		if heading_change > 180:
			heading_change = 360 - heading_change
		turn_detected = spd_kmh > 5 and heading_change > turn_threshold
		time_since_last = time.time() - self.last_beacon_time
		should_send = False
		if turn_detected and time_since_last > self.cfg.smartbeaconing_min_turn_time:
			logging.debug('SmartBeaconing: Turn detected (Heading difference: %d, Threshold: %d)', heading_change, turn_threshold)
			should_send = True
		elif time_since_last > rate:
			logging.debug('SmartBeaconing: Rate expired (Rate: %d, Speed: %d)', rate, spd_kmh)
			should_send = True
		if should_send:
			self.last_beacon_time = time.time()
			self.last_course = cur_cse
		return should_send


class SystemStats(object):
	"""Class to handle system statistics."""

	def __init__(self):
		self._cache = {}

	def _get_cached(self, key, func, ttl=10):
		now = time.time()
		if key in self._cache:
			val, ts = self._cache[key]
			if now - ts < ttl:
				return val
		val = func()
		self._cache[key] = (val, now)
		return val

	def avg_cpu_load(self):
		"""Get CPU load as a percentage of total capacity."""

		def _fetch():
			try:
				load = psutil.getloadavg()[2]
				core = psutil.cpu_count()
				return int((load / core) * 100 * 1000)
			except Exception as e:
				logging.error('Unexpected error: %s', e)
				return 0

		return self._get_cached('cpu_load', _fetch, ttl=5)

	def memory_used(self):
		"""Get used memory in bits."""

		def _fetch():
			try:
				totalVmem = psutil.virtual_memory().total
				freeVmem = psutil.virtual_memory().free
				buffVmem = psutil.virtual_memory().buffers
				cacheVmem = psutil.virtual_memory().cached
				return totalVmem - freeVmem - buffVmem - cacheVmem
			except Exception as e:
				logging.error('Unexpected error: %s', e)
				return 0

		return self._get_cached('memory_used', _fetch, ttl=5)

	def storage_used(self):
		"""Get used disk space in bits."""

		def _fetch():
			try:
				diskused = psutil.disk_usage('/').used
				return diskused
			except Exception as e:
				logging.error('Unexpected error: %s', e)
				return 0

		return self._get_cached('storage_used', _fetch, ttl=60)

	def cur_temp(self):
		"""Get CPU temperature in degC."""

		def _fetch():
			try:
				temperature = psutil.sensors_temperatures()['cpu_thermal'][0].current
				return int(temperature * 10)
			except Exception as e:
				logging.error('Unexpected error: %s', e)
				return 0

		return self._get_cached('cur_temp', _fetch, ttl=5)

	def uptime(self):
		"""Get system uptime in a human-readable format."""

		def _fetch():
			try:
				uptime_seconds = dt.datetime.now(dt.timezone.utc).timestamp() - psutil.boot_time()
				uptime = dt.timedelta(seconds=uptime_seconds)
				return f'up: {humanize.naturaldelta(uptime)}'
			except Exception as e:
				logging.error('Unexpected error: %s', e)
				return ''

		return self._get_cached('uptime', _fetch, ttl=60)

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

		return self._get_cached('os_info', _fetch, ttl=300)

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
			rx = round(rx_freq / 1000000, 6)
			tx = round(tx_freq / 1000000, 6)
			shift = ''
			if tx > rx:
				shift = f' ({round(rx - tx, 6)}MHz)'
			elif tx < rx:
				shift = f' (+{round(rx - tx, 6)}MHz)'
			cc = f' CC{color_code}' if dmr_enabled else ''
			return (str(tx) + 'MHz' + shift + cc) + ','

		return self._get_cached('mmdvm_info', _fetch, ttl=300)


class ScheduledMessageHandler:
	"""Class to handle sending scheduled messages."""

	def __init__(self, cfg):
		self.cfg = cfg
		self.tracking = {}
		self.messages = []
		self._init_messages()

	def _init_messages(self):
		self.messages = []
		definitions = [
			('aprsthursday_enabled', 'APRSThursday', 3, 'ANSRVR', 'CQ HOTG #{}'),
			('aprsmysunday_enabled', 'APRSMYSunday', 6, 'APRSMY', 'CHECK #{}'),
		]
		for attr, name, weekday, addrcall, template_fmt in definitions:
			if getattr(self.cfg, attr, False):
				self.messages.append(
					{'name': name, 'weekday': weekday, 'addrcall': addrcall, 'template': template_fmt.format(name), 'from_call': None}
				)
				if self.cfg.additional_sender:
					for sender in self.cfg.additional_sender:
						self.messages.append(
							{'name': name, 'weekday': weekday, 'addrcall': addrcall, 'template': template_fmt.format(name), 'from_call': sender}
						)

	async def send_all(self, ais, tg_logger, sys_stats):
		"""Send all due scheduled messages."""
		for msg_info in self.messages:
			ais = await self._send_one(ais, tg_logger, sys_stats, **msg_info)
		return ais

	async def _send_one(self, ais, tg_logger, sys_stats, name, weekday, addrcall, template, from_call=None):
		"""Send a single scheduled message to APRS-IS if it's due."""
		now = dt.datetime.now(dt.timezone.utc)
		if now.weekday() != weekday:
			return ais
		today = now.strftime('%Y-%m-%d')
		source = from_call or FROMCALL
		tracking_key = f'{name}:{source}'
		if self.tracking.get(tracking_key) == today:
			return ais
		_, lat, lon, _, _, _ = await _get_current_location_data(self.cfg)
		gridsquare = latlon_to_grid(lat, lon)
		with Sequence(path=MSG_SEQUENCE_FILE, modulo=100000) as seq_mgr:
			seq = seq_mgr.count
		message = f'{template} from ({gridsquare}) via {APP_NAME}, de {source}'
		if len(message) > 67:
			logging.error('Message length %d exceeds APRS limit of 67 characters: %s', len(message), message)
			return ais
		path_str = ''
		if from_call:
			path_str = f',{FROMCALL}'
		payload = f'{source}>{TOCALL}{path_str}::{addrcall:9s}:{message}{{{seq}'
		try:
			parsed = aprslib.parse(payload)
		except APRSParseError as err:
			logging.error('APRS packet parsing error at %s: %s', name, err)
			return ais
		try:
			ais.sendall(payload)
			logging.info(payload)
			tg_msg = f'<u>{parsed["from"]} <b>{name}</b></u>\n\nFrom: <b>{parsed["from"]}</b>\nTo: <b>{parsed["addresse"]}</b>'
			if parsed.get('path'):
				tg_msg += f'\nPath: <b>{parsed["path"]}</b>'
			if parsed.get('via'):
				tg_msg += f'\nvia: <b>{parsed["via"]}</b>'
			tg_msg += f'\nMessage: <b>{parsed["message_text"]}</b>'
			if parsed.get('msgNo'):
				tg_msg += f'\nMessage No: <b>{parsed["msgNo"]}</b>'
			await tg_logger.log(tg_msg, topic_id=self.cfg.telegram_msg_topic_id)
			self.tracking[tracking_key] = today
			ais = await send_status(ais, self.cfg, tg_logger, sys_stats)
		except APRSConnectionError as err:
			logging.error('APRS connection error at %s: %s', name, err)
			ais = await ais_connect(self.cfg)
			ais = await self._send_one(ais, tg_logger, sys_stats, name, weekday, addrcall, template, from_call)
		return ais


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
			msg_kwargs = {
				'chat_id': self.chat_id,
				'text': tg_message,
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


def _fetch_from_gpsd(host, port, filter_class):
	"""Worker function to fetch data from GPSD synchronously."""
	try:
		with GPSDClient(host=host, port=port, timeout=15) as client:
			for result in client.dict_stream(convert_datetime=True, filter=[filter_class]):
				if filter_class == 'TPV':
					if result.get('mode', 0) > 1:
						return result
				else:
					return result
			return None
	except Exception as e:
		return e


async def _retrieve_gpsd_data(cfg, filter_class, log_name):
	"""Retrieve data from GPSD with retries."""
	if not cfg.gpsd_enabled:
		return None
	max_retries = 5
	retry_delay = 1
	loop = asyncio.get_running_loop()
	for attempt in range(max_retries):
		try:
			result = await loop.run_in_executor(None, _fetch_from_gpsd, cfg.gpsd_host, cfg.gpsd_port, filter_class)
			if isinstance(result, Exception):
				raise result
			if result:
				logging.debug('GPS %s acquired', log_name)
				return result
			else:
				logging.warning('GPS %s unavailable, retrying...', log_name)
		except Exception as e:
			logging.error('GPSD (%s) connection error (attempt %d/%d): %s', log_name, attempt + 1, max_retries, e)
		if attempt < max_retries - 1:
			await asyncio.sleep(retry_delay)
			retry_delay *= 5
	logging.warning('Failed to get GPS %s data after %d attempts.', log_name, max_retries)
	return None


def _get_fallback_location(cfg):
	"""Retrieve location from cache or environment variables."""
	lat, lon, alt = 0.0, 0.0, 0.0
	if os.path.exists(GPS_FILE):
		try:
			with open(GPS_FILE, 'r') as f:
				gps_data = json.load(f)
				lat = float(gps_data.get('lat', 0.0))
				lon = float(gps_data.get('lon', 0.0))
				alt = float(gps_data.get('alt', 0.0))
		except (IOError, OSError, json.JSONDecodeError, ValueError) as e:
			logging.warning('Could not read or parse GPS file %s: %s', GPS_FILE, e)
	if lat == 0.0 and lon == 0.0:
		try:
			lat = float(cfg.latitude)
			lon = float(cfg.longitude)
			alt = float(cfg.altitude)
		except ValueError:
			lat, lon, alt = 0.0, 0.0, 0.0
	return lat, lon, alt


def _save_gps_cache(lat, lon, alt):
	"""Save GPS location to cache file."""
	try:
		with open(GPS_FILE, 'w') as f:
			json.dump({'lat': lat, 'lon': lon, 'alt': alt}, f)
	except (IOError, OSError) as e:
		logging.error('Failed to write GPS data to %s: %s', GPS_FILE, e)


async def get_gpspos(cfg):
	"""Get position from GPSD."""
	timestamp = dt.datetime.now(dt.timezone.utc)
	if not cfg.gpsd_enabled:
		return timestamp, 0, 0, 0, 0, 0
	result = await _retrieve_gpsd_data(cfg, 'TPV', 'position')
	if result:
		utc = result.get('time', timestamp)
		lat = result.get('lat', 0.0)
		lon = result.get('lon', 0.0)
		alt = result.get('alt', 0.0)
		spd = result.get('speed', 0)
		cse = result.get('magtrack', 0) or result.get('track', 0)
		logging.debug('%s | GPS Position: %s, %s, %s, %s, %s', utc, lat, lon, alt, spd, cse)
		_save_gps_cache(lat, lon, alt)
		return utc, lat, lon, alt, spd, cse
	logging.warning('Reading from cache.')
	env_lat, env_lon, env_alt = _get_fallback_location(cfg)
	return timestamp, env_lat, env_lon, env_alt, 0, 0


def _lat_to_aprs(lat):
	"""Format latitude for APRS."""
	ns = 'N' if lat >= 0 else 'S'
	lat = abs(lat)
	deg = int(lat)
	minutes = (lat - deg) * 60
	return f'{deg:02d}{minutes:05.2f}{ns}'


def _lon_to_aprs(lon):
	"""Format longitude for APRS."""
	ew = 'E' if lon >= 0 else 'W'
	lon = abs(lon)
	deg = int(lon)
	minutes = (lon - deg) * 60
	return f'{deg:03d}{minutes:05.2f}{ew}'


def _alt_to_aprs(alt):
	"""Format altitude for APRS (meters to feet)."""
	alt_ft = alt / 0.3048 if alt else 0
	alt_ft = max(-999999, alt_ft)
	alt_ft = min(999999, alt_ft)
	return f'/A={alt_ft:06.0f}'


def _spd_to_knots(spd):
	"""Format speed for APRS (mps to knots)."""
	spd_knots = spd / 0.51444 if spd else 0
	spd_knots = max(0, spd_knots)
	spd_knots = min(999, spd_knots)
	return f'{spd_knots:03.0f}'


def _cse_to_aprs(cse):
	"""Format course for APRS."""
	cse = cse % 360 if cse else 0
	cse = max(0, cse)
	cse = min(359, cse)
	return f'{cse:03.0f}'


def _spd_to_kmh(spd):
	"""Format speed for APRS (mps to kmh)."""
	spd_kmh = spd * 3.6 if spd else 0
	spd_kmh = max(0, spd_kmh)
	spd_kmh = min(999, spd_kmh)
	return f'{spd_kmh:03.0f}'


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
	coord_key = f'{lat:.4f},{lon:.4f}'
	if coord_key in cache:
		return cache[coord_key]
	geolocator = Nominatim(user_agent=APP_NAME)
	try:
		location = geolocator.reverse((lat, lon), exactly_one=True, namedetails=True, addressdetails=True)
		if location:
			address = location.raw['address']
			cache[coord_key] = address
			with open(CACHE_FILE, 'wb') as cache_file:
				pickle.dump(cache, cache_file)
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


async def get_gpssat(cfg):
	"""Get satellite from GPSD."""
	timestamp = dt.datetime.now(dt.timezone.utc)
	if not cfg.gpsd_enabled:
		return timestamp, 0, 0
	result = await _retrieve_gpsd_data(cfg, 'SKY', 'satellite')
	if result:
		utc = result.get('time', timestamp)
		uSat = result.get('uSat', 0)
		nSat = result.get('nSat', 0)
		return utc, uSat, nSat
	return timestamp, 0, 0


async def _get_current_location_data(cfg, gps_data=None):
	"""Determines the current location data from GPS or fallback to config.Returns a tuple of (timestamp, lat, lon, alt, spd, cse)."""
	if not gps_data and cfg.gpsd_enabled:
		gps_data = await get_gpspos(cfg)
	if gps_data:
		timestamp, lat, lon, alt, spd, cse = gps_data
		if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and (lat != 0 or lon != 0):
			return timestamp, lat, lon, alt, spd, cse
	lat = float(cfg.latitude)
	lon = float(cfg.longitude)
	alt = float(cfg.altitude)
	return None, lat, lon, alt, 0, 0


async def send_position(ais, cfg, tg_logger, sys_stats, gps_data=None):
	"""Send APRS position packet to APRS-IS."""
	cur_time, cur_lat, cur_lon, cur_alt, cur_spd, cur_cse = await _get_current_location_data(cfg, gps_data)
	latstr = _lat_to_aprs(cur_lat)
	lonstr = _lon_to_aprs(cur_lon)
	altstr = _alt_to_aprs(cur_alt)
	spdstr = _spd_to_knots(cur_spd)
	csestr = _cse_to_aprs(cur_cse)
	spdkmh = _spd_to_kmh(cur_spd)
	mmdvminfo = sys_stats.mmdvm_info()
	osinfo = sys_stats.os_info()
	comment = f'{mmdvminfo}{osinfo} {PROJECT_URL}'
	ztime = dt.datetime.now(dt.timezone.utc)
	timestamp = cur_time.strftime('%d%H%Mz') if cur_time else ztime.strftime('%d%H%Mz')
	symbt = cfg.symbol_table
	symb = cfg.symbol
	if cfg.symbol_overlay:
		symbt = cfg.symbol_overlay
	tgposmoving = ''
	extdatstr = ''
	if cur_spd > 0:
		extdatstr = f'{csestr}/{spdstr}'
		tgposmoving = f'\n\tSpeed: <b>{int(cur_spd)}m/s</b> | <b>{int(spdkmh)}km/h</b> | <b>{int(spdstr)}kn</b>\n\tCourse: <b>{int(cur_cse)}°</b>'
		if cfg.smartbeaconing_enabled:
			sspd = cfg.smartbeaconing_slow_speed
			fspd = cfg.smartbeaconing_fast_speed
			kmhspd = int(spdkmh)
			if kmhspd > fspd:
				symbt, symb = '\\', '>'
			elif sspd < kmhspd <= fspd:
				symbt, symb = '/', '>'
			elif 0 < kmhspd <= sspd:
				symbt, symb = '/', '('
	lookup_table = symbt if symbt in ['/', '\\'] else '\\'
	sym_desc = symbols.get_desc(lookup_table, symb).split('(')[0].strip()
	payload = f'@{timestamp}{latstr}{symbt}{lonstr}{symb}{extdatstr}{altstr}{comment}'
	posit = f'{FROMCALL}>{TOCALL}:{payload}'
	tgpos = f'<u>{FROMCALL} Position</u>\n\nTime: <b>{timestamp}</b>\nSymbol: {symbt}{symb} ({sym_desc})\nPosition:\n\tLatitude: <b>{cur_lat}</b>\n\tLongitude: <b>{cur_lon}</b>\n\tAltitude: <b>{cur_alt}m</b>{tgposmoving}\nComment: <b>{comment}</b>'
	try:
		ais.sendall(posit)
		logging.info(posit)
		await tg_logger.log(tgpos, cur_lat, cur_lon, int(csestr))
		await send_status(ais, cfg, tg_logger, sys_stats, gps_data)
	except APRSConnectionError as err:
		logging.error('APRS connection error at position: %s', err)
		ais = await ais_connect(cfg)
		ais = await send_position(ais, cfg, tg_logger, sys_stats, gps_data)
	return ais


async def send_header(ais, cfg, tg_logger, sys_stats):
	"""Send APRS header information to APRS-IS."""
	caller = f'{FROMCALL}>{TOCALL}::{FROMCALL:9s}:'
	params = ['CPUTemp', 'CPULoad', 'RAMUsed', 'DiskUsed']
	units = ['deg.C', '%', 'GB', 'GB']
	eqns = ['0,0.1,0', '0,0.001,0', '0,0.001,0', '0,0.001,0']
	if cfg.gpsd_enabled:
		params.append('GPSUsed')
		units.append('sats')
		eqns.append('0,1,0')
	payload = f'{caller}PARM.{",".join(params)}\r\n{caller}UNIT.{",".join(units)}\r\n{caller}EQNS.{",".join(eqns)}'
	tg_msg = f'<u>{FROMCALL} Header</u>\n\nParameters: <b>{",".join(params)}</b>\nUnits: <b>{",".join(units)}</b>\nEquations: <b>{",".join(eqns)}</b>\n\nValue: <code>[a,b,c]=(a×v²)+(b×v)+c</code>'
	try:
		ais.sendall(payload)
		logging.info(payload)
		await tg_logger.log(tg_msg)
		await send_status(ais, cfg, tg_logger, sys_stats)
	except APRSConnectionError as err:
		logging.error('APRS connection error at header: %s', err)
		ais = await ais_connect(cfg)
		ais = await send_header(ais, cfg, tg_logger, sys_stats)
	return ais


async def send_telemetry(ais, cfg, tg_logger, sys_stats):
	"""Send APRS telemetry information to APRS-IS."""
	with Sequence() as seq_mgr:
		seq = seq_mgr.count
	temp = sys_stats.cur_temp()
	cpuload = sys_stats.avg_cpu_load()
	memused = sys_stats.memory_used()
	diskused = sys_stats.storage_used()
	telemmemused = int(memused / 1.0000e6)
	telemdiskused = int(diskused / 1.0000e6)
	telem = f'{FROMCALL}>{TOCALL}:T#{seq:03d},{temp:d},{cpuload:d},{telemmemused:d},{telemdiskused:d}'
	tgtel = (
		f'<u>{FROMCALL} Telemetry</u>\n\n'
		f'Sequence: <b>#{seq}</b>\n'
		f'CPU Temp: <b>{temp / 10:.1f} °C</b>\n'
		f'CPU Load: <b>{cpuload / 1000:.1f}%</b>\n'
		f'RAM Used: <b>{humanize.naturalsize(memused, binary=True)}</b>\n'
		f'Disk Used: <b>{humanize.naturalsize(diskused, binary=True)}</b>'
	)
	if cfg.gpsd_enabled:
		_, uSat, _ = await get_gpssat(cfg)
		telem += f',{uSat:d}'
		tgtel += f'\nGPS Used: <b>{uSat}</b>'
	try:
		ais.sendall(telem)
		logging.info(telem)
		await tg_logger.log(tgtel)
		await send_status(ais, cfg, tg_logger, sys_stats)
	except APRSConnectionError as err:
		logging.error('APRS connection error at telemetry: %s', err)
		ais = await ais_connect(cfg)
		ais = await send_telemetry(ais, cfg, tg_logger, sys_stats)
	return ais


async def send_status(ais, cfg, tg_logger, sys_stats, gps_data=None):
	"""Send APRS status information to APRS-IS."""
	_, lat, lon, _, _, _ = await _get_current_location_data(cfg, gps_data)
	gridsquare = latlon_to_grid(lat, lon)
	address = get_add_from_pos(lat, lon)
	near_add = format_address(address)
	near_add_tg = format_address(address, True)
	ztime = dt.datetime.now(dt.timezone.utc)
	timestamp = ztime.strftime('%d%H%Mz')
	sats_info = ''
	if cfg.gpsd_enabled:
		timez, u_sat, n_sat = await get_gpssat(cfg)
		if u_sat > 0:
			timestamp = timez.strftime('%d%H%Mz')
			sats_info = f', gps: {u_sat}/{n_sat}'
		else:
			sats_info = f', gps: {u_sat}'
	uptime = sys_stats.uptime()
	status_text = f'{timestamp}[{gridsquare}]{near_add} {uptime}{sats_info}'
	aprs_packet = f'{FROMCALL}>{TOCALL}:>{status_text}'
	tg_msg = f'<u>{FROMCALL} Status</u>\n\n<b>{timestamp}[{gridsquare}]{near_add_tg} {uptime}{sats_info}</b>'
	if os.path.exists(STATUS_FILE):
		try:
			with open(STATUS_FILE, 'r') as f:
				if f.read() == aprs_packet:
					return ais
		except (IOError, OSError):
			pass
	try:
		ais.sendall(aprs_packet)
		logging.info(aprs_packet)
		try:
			with open(STATUS_FILE, 'w') as f:
				f.write(aprs_packet)
		except (IOError, OSError):
			pass
		await tg_logger.log(tg_msg)
	except APRSConnectionError as err:
		logging.error('APRS connection error at status: %s', err)
		ais = await ais_connect(cfg)
		ais = await send_status(ais, cfg, tg_logger, sys_stats, gps_data)
	return ais


async def ais_connect(cfg):
	"""Establish connection to APRS-IS with retries."""
	logging.info('Connecting to APRS-IS server %s:%d as %s', cfg.server, cfg.port, FROMCALL)
	ais = aprslib.IS(FROMCALL, passwd=cfg.passcode, host=cfg.server, port=cfg.port)
	loop = asyncio.get_running_loop()
	max_retries = 5
	retry_delay = 5
	for attempt in range(max_retries):
		try:
			await loop.run_in_executor(None, ais.connect)
			# ais.set_filter(cfg.filter)
			logging.info('Connected to APRS-IS server %s:%d as %s', cfg.server, cfg.port, FROMCALL)
			return ais
		except APRSConnectionError as err:
			logging.warning('APRS connection error (attempt %d/%d): %s', attempt + 1, max_retries, err)
			if attempt < max_retries - 1:
				await asyncio.sleep(retry_delay)
				retry_delay = min(retry_delay * 2, 60)
	logging.error('Connection error, exiting')
	sys.exit(getattr(os, 'EX_NOHOST', 1))


def should_send_position(cfg, timer_tick, sb, gps_data):
	"""Determine if a position update is needed."""
	return (cfg.gpsd_enabled and cfg.smartbeaconing_enabled and sb.should_send(gps_data)) or (timer_tick % 1200 == 1)


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
		cfg.latitude, cfg.longitude = await get_coordinates()
	if cfg.gpsd_enabled:
		gps_data = await get_gpspos(cfg)
		cfg.timestamp, cfg.latitude, cfg.longitude, cfg.altitude, cfg.speed, cfg.course = gps_data
	ais = await ais_connect(cfg)
	tg_logger = TelegramLogger(cfg)
	timer = Timer()
	sb = SmartBeaconing(cfg)
	sys_stats = SystemStats()
	scheduled_msg_handler = ScheduledMessageHandler(cfg)
	return ais, tg_logger, timer, sb, sys_stats, scheduled_msg_handler


async def process_loop(cfg, ais, tg_logger, timer, sb, sys_stats, reload_event, scheduled_msg_handler):
	"""Run the main processing loop."""
	while True:
		with timer:
			timer_tick = timer.count
		if reload_event.is_set():
			break
		gps_data = None
		if cfg.gpsd_enabled:
			gps_data = await get_gpspos(cfg)
		if should_send_position(cfg, timer_tick, sb, gps_data):
			ais = await send_position(ais, cfg, tg_logger, sys_stats, gps_data=gps_data)
		if timer_tick % 14400 == 1:
			ais = await send_header(ais, cfg, tg_logger, sys_stats)
		if timer_tick % cfg.sleep == 1:
			ais = await send_telemetry(ais, cfg, tg_logger, sys_stats)
		ais = await scheduled_msg_handler.send_all(ais, tg_logger, sys_stats)
		await asyncio.sleep(0.5)


async def main():
	"""Main function to run the APRS reporting loop."""
	reload_event = asyncio.Event()
	setup_signal_handling(reload_event)
	cfg = Config()
	while True:
		reload_event.clear()
		ais, tg_logger, timer, sb, sys_stats, scheduled_msg_handler = await initialize_session(cfg)
		async with tg_logger:
			await tg_logger.log(f'🚀 <b>{FROMCALL}</b>, {APP_NAME} starting up...')
			try:
				await process_loop(cfg, ais, tg_logger, timer, sb, sys_stats, reload_event, scheduled_msg_handler)
			finally:
				if reload_event.is_set():
					await tg_logger.log(f'🔄 <b>{FROMCALL}</b>, {APP_NAME} reloading configuration...')
				else:
					await tg_logger.log(f'🛑 <b>{FROMCALL}</b>, {APP_NAME} shutting down...')
				await tg_logger.stop_location()
				if ais:
					try:
						ais.close()
					except Exception:
						pass
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
