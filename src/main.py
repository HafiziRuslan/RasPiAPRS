#!/usr/bin/python3

# 	Send APRS position and telemetry from Raspberry Pi to APRS-IS.
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

import asyncio
import contextlib
import datetime as dt
import json
import logging
import logging.handlers
import math
import os
import pickle
import platform
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from collections import UserDict
from collections import deque
from dataclasses import dataclass
from typing import Callable
from typing import NamedTuple

import aiohttp
import aprslib
import aprslib.util
import dotenv
import humanize
import psutil
import symbols
import telegram
from aprslib.exceptions import ConnectionError as APRSConnectionError
from aprslib.exceptions import ParseError as APRSParseError
from geopy.geocoders import Nominatim
from gpsdclient import GPSDClient
from itu_appendix42 import ItuAppendix42


@dataclass
class Config:
	tmp_dir: str = '/var/tmp/RasPiAPRS'
	log_dir: str = '/var/log/RasPiAPRS'
	lib_dir: str = '/var/lib/RasPiAPRS'
	mmdvmhost_file: str = ''
	gps_file: str = f'{tmp_dir}/gps.json'
	location_id_file: str = f'{tmp_dir}/location_id.tmp'
	status_file: str = f'{tmp_dir}/status.tmp'
	msg_tracking_file: str = f'{lib_dir}/msg_tracking.pkl'
	nominatim_cache_file: str = f'{lib_dir}/nominatim_cache.pkl'
	app_name: str = 'RasPiAPRS'
	project_url: str = 'https://git.new/RasPiAPRS'
	sleep: int = 600
	call: str = 'N0CALL'
	aprs_passcode: str | int = -1
	ssid: int = 0
	from_call: str = call
	to_call: str = 'APP642'
	altitude: float = 0.0
	latitude: float = 0.0
	longitude: float = 0.0
	symbol: str = 'n'
	symbol_table: str = '/'
	symbol_overlay: str | None = None
	aprsis_server: str = 'rotate.aprs2.net'
	aprsis_port: int = 14580
	aprsis_filter: str | None = None
	phg_power: float | None = 0.1
	phg_height: float | None = 5
	phg_gain: float | None = 3
	phg_direction: float | None = 0
	gpsd_enabled: bool = False
	gpsd_host: str | None = 'localhost'
	gpsd_port: int | None = 2947
	gpsd_sock: str | None = None
	smartbeaconing_enabled: bool = False
	smartbeaconing_fast_rate: int = 60
	smartbeaconing_fast_speed: int = 100
	smartbeaconing_min_turn_angle: int = 28
	smartbeaconing_min_turn_time: int = 5
	smartbeaconing_slow_rate: int = 600
	smartbeaconing_slow_speed: int = 10
	smartbeaconing_turn_slope: int = 255
	telegram_enabled: bool = False
	telegram_token: str | None = None
	telegram_chat_id: str | None = None
	telegram_topic_id: int | None = None
	telegram_loc_topic_id: int | None = None
	telegram_msg_topic_id: int | None = None
	aprsphnet_enabled: bool = False
	aprsthursday_enabled: bool = False
	aprsaturday_enabled: bool = False
	aprsmysunday_enabled: bool = False
	aprshamfinity_enabled: bool = False
	additional_sender: list[str] | None = None
	additional_sender_raw: str | None = None
	log_level_raw: int = 2
	log_max_bytes: float = 1.0
	log_max_count: int = 3
	_env_mtime: float = 0.0

	def __post_init__(self):
		self.app_name, self.project_url = self.get_app_metadata()
		self.reload()

	@staticmethod
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
		return f'{"/".join(filter(None, [meta["name"], meta["version"], git_sha]))}', meta['github']

	@staticmethod
	def _env_get_bool(key: str, default: str = 'False') -> bool:
		return os.getenv(key, default).lower() in ('true', '1', 't', 'y', 'yes')

	@staticmethod
	def _env_get_float(key: str, default: float) -> float:
		val = os.getenv(key)
		if val is None:
			return default
		try:
			return float(val)
		except (ValueError, TypeError):
			return default

	@staticmethod
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

	@staticmethod
	def _env_get_int_or_none(key: str) -> int | None:
		val = os.getenv(key)
		if val is None:
			return None
		try:
			return int(val)
		except (ValueError, TypeError):
			return None

	@staticmethod
	@contextlib.contextmanager
	def _atomic_write(file_path: str):
		"""Context manager for atomic file writing, preserving permissions and ownership."""
		abs_path = os.path.abspath(file_path)
		fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(abs_path), text=True)
		try:
			with os.fdopen(fd, 'w') as f_tmp:
				yield f_tmp
			if os.path.exists(file_path):
				shutil.copymode(file_path, temp_path)
				if hasattr(os, 'geteuid') and os.geteuid() == 0:
					st = os.stat(file_path)
					shutil.chown(temp_path, user=st.st_uid, group=st.st_gid)
			os.replace(temp_path, file_path)
		except Exception:
			if os.path.exists(temp_path):
				os.remove(temp_path)
			raise

	def reload(self):
		"""Reload configuration from environment variables."""
		env_file = '.env'
		try:
			current_mtime = os.path.getmtime(env_file)
		except OSError:
			current_mtime = 0.0
		if self._env_mtime != 0.0 and current_mtime <= self._env_mtime and self.mmdvmhost_file:
			return

		self._env_mtime = current_mtime
		dotenv.load_dotenv(env_file, override=True)
		self.log_level_raw = self._env_get_int('LOG_LEVEL', 2)
		self.log_max_bytes = self._env_get_float('LOG_MAX_BYTES', 1)
		self.log_max_count = self._env_get_int('LOG_MAX_COUNT', 3)
		self.call = os.getenv('APRS_CALL', 'N0CALL')
		self.ssid = self._env_get_int('APRS_SSID', 0, 'SSID value error')
		self.sleep = self._env_get_int('SLEEP', 600, 'Sleep value error')
		self.symbol_table = os.getenv('APRS_SYMBOL_TABLE', '/')
		self.symbol = os.getenv('APRS_SYMBOL', 'n')
		self.latitude = self._env_get_float('APRS_LATITUDE', 0.0)
		self.longitude = self._env_get_float('APRS_LONGITUDE', 0.0)
		self.altitude = self._env_get_float('APRS_ALTITUDE', 0.0)
		mmdvm_file_from_env = os.getenv('MMDVMHOST_FILE')
		if mmdvm_file_from_env:
			self.mmdvmhost_file = mmdvm_file_from_env
		else:
			self.mmdvmhost_file = ''
			for proc in psutil.process_iter(['name', 'cmdline']):
				if proc.info['name'] == 'MMDVMHost' and proc.info['cmdline']:
					for arg in proc.info['cmdline']:
						if 'MMDVM.ini' in arg or 'mmdvmhost' in arg:
							if os.path.isfile(arg) and os.access(arg, os.R_OK):
								self.mmdvmhost_file = arg
								logging.info('Found MMDVMHost configuration from active process: %s', self.mmdvmhost_file)
								break
					if self.mmdvmhost_file:
						break
			if not self.mmdvmhost_file:
				for p in ['/etc/mmdvmhost', '/etc/MMDVM.ini', '/opt/MMDVMHost/MMDVM.ini']:
					if os.path.isfile(p) and os.access(p, os.R_OK):
						self.mmdvmhost_file = p
						break
		self.phg_power = self._env_get_float('PHG_POWER', 0.1)
		self.phg_height = self._env_get_float('PHG_HEIGHT', 5.0)
		self.phg_gain = self._env_get_float('PHG_GAIN', 3)
		self.phg_direction = self._env_get_float('PHG_DIRECTION', 0)
		self.aprsis_server = os.getenv('APRSIS_SERVER', 'rotate.aprs2.net')
		self.aprsis_port = self._env_get_int('APRSIS_PORT', 14580, 'APRSIS Port value error')
		self.aprsis_filter = os.getenv('APRSIS_FILTER')
		self.aprs_passcode = os.getenv('APRS_PASSCODE')
		self.gpsd_enabled = self._env_get_bool('GPSD_ENABLE')
		if self.gpsd_enabled:
			self.gpsd_host = os.getenv('GPSD_HOST')
			self.gpsd_port = self._env_get_int_or_none('GPSD_PORT')
			self.gpsd_sock = os.getenv('GPSD_SOCK')
			if not self.gpsd_sock and not (self.gpsd_host and self.gpsd_port):
				self.gpsd_host = 'localhost'
				self.gpsd_port = 2947
		self.smartbeaconing_enabled = self._env_get_bool('SMARTBEACONING_ENABLE')
		if self.smartbeaconing_enabled:
			self.smartbeaconing_fast_speed = self._env_get_int('SMARTBEACONING_FASTSPEED', 100)
			self.smartbeaconing_slow_speed = self._env_get_int('SMARTBEACONING_SLOWSPEED', 10)
			self.smartbeaconing_fast_rate = self._env_get_int('SMARTBEACONING_FASTRATE', 60)
			self.smartbeaconing_slow_rate = self._env_get_int('SMARTBEACONING_SLOWRATE', 600)
			self.smartbeaconing_min_turn_angle = self._env_get_int('SMARTBEACONING_MINTURNANGLE', 28)
			self.smartbeaconing_turn_slope = self._env_get_int('SMARTBEACONING_TURNSLOPE', 255)
			self.smartbeaconing_min_turn_time = self._env_get_int('SMARTBEACONING_MINTURNTIME', 5)
		self.telegram_enabled = self._env_get_bool('TELEGRAM_ENABLE')
		if self.telegram_enabled:
			self.telegram_token = os.getenv('TELEGRAM_TOKEN')
			self.telegram_chat_id = os.getenv('TELEGRAM_CHAT_ID')
			self.telegram_topic_id = self._env_get_int_or_none('TELEGRAM_TOPIC_ID')
			self.telegram_msg_topic_id = self._env_get_int_or_none('TELEGRAM_MSG_TOPIC_ID')
			self.telegram_loc_topic_id = self._env_get_int_or_none('TELEGRAM_LOC_TOPIC_ID')
		self.aprsphnet_enabled = self._env_get_bool('APRSPHNET_ENABLE')
		self.aprsthursday_enabled = self._env_get_bool('APRSTHURSDAY_ENABLE')
		self.aprsaturday_enabled = self._env_get_bool('APRSATURDAY_ENABLE')
		self.aprsmysunday_enabled = self._env_get_bool('APRSMYSUNDAY_ENABLE')
		self.aprshamfinity_enabled = self._env_get_bool('APRSHAMFINITY_ENABLE')
		self.additional_sender_raw = os.getenv('ADDITIONAL_SENDER')
		self.validate()

	def validate(self):
		"""Validate and normalize configuration values."""
		if not (1 <= self.ssid <= 15):
			self.ssid = 0
		self.from_call = self.call if self.ssid == 0 else f'{self.call}-{self.ssid}'
		if self.symbol_table not in ['/', '\\']:
			self.symbol_overlay = self.symbol_table
		else:
			self.symbol_overlay = None
		if not self.aprs_passcode:
			logging.warning('No passcode provided. Generating one.')
			self.aprs_passcode = aprslib.passcode(self.call)
		self.additional_sender = None
		events_active = any(
			[self.aprsphnet_enabled, self.aprsthursday_enabled, self.aprsaturday_enabled, self.aprsmysunday_enabled, self.aprshamfinity_enabled]
		)
		if events_active and self.additional_sender_raw:
			ituappendix42 = ItuAppendix42()
			valid_senders = []
			raw_senders = self.additional_sender_raw.split(',')
			needs_cleanup = False
			for sender in raw_senders:
				sender = sender.strip().upper()
				if not sender:
					continue
				base, ssid_str = sender.rsplit('-', 1) if '-' in sender else (sender, None)
				is_valid = False
				if ituappendix42.fullmatch(base):
					if ssid_str is None or (ssid_str.isdigit() and 0 <= int(ssid_str) <= 15):
						is_valid = True
				if is_valid:
					valid_senders.append(sender)
				else:
					logging.warning('Invalid ITU callsign format: %s. Removing from configuration.', sender)
					needs_cleanup = True
			if valid_senders:
				self.additional_sender = valid_senders
			if needs_cleanup:
				self._cleanup_env_senders(valid_senders)
		all_callsigns_for_group_filter = []
		if self.from_call:
			all_callsigns_for_group_filter.append(self.from_call)
		if self.additional_sender:
			all_callsigns_for_group_filter.extend(self.additional_sender)
		unique_filter_parts = set()
		if all_callsigns_for_group_filter:
			group_filter_string = 'g/' + '/'.join(sorted(set(all_callsigns_for_group_filter)))
			unique_filter_parts.add(group_filter_string)
		if self.aprsis_filter and self.aprsis_filter.strip():
			for part in self.aprsis_filter.strip().split():
				if part.strip():
					unique_filter_parts.add(part.strip())
		if unique_filter_parts:
			self.aprsis_filter = ' '.join(sorted(list(unique_filter_parts)))
		else:
			self.aprsis_filter = None
		logging.debug('Final constructed APRS-IS filter: %s', self.aprsis_filter)

	def _cleanup_env_senders(self, valid_senders: list[str]):
		"""Update .env file to remove invalid senders."""
		env_path = '.env'
		if not os.path.exists(env_path):
			return
		try:
			with open(env_path, 'r') as f:
				lines = f.readlines()
			new_lines = []
			for line in lines:
				if line.strip().startswith('ADDITIONAL_SENDER='):
					comment = ''
					if '#' in line:
						comment = ' ' + line[line.find('#') :].strip()
					new_lines.append(f'ADDITIONAL_SENDER={",".join(valid_senders)}{comment}\n')
				else:
					new_lines.append(line)
			with self._atomic_write(env_path) as f_tmp:
				f_tmp.writelines(new_lines)
			logging.info('Cleaned up invalid senders in %s', env_path)
		except Exception as e:
			logging.error('Failed to cleanup .env file: %s', e)


def configure_logging(cfg: Config):
	"""Sets up logging."""
	log_dir = cfg.log_dir
	if not os.path.exists(log_dir) or not os.access(log_dir, os.W_OK):
		log_dir = 'logs'
	os.makedirs(log_dir, exist_ok=True)
	logger = logging.getLogger()
	for handler in logger.handlers[:]:
		logger.removeHandler(handler)
	log_level_map = {
		0: 100,  # OFF
		1: logging.DEBUG,
		2: logging.INFO,
		3: logging.WARNING,
		4: logging.ERROR,
		5: logging.CRITICAL,
	}
	log_level = log_level_map.get(cfg.log_level_raw)
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
	max_bytes = cfg.log_max_bytes * 1024 * 1024
	max_count = cfg.log_max_count
	for level, filename in log_files.items():
		if level < log_level:
			continue
		try:
			path = os.path.join(log_dir, filename)
			handler = NumberedRotatingFileHandler(path, maxBytes=max_bytes, backupCount=max_count)
			handler.setLevel(level)
			handler.addFilter(LevelFilter(level))
			handler.setFormatter(formatter)
			logger.addHandler(handler)
		except (OSError, PermissionError) as e:
			logging.error('Failed to create %s: %s', filename, e)


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

	def __init__(self, lib_dir, name='sequence', modulo=100):
		super().__init__(f'{lib_dir}/{name}.seq', modulo)


class Timer(PersistentCounter):
	"""Class to manage persistent timer."""

	def __init__(self, tmp_dir, name='timer', modulo=86400):
		super().__init__(f'{tmp_dir}/{name}.tmr', modulo)


class APRSConverter:
	"""Utility class for APRS data conversions."""

	@staticmethod
	def lat_to_aprs(lat):
		"""Format latitude for APRS."""
		return aprslib.util.latitude_to_ddm(lat)

	@staticmethod
	def lon_to_aprs(lon):
		"""Format longitude for APRS."""
		return aprslib.util.longitude_to_ddm(lon)

	@staticmethod
	def alt_to_aprs(alt):
		"""Format altitude for APRS."""
		return aprslib.util.comment_altitude(alt)

	@staticmethod
	def cse_to_aprs(cse):
		"""Format course for APRS."""
		cse = cse % 360 if cse else 0
		cse = max(0, cse)
		cse = min(359, cse)
		return f'{cse:03.0f}'

	@staticmethod
	def _format_speed(val):
		"""Format speed for APRS with clamping."""
		val = max(0, min(999, val))
		return f'{val:03.0f}'

	@classmethod
	def spd_to_kmh(cls, spd):
		"""Format speed for APRS (mps to kmh)."""
		val = spd * 3.6 if spd else 0
		return cls._format_speed(val)

	@classmethod
	def spd_to_knot(cls, spd):
		"""Format speed for APRS (mps to knots)."""
		val = spd / 0.51444 if spd else 0
		return cls._format_speed(val)

	@staticmethod
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
			grid += chr(subsq_lon + ord('a')) + chr(subsq_lat + ord('a'))
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
		fallback_lat, fallback_lon, fallback_alt = self._get_fallback_location()
		self._current_pos = GPSFix(dt.datetime.now(dt.timezone.utc), fallback_lat, fallback_lon, fallback_alt, 0.0, 0.0)
		self._current_sat = SATFix(dt.datetime.now(dt.timezone.utc), 0, 0)
		self.last_valid_pos = None

	def _parse_gps_time(self, raw_time):
		"""Parse ISO8601 time string from GPSD to datetime object."""
		if isinstance(raw_time, str):
			try:
				return dt.datetime.fromisoformat(raw_time.replace('Z', '+00:00'))
			except ValueError:
				pass
		return dt.datetime.now(dt.timezone.utc)

	def _fetch_from_gpsd(self, filter_class):
		"""Worker function to fetch data from GPSD synchronously."""
		host = self.cfg.gpsd_host or 'localhost'
		port = self.cfg.gpsd_port or 2947
		sock_path = self.cfg.gpsd_sock
		client = None
		sock = None
		try:
			if sock_path:
				import socket

				sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
				sock.settimeout(10)
				sock.connect(sock_path)
				sock.sendall(b'?WATCH={"enable":true,"json":true}\n')
				sock.sendall(b'?POLL;\n')
				lines = sock.makefile('r', encoding='utf-8')
			else:
				client = GPSDClient(host=host, port=port, timeout=10)
				lines = client.gpsd_lines()
			for i, line in enumerate(lines):
				if not sock_path and i == 1:
					if hasattr(client, 'sock') and client.sock:
						client.sock.sendall(b'?POLL;\n')
				answ = line.strip()
				if not answ or answ.startswith('{"class":"VERSION"'):
					if filter_class == 'VERSION' and answ.startswith('{"class":"VERSION"'):
						return json.loads(answ)
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
		finally:
			if sock:
				sock.close()
			if client:
				client.close()

	async def _retrieve_data(self, filter_class, log_name):
		"""Retrieve data from GPSD via executor to prevent blocking."""
		if not self.cfg.gpsd_enabled:
			return None
		loop = asyncio.get_running_loop()
		max_retries = 3
		retry_delay = 1
		for attempt in range(max_retries):
			try:
				result = await loop.run_in_executor(None, self._fetch_from_gpsd, filter_class)
				if result:
					if not self.healthy:
						logging.info('GPSD (%s) connection restored.', log_name)
					self.healthy = True
					self.unhealthy_warning_sent = False
					return result
				logging.debug('GPS %s data currently unavailable (attempt %d/%d).', log_name, attempt + 1, max_retries)
				break
			except (ConnectionError, OSError, TimeoutError) as e:
				if self.healthy or not self.unhealthy_warning_sent:
					logging.error('GPSD (%s) connection error (attempt %d/%d): %s', log_name, attempt + 1, max_retries, e)
					self.unhealthy_warning_sent = True
				self.healthy = False
				if attempt < max_retries - 1:
					await asyncio.sleep(retry_delay)
					retry_delay = min(retry_delay * 2, 10)
					continue
				else:
					logging.error('GPSD (%s) all retry attempts failed.', log_name)
			except Exception as e:
				logging.error('GPSD (%s) unexpected error: %s', log_name, e, exc_info=True)
				self.healthy = False
				self.unhealthy_warning_sent = True
				break
		return None

	async def run_polling(self):
		"""Continuously poll GPSD for data in the background."""
		if not self.cfg.gpsd_enabled:
			return
		while True:
			if not self.healthy:
				await asyncio.sleep(15)
				continue
			pos_res = await self._retrieve_data('TPV', 'position')
			if pos_res:
				self._current_pos = GPSFix(
					timestamp=self._parse_gps_time(pos_res.get('time')),
					lat=pos_res.get('lat', 0.0),
					lon=pos_res.get('lon', 0.0),
					alt=pos_res.get('alt', 0.0),
					spd=pos_res.get('speed', 0.0),
					cse=pos_res.get('magtrack', 0.0) or pos_res.get('track', 0.0),
				)
				self.last_valid_pos = self._current_pos
				self._save_cache(self._current_pos.lat, self._current_pos.lon, self._current_pos.alt)
				logging.debug(
					'GPSD pos data: [time: %s, lat: %f, lon: %f, alt: %0.1f, spd: %0.0f, cse: %0.0f]',
					self._current_pos.timestamp.astimezone().isoformat(timespec='seconds'),
					self._current_pos.lat,
					self._current_pos.lon,
					self._current_pos.alt,
					self._current_pos.spd,
					self._current_pos.cse,
				)
			sat_res = await self._retrieve_data('SKY', 'satellite')
			if sat_res:
				self._current_sat = SATFix(
					timestamp=self._parse_gps_time(sat_res.get('time')), uSat=sat_res.get('uSat', 0), nSat=sat_res.get('nSat', 0)
				)
				logging.debug(
					'GPSD sat data: [time: %s, uSat: %0.0f, nSat: %0.0f]',
					self._current_sat.timestamp.astimezone().isoformat(timespec='seconds'),
					self._current_sat.uSat,
					self._current_sat.nSat,
				)
			await asyncio.sleep(1)

	def _get_fallback_location(self):
		"""Retrieve location from cache or config (Static I/O)."""
		try:
			gps_cache = PersistentDict(self.cfg.gps_file)
			lat = float(gps_cache.get('lat', self.cfg.latitude))
			lon = float(gps_cache.get('lon', self.cfg.longitude))
			alt = float(gps_cache.get('alt', self.cfg.altitude))
			return lat, lon, alt
		except (ValueError, TypeError):
			return self.cfg.latitude, self.cfg.longitude, self.cfg.altitude

	def _save_cache(self, lat, lon, alt):
		"""Save GPS location to cache file."""
		try:
			cache = PersistentDict(self.cfg.gps_file)
			cache.update({'lat': lat, 'lon': lon, 'alt': alt})
			cache.flush()
		except Exception as e:
			logging.debug('Failed to save GPS cache: %s', e)

	async def get_loc_and_sat(self, gps_data=None):
		"""Returns current data immediately from memory."""
		if gps_data:
			pos, sat = gps_data
		else:
			now = dt.datetime.now(dt.timezone.utc)
			if (now - max(self._current_pos.timestamp, self._current_sat.timestamp)).total_seconds() > 600:
				lat, lon, alt = self._get_fallback_location()
				self._current_pos = GPSFix(now, lat, lon, alt, 0.0, 0.0)
				self._current_sat = SATFix(now, 0, 0)
			pos, sat = self._current_pos, self._current_sat
		dist = self.calculate_distance(pos.lat, pos.lon, self.cfg.latitude, self.cfg.longitude)
		if dist <= 50:
			pos = pos._replace(lat=self.cfg.latitude, lon=self.cfg.longitude, alt=self.cfg.altitude)
		return pos, sat

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
				if result:
					if not self.healthy:
						logging.info('GPSD connection restored.')
						self.unhealthy_warning_sent = False
					self.healthy = True
				else:
					raise ConnectionError('Empty response from GPSD')
			except (ConnectionError, OSError, TimeoutError, Exception) as e:
				if self.healthy:
					logging.warning('GPSD connection lost or failed: %s', e)
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
		delta_phi = math.radians(lon2 - lon1)
		delta_lambda = math.radians(lon2 - lon1)
		a = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
		c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
		return R * c


class Geolocation:
	"""Class to handle reverse geocoding logic."""

	def __init__(self, app_name, project_url, cache_file):
		self._app_name = app_name
		self._project_url = project_url
		self._cache = PersistentDict(cache_file)
		self._geolocator = None

	def get_address(self, lat, lon):
		"""Get address from coordinates, using a local cache."""
		coord_key = f'{lat:.4f},{lon:.4f}'
		if coord_key in self._cache:
			return self._cache[coord_key]
		if self._geolocator is None:
			self._geolocator = Nominatim(user_agent=f'{self._app_name} (+{self._project_url})', timeout=10)
		try:
			location = self._geolocator.reverse((lat, lon), exactly_one=True, namedetails=True, addressdetails=True)
			if location:
				address = location.raw['address']
				self._cache[coord_key] = address
				self._cache.flush()
				logging.debug('Address cached for requested coordinates: %s', coord_key)
				return address
			logging.warning('No address found for provided coordinates: %s', coord_key)
			return None
		except Exception as e:
			logging.error('Error getting address: %s', e)
			return None

	@staticmethod
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
		self.stop_time = 0
		self.sb_fspd = self.cfg.smartbeaconing_fast_speed
		self.sb_frat = self.cfg.smartbeaconing_fast_rate
		self.sb_sspd = self.cfg.smartbeaconing_slow_speed
		self.sb_srat = self.cfg.smartbeaconing_slow_rate
		self.sb_mtt = self.cfg.smartbeaconing_min_turn_time
		self.sb_mta = self.cfg.smartbeaconing_min_turn_angle
		self.sb_tsl = self.cfg.smartbeaconing_turn_slope
		self.symbt = self.cfg.symbol_table
		self.symb = self.cfg.symbol

	def _calculate_rate(self, spd_kmh):
		"""Calculate beacon rate based on speed and determine symbols."""
		if spd_kmh >= self.sb_fspd:
			return self.sb_frat, '\\', '>'
		if spd_kmh <= self.sb_sspd:
			return self.sb_srat, '/', '('
		rate = int(self.sb_srat - ((spd_kmh - self.sb_sspd) * (self.sb_srat - self.sb_frat) / (self.sb_fspd - self.sb_sspd)))
		return rate, '/', '>'

	def _check_turn(self, cse, spd_kmh):
		"""Check if a turn is detected."""
		if not self.is_moving:
			return False, 0.0, 0.0
		heading_change = abs(cse - self.last_course)
		if heading_change > 180:
			heading_change = 360 - heading_change
		turn_threshold = self.sb_mta + (self.sb_tsl / (spd_kmh if spd_kmh > 0 else 1))
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
		spd_kmh = int(APRSConverter.spd_to_kmh(spd))
		if self.is_moving:
			if spd_kmh <= 5:
				if not self.stop_time:
					self.stop_time = now
				if now - self.stop_time > 900:
					self.is_moving = False
					self.stop_time = 0
					self.symbt = self.cfg.symbol_table
					self.symb = self.cfg.symbol
					logging.info('SmartBeaconing disabled: Stopped moving.')
					return False
			else:
				self.stop_time = 0
			rate, self.symbt, self.symb = self._calculate_rate(spd_kmh)
			turn_detected, heading_change, turn_threshold = self._check_turn(cse, spd_kmh)
			time_since_last = now - self.last_beacon_time
			should_send = False
			if turn_detected and time_since_last > self.sb_mtt:
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
			if spd_kmh > 5:
				self.is_moving = True
				_, self.symbt, self.symb = self._calculate_rate(spd_kmh)
				self.stop_time = 0
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
		self._mmdvmhost_mtime: float = 0.0
		self._mmdvmhost_raw_config: dict = {}
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
		return psutil.virtual_memory().used + psutil.swap_memory().used

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

	def _calculate_uptime(self):
		"""Calculate human-readable uptime."""
		uptime_seconds = dt.datetime.now(dt.timezone.utc).timestamp() - psutil.boot_time()
		uptime = dt.timedelta(seconds=uptime_seconds)
		u_str = humanize.precisedelta(uptime, minimum_unit='minutes', format='%0.0f')
		for pattern, repl in [(r' years?', 'y'), (r' months?', 'mo'), (r' days?', 'd'), (r' hours?', 'h'), (r' minutes?', 'm'), (r' and|,', '')]:
			u_str = re.sub(pattern, repl, u_str)
		return f'up: {u_str}'

	def _calculate_traffic(self):
		"""Calculate network traffic info from vnstat."""
		try:
			output = subprocess.check_output(['vnstat', '--json', 'f', '1'], text=True)
			data = json.loads(output)
			best_rx, best_tx, max_total, found = 0, 0, -1, False
			if data.get('interfaces'):
				for iface in data['interfaces']:
					fiveminute_traffic = iface.get('traffic', {}).get('fiveminute')
					if fiveminute_traffic:
						last_entry = fiveminute_traffic[-1]
						rx_bytes, tx_bytes = last_entry.get('rx', 0), last_entry.get('tx', 0)
						total = rx_bytes + tx_bytes
						if total > max_total:
							max_total, best_rx, best_tx, found = total, rx_bytes, tx_bytes, True
			if found:
				rxtx = humanize.naturalsize(best_rx + best_tx).replace(' ', '')
				return f'net: {rxtx}'
		except Exception as e:
			logging.debug('Could not fetch vnstat 5-min traffic: %s', e)
		return ''

	def update_metrics(self):
		"""Unified method to calculate and update system metrics."""
		now = time.time()
		self._update_history(self._temp_history, self._fetch_raw_cpu_temp, now)
		self._update_history(self._cpu_history, self._fetch_raw_cpu_load, now)
		self._update_history(self._mem_history, self._fetch_raw_vram_used, now)
		self._cache['uptime'] = (self._calculate_uptime(), now)
		self._cache['traffic_info'] = (self._calculate_traffic(), now)
		try:
			self._cache['storage_used'] = (psutil.disk_usage('/').used, now)
		except Exception:
			pass

	def _get_stat_property(self, history, fetch_func, cache_key, scale=1):
		"""Get historical data property."""
		avg = self._calculate_average(history)
		if avg is not None:
			return int(avg * scale)
		return self._get_cached(cache_key, lambda: int(fetch_func() * scale), ttl=5, default=0)

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
		return self._get_cached('storage_used', lambda: psutil.disk_usage('/').used, ttl=60, default=0)

	@property
	def uptime(self):
		"""Get system uptime in a human-readable format."""
		return self._get_cached('uptime', self._calculate_uptime, ttl=60, default='')

	@property
	def traffic_info(self):
		"""Get network traffic info from vnstat."""
		return self._get_cached('traffic_info', self._calculate_traffic, ttl=300, default='')

	@property
	def os_info(self):
		"""Get operating system information."""

		def _fetch():
			os_name = ''
			try:
				info = platform.freedesktop_os_release()
				vendor = info.get('ID_LIKE', info.get('ID', '')).title()
				version = info.get('DEBIAN_VERSION_FULL') or info.get('VERSION_ID', '')
				codename = info.get('VERSION_CODENAME', '')
				os_name = ' '.join(filter(None, [vendor, version, f'({codename})' if codename else None]))
			except (AttributeError, OSError):
				os_name = platform.platform()

			kernel_ver = ''
			try:
				sysname = platform.system()
				release = platform.release()
				version_str = platform.version()
				build_no = version_str.split()[0] if version_str else ''
				build_date = ''
				if iso_match := re.search(r'(\d{4}-\d{2}-\d{2})', version_str):
					build_date = iso_match.group(1)
				else:
					kvpart = version_str.split()
					if len(kvpart) >= 5:
						try:
							d_str = f'{kvpart[-1]}{kvpart[-5]}{kvpart[-4]} {kvpart[-3]}'
							build_date = dt.datetime.strptime(d_str, '%Y%b%d %H:%M:%S').isoformat()
						except (ValueError, IndexError):
							pass
				rel_info = ''.join(filter(None, [release.split('-')[0], build_no, f'({build_date})' if build_date else None]))
				raw_machine = platform.machine()
				arch_map = {
					'aarch64': 'arm64',
					'armv8l': 'armhf',
					'armv7l': 'armhf',
					'armv6l': 'armhf',
					'armv5tejl': 'arm',
					'x86_64': 'amd64',
					'i386': 'x86',
					'i686': 'x86',
					'mips': 'mips',
					'mipsel': 'mipsel',
					'powerpc': 'ppc',
					'ppc64': 'ppc64',
					'ppc64le': 'ppc64le',
					'riscv64': 'riscv64',
					's390x': 's390x',
				}
				machine = arch_map.get(raw_machine, raw_machine)
				kernel_ver = ' '.join(filter(None, [sysname, rel_info, machine]))
			except Exception as e:
				logging.error('Unexpected error: %s', e)
			return f'{", ".join(filter(None, [os_name, kernel_ver]))}'

		return self._get_cached('os_info', _fetch, ttl=3600, default='')

	@property
	def mmdvm_info(self):
		"""Get MMDVM configured frequency and color code."""
		return self._get_cached('mmdvm_all', self._fetch_mmdvm_all, ttl=3600, default={}).get('info', '')

	@property
	def mmdvm_phg(self):
		"""Get PHG code from MMDVMHost configuration."""
		return self._get_cached('mmdvm_all', self._fetch_mmdvm_all, ttl=3600, default={}).get('phg', '')

	@staticmethod
	def _calc_phg(p_val, h_val, g_val, d_val):
		"""Helper to calculate PHG string from power, height, gain, and direction."""
		try:
			p = min(9, int(math.sqrt(float(p_val or 0))))
			h_val_f = float(h_val or 0)
			h_idx = int(math.log2(max(10, h_val_f) / 10))
			h = chr(48 + max(0, min(9, h_idx)))
			g = min(9, int(float(g_val or 0)))
			d = min(8, int(float(d_val or 0)) // 45)
			return f'PHG{p}{h}{g}{d}'
		except (ValueError, TypeError, ZeroDivisionError):
			return ''

	def _fetch_mmdvm_all(self):
		"""Unified fetch for MMDVM info and PHG from MMDVMHost configuration."""
		phg_str = self._calc_phg(self.cfg.phg_power, self.cfg.phg_height, self.cfg.phg_gain, self.cfg.phg_direction)
		mmdvm_file_path = self.cfg.mmdvmhost_file
		if not (os.path.isfile(mmdvm_file_path) and os.access(mmdvm_file_path, os.R_OK)):
			logging.debug('MMDVMHost file not found or not readable: %s', self.cfg.mmdvmhost_file)
			return {'info': '', 'phg': phg_str}
		current_mtime = os.path.getmtime(mmdvm_file_path)
		if current_mtime == self._mmdvmhost_mtime and self._mmdvmhost_raw_config:
			conf = self._mmdvmhost_raw_config
		else:
			conf = {}
			self._mmdvmhost_mtime = current_mtime
		section = 'GLOBAL'
		try:
			with open(mmdvm_file_path, 'r', encoding='utf-8', errors='replace') as f:
				for line in f:
					line = line.strip()
					if not line or line.startswith(('#', ';', '!')):
						continue
					if line.startswith('[') and ']' in line:
						section = line[1 : line.find(']')].strip().upper()
					elif '=' in line:
						parts = line.split('=', 1)
						key = parts[0].strip()
						val = parts[1].split('#', 1)[0].split(';', 1)[0].strip()
						conf[f'{section}:{key}'] = val
						if key not in conf:
							conf[key] = val
			self._mmdvmhost_raw_config = conf
		except (IOError, OSError) as e:
			logging.debug('Could not read MMDVMHost file %s: %s', self.cfg.mmdvmhost_file, e)
			return {'info': '', 'phg': phg_str}
		try:
			rx_f = int(conf.get('RXFrequency', 0))
			tx_f = int(conf.get('TXFrequency', 0))
			tx_str = humanize.metric(tx_f, 'Hz', precision=len(str(tx_f).rstrip('0')) or 1)
			offset = rx_f - tx_f
			shift = f'({"+" if offset > 0 else ""}{humanize.metric(offset, "Hz", precision=2)})' if offset != 0 else None
		except (ValueError, TypeError):
			tx_str, shift = '', None
		cc_ts = ''
		if conf.get('DMR:Enable', conf.get('Enable')) == '1':
			cc = f'C{conf.get("DMR:ColorCode", conf.get("ColorCode", "0"))}'
			s1 = conf.get('DMR:Slot1', conf.get('Slot1', '0')) == '1'
			s2 = conf.get('DMR:Slot2', conf.get('Slot2', '0')) == '1'
			ts = 'S1S2' if s1 and s2 else ('S1' if s1 else ('S2' if s2 else ''))
			cc_ts = cc + ts
		info_str = ' '.join(filter(None, [tx_str, shift, cc_ts]))
		if not phg_str:
			phg_str = self._calc_phg(
				conf.get('INFO:Power', conf.get('Power')),
				conf.get('INFO:Height', conf.get('Height')),
				conf.get('INFO:Gain', conf.get('Gain', 3)),
				conf.get('INFO:Direction', conf.get('Direction', 0)),
			)
		return {'info': info_str, 'phg': phg_str}


class ScheduledMessageHandler:
	"""Class to handle sending scheduled messages."""

	def __init__(self, cfg, gps_handler):
		self.cfg = cfg
		self.gps_handler = gps_handler
		self.tracking = PersistentDict(self.cfg.msg_tracking_file)
		self.messages = []
		self.sequences = {}
		self._init_messages()
		self._init_sequences()

	def _init_messages(self):
		"""Initialize scheduled messages."""
		self.messages = []
		definitions = [
			('aprsphnet_enabled', 'APRSPHNet', None, 'APRSPH', 'NET #{}', dt.timezone.utc),
			('aprsthursday_enabled', 'APRSThursday', 3, 'ANSRVR', 'CQ HOTG #{}', dt.timezone.utc),
			('aprsaturday_enabled', 'APRSaturday', 5, '9M4GHZ', 'CQ DXMY #{}', dt.timezone.utc),
			('aprsmysunday_enabled', 'APRSMYSunday', 6, 'APRSMY', 'CHECK #{}', dt.timezone(dt.timedelta(hours=8))),
			('aprshamfinity_enabled', 'APRSHamfinity', 6, '9M4GKS', 'CQ HAMFINITY #{}', dt.timezone.utc),
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
			source = msg_info['from_call'] or self.cfg.from_call
			addrcall = msg_info['addrcall']
			seq_name = f'msg_sequence_{source}_{addrcall}'
			if seq_name not in self.sequences:
				self.sequences[seq_name] = Sequence(self.cfg.lib_dir, name=seq_name, modulo=100000)

	async def _is_due(self, msg_info) -> bool:
		"""Return True if the message described by *msg_info* should be sent now."""
		now = dt.datetime.now(msg_info['tz'])
		if msg_info['weekday'] is not None and now.weekday() != msg_info['weekday']:
			return False
		today = now.strftime('%Y-%m-%d')
		source = msg_info['from_call'] or self.cfg.from_call
		tracking_key = f'{msg_info["name"]},{source},{msg_info["addrcall"]}'
		last_sent = self.tracking.get(tracking_key)
		if last_sent and last_sent.startswith(today):
			return False
		return True

	async def _send_one_with_delay(self, aprs_sender, gps_data=None, **msg_info):
		"""Perform ``_send_one`` after a random pause"""
		await asyncio.sleep(random.randint(15, 90))
		await self._send_one(aprs_sender, gps_data=gps_data, **msg_info)

	async def send_all(self, aprs_sender, gps_data=None):
		"""Send all due scheduled messages."""
		for msg_info in self.messages:
			if await self._is_due(msg_info):
				source = msg_info['from_call'] or self.cfg.from_call
				tracking_key = f'{msg_info["name"]},{source},{msg_info["addrcall"]}'
				self.tracking[tracking_key] = dt.datetime.now(msg_info['tz']).isoformat()
				self.tracking.flush()
				asyncio.create_task(self._send_one_with_delay(aprs_sender, gps_data=gps_data, **msg_info))
		return False

	async def _send_one(self, aprs_sender, name, addrcall, template, from_call=None, gps_data=None, **kwargs):
		"""Send a single scheduled message to APRS-IS if it's due."""
		loc_data, _ = gps_data if gps_data else await self.gps_handler.get_loc_and_sat()
		_, lat, lon, _, _, _ = loc_data
		gridsquare = APRSConverter.latlon_to_grid(lat, lon)
		source = from_call or self.cfg.from_call
		seq_name = f'msg_sequence_{source}_{addrcall}'
		seq = next(self.sequences[seq_name])
		app_id = '/'.join(self.cfg.app_name.split('/')[:2])
		message = f'{template} from {gridsquare} via {app_id}'[:67]
		path_str = ''
		if from_call:
			path_str = f',{self.cfg.from_call}*,qAR,{self.cfg.from_call}'
		payload = f'{source}>{self.cfg.to_call}{path_str}::{addrcall:9s}:{message}{{{seq}'
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
		tg_msg += f'\nTo: <b>{parsed["addresse"]}</b>\n{f"MessageNo: {parsed['msgNo']}" if parsed.get("msgNo") else ""}\nMessage: <b>{parsed["message_text"]}</b>'
		await aprs_sender.tg_logger.log(tg_msg, topic_id=self.cfg.telegram_msg_topic_id)
		return True


class TelegramLogger(object):
	"""Class to handle logging to Telegram."""

	def __init__(self, cfg):
		self.cfg = cfg
		self.enabled = cfg.telegram_enabled
		self.bot = None
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
			if lat != 0 and lon != 0:
				await self._update_location(lat, lon, cse)
			message = f'{tg_message}\n\n<code>{self.cfg.app_name}</code>'
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
		except Exception as e:
			logging.error('Failed to send message to Telegram: %s', e)

	def _read_location_id(self):
		"""Reads location message ID and start time from file."""
		if not os.path.exists(self.cfg.location_id_file):
			return None, None
		try:
			with open(self.cfg.location_id_file, 'r') as f:
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
			with open(self.cfg.location_id_file, 'w') as f:
				f.write(f'{msg_id}:{start_time}')
		except IOError as e:
			logging.error('Failed to save location ID: %s', e)

	def _remove_location_id_file(self):
		"""Removes the location ID file."""
		if os.path.exists(self.cfg.location_id_file):
			try:
				os.remove(self.cfg.location_id_file)
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

	def __init__(self, cfg, tg_logger, sys_stats, gps_handler, geolocation, telem_seq):
		self.cfg = cfg
		self.tg_logger = tg_logger
		self.sys_stats = sys_stats
		self.gps_handler = gps_handler
		self.geolocation = geolocation
		self.ais = None
		self.telem_seq = telem_seq

	def _get_timestamps(self, source_time: dt.datetime | None = None) -> tuple[str, str]:
		"""Generate APRS and ISO8601 timestamps."""
		ctime = source_time or dt.datetime.now(dt.timezone.utc)
		return ctime.strftime('%d%H%Mz'), ctime.astimezone().isoformat(timespec='seconds')

	def _aprs_callback(self, packet: str):
		"""Callback function to process incoming APRS packets from the server."""
		logging.info('Received APRS packet: %s', packet)
		try:
			parsed_packet = aprslib.parse(packet)
			if 'message' in parsed_packet:
				from_call = parsed_packet.get('from', 'UNKNOWN')
				addresse = parsed_packet.get('addresse', 'UNKNOWN')
				message_text = parsed_packet.get('message_text', '')
				msg_no = parsed_packet.get('msgNo')
				if addresse == self.cfg.from_call and msg_no:
					ack_payload = f'{self.cfg.from_call}>{self.cfg.to_call}::{from_call:9s}:ack{msg_no}'
					logging.debug('Replying acknowledge for message %s from %s', msg_no, from_call)
					asyncio.create_task(self.send_packet(ack_payload, 'ack'))
				tg_msg = (
					f'<u>APRS Message Received</u>\n\n'
					f'From: <b>{from_call}</b>\n'
					f'To: <b>{addresse}</b>\n'
					f'{f"MsgNo: {msg_no}" if msg_no else ""}\nMessage: <b>{message_text}</b>'
				)
				asyncio.create_task(self.tg_logger.log(tg_msg, topic_id=self.cfg.telegram_msg_topic_id))
		except APRSParseError as e:
			logging.warning('Failed to parse incoming APRS packet: %s - Raw: %s', e, packet)
		except Exception as e:
			logging.error('Unexpected error in APRS callback: %s', e, exc_info=True)

	async def connect(self):
		"""Establish connection to APRS-IS with retries."""
		logging.info('Connecting to APRS-IS server %s:%d as %s', self.cfg.aprsis_server, self.cfg.aprsis_port, self.cfg.from_call)
		loop = asyncio.get_running_loop()
		max_retries = 5
		retry_delay = 5
		for attempt in range(max_retries):
			try:
				self.ais = aprslib.IS(
					callsign=self.cfg.from_call, passwd=self.cfg.aprs_passcode, host=self.cfg.aprsis_server, port=self.cfg.aprsis_port
				)
				if self.ais is None:
					logging.critical('Failed to create aprslib.IS instance; object is None.')
					raise APRSConnectionError('Failed to initialize aprslib.IS object.')
				logging.debug('Attempting connect to APRS-IS %s', self.ais.server)
				await loop.run_in_executor(None, self.ais.connect)
				if self.ais._connected:
					logging.info('Connected to APRS-IS server %s:%d as %s', self.ais.server[0], self.ais.server[1], self.ais.callsign)
				if self.cfg.aprsis_filter:
					await loop.run_in_executor(None, self.ais.set_filter, self.cfg.aprsis_filter)
					logging.info('APRS-IS filter set to: %s', self.cfg.aprsis_filter)
					await loop.run_in_executor(None, self.ais.consumer(self._aprs_callback, raw=True))
				return
			except APRSConnectionError as err:
				logging.warning('APRS connection error (attempt %d/%d): %s', attempt + 1, max_retries, err)
			except Exception as e:
				logging.error('Unexpected error (attempt %d/%d): %s', attempt + 1, max_retries, e, exc_info=True)
			if attempt < max_retries - 1:
				await asyncio.sleep(retry_delay)
				retry_delay = min(retry_delay * 2, 60)
			else:
				logging.critical('All attempts to connect to APRS-IS failed, exiting.')
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

	async def send_position(self, gps_data=None, is_moving=False, symbt=None, symb=None):
		"""Send APRS position packet to APRS-IS."""
		loc_data, _ = gps_data if gps_data else await self.gps_handler.get_loc_and_sat()
		cur_time, cur_lat, cur_lon, cur_alt, cur_spd, cur_cse = loc_data
		latstr = APRSConverter.lat_to_aprs(cur_lat)
		lonstr = APRSConverter.lon_to_aprs(cur_lon)
		altstr = APRSConverter.alt_to_aprs(cur_alt)
		csestr = APRSConverter.cse_to_aprs(cur_cse)
		spdknt = APRSConverter.spd_to_knot(cur_spd)
		spdkmh = APRSConverter.spd_to_kmh(cur_spd)
		mmdvminfo = self.sys_stats.mmdvm_info
		mmdvmphg = self.sys_stats.mmdvm_phg
		osinfo = self.sys_stats.os_info
		comment = '; '.join(filter(None, [mmdvminfo, osinfo, self.cfg.project_url]))
		timestamp, tg_timestamp = self._get_timestamps(cur_time)
		symbt = symbt or self.cfg.symbol_table
		symb = symb or self.cfg.symbol
		if self.cfg.symbol_overlay:
			symbt = self.cfg.symbol_overlay
		extstr = ''
		ext_tg = ''
		if not is_moving:
			extstr = mmdvmphg
			if mmdvmphg.startswith('PHG') and len(mmdvmphg) == 7:
				p = int(mmdvmphg[3])
				h = ord(mmdvmphg[4]) - 48
				g = int(mmdvmphg[5])
				d = int(mmdvmphg[6])
				p_w, h_ft, dir_deg = p * p, 10 * (2**h), d * 45
				dir_txt = ['Omni', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW', 'N'][d]
				dir_deg = f' ({dir_deg}°)' if d > 0 else ''
				ext_tg = (
					f'\n\t{mmdvmphg}'
					f'\n\t\tPower: <b>{humanize.metric(int(p_w), "W", precision=1)}</b>'
					f'\n\t\tHeight: <b>{humanize.metric(int(h_ft), "ft", precision=1)}</b>'
					f'\n\t\tGain: <b>{humanize.metric(int(g), "dB", precision=1)}</b>'
					f'\n\t\tDirection: <b>{dir_txt}{dir_deg}</b>'
				)
		else:
			extstr = f'{csestr}/{spdknt}'
			ext_tg = (
				f'\n\tHeading: <b>{int(cur_cse)}°</b>'
				f'\n\tSpeed: <b>{humanize.metric(float(spdkmh), "km/h", precision=1)}</b> | <b>{humanize.metric(float(spdknt), "kn", precision=1)}</b> | <b>{humanize.metric(cur_spd, "m/s")}</b>'
			)
		lookup_table = symbt if symbt in ['/', '\\'] else '\\'
		sym_desc = symbols.get_desc(lookup_table, symb)
		payload = f'{self.cfg.from_call}>{self.cfg.to_call}:/{timestamp}{latstr}{symbt}{lonstr}{symb}{extstr}{altstr}{comment}'
		tg_pos = (
			f'<u>{self.cfg.from_call} Position</u>\n\n'
			f'Time: <b>{tg_timestamp}</b>\n'
			f'Symbol: <b>{symbt}{symb} ({sym_desc})</b>\n'
			f'Position:\n'
			f'\tLatitude: <b>{cur_lat}</b>\n'
			f'\tLongitude: <b>{cur_lon}</b>\n'
			f'\tAltitude: <b>{cur_alt}m</b>{ext_tg}\n'
			f'Comment: <b>{comment}</b>'
		)
		await self.send_packet(payload, 'position')
		await self.tg_logger.log(tg_pos, cur_lat, cur_lon, int(csestr))

	async def send_header(self):
		"""Send APRS header information to APRS-IS."""
		caller = f'{self.cfg.from_call}>{self.cfg.to_call}::{self.cfg.from_call:9s}:'
		params = ['Temp', 'Load', 'RAM', 'ROM']
		units = ['deg.C', '%', 'GB', 'GB']
		eqns = ['0,0.1,0', '0,0.1,0', '0,0.001,0', '0,0.001,0']
		if self.cfg.gpsd_enabled:
			params.append('GPS')
			units.append('sats')
			eqns.append('0,1,0')
		payload = f'{caller}PARM.{",".join(params)}\r\n{caller}UNIT.{",".join(units)}\r\n{caller}EQNS.{",".join(eqns)}'
		tg_hdr = (
			f'<u>{self.cfg.from_call} Header</u>\n\n'
			f'Parameters: <b>{",".join(params)}</b>\n'
			f'Units: <b>{",".join(units)}</b>\n'
			f'Equations: <b>{",".join(eqns)}</b>\n\n'
			f'Value: <code>[a,b,c]=(a×v²)+(b×v)+c</code>'
		)
		await self.send_packet(payload, 'header')
		await self.tg_logger.log(tg_hdr)

	async def send_telemetry(self, gps_data=None):
		"""Send APRS telemetry information to APRS-IS."""
		seq = next(self.telem_seq)
		cputemp = self.sys_stats.avg_temp
		cpuload = self.sys_stats.avg_cpu
		memused = self.sys_stats.avg_vram
		diskused = self.sys_stats.storage_used
		telemmemused = int(memused / 1.0000e6)
		telemdiskused = int(diskused / 1.0000e6)
		payload = f'{self.cfg.from_call}>{self.cfg.to_call}:T#{seq:03d},{cputemp:d},{cpuload:d},{telemmemused:d},{telemdiskused:d}'
		tg_tlm = (
			f'<u>{self.cfg.from_call} Telemetry</u>\n\n'
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
				tg_tlm += f'\nGPS Lock: <b>{uSat}</b>\nGPS Avail: <b>{nSat}</b>'
		await self.send_packet(payload, 'telemetry')
		await self.tg_logger.log(tg_tlm)

	async def send_status(self, gps_data=None):
		"""Send APRS status information to APRS-IS."""
		loc_data, sat_data = gps_data if gps_data else await self.gps_handler.get_loc_and_sat()
		cur_time, lat, lon, _, _, _ = loc_data
		timestamp, tg_timestamp = self._get_timestamps(cur_time)
		gridsquare = f'{APRSConverter.latlon_to_grid(lat, lon)}'
		address = self.geolocation.get_address(lat, lon)
		near_add = self.geolocation.format_address(address)
		near_add_tg = self.geolocation.format_address(address, True)
		uptime = self.sys_stats.uptime
		traffic = self.sys_stats.traffic_info
		sats_info = ''
		if self.cfg.gpsd_enabled:
			_, u_sat, n_sat = sat_data
			if u_sat > 0:
				sats_info = f'gps: {u_sat}/{n_sat}'
		stat_text = f'{timestamp}{"; ".join(filter(None, [gridsquare, near_add, uptime, traffic, sats_info]))}'
		tele_text = f'Time: <b>{tg_timestamp}</b>\nText: <b>{"; ".join(filter(None, [gridsquare, near_add_tg, uptime, traffic, sats_info]))}</b>'
		payload = f'{self.cfg.from_call}>{self.cfg.to_call}:>{stat_text}'
		tg_stat = f'<u>{self.cfg.from_call} Status</u>\n\n<b>{tele_text}</b>'
		if os.path.exists(self.cfg.status_file):
			try:
				with open(self.cfg.status_file, 'r') as f:
					if f.read() == payload:
						return
			except (IOError, OSError):
				pass
		await self.send_packet(payload, 'status')
		try:
			with open(self.cfg.status_file, 'w') as f:
				f.write(payload)
		except (IOError, OSError):
			pass
		await self.tg_logger.log(tg_stat)

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
	sys_stats.update_metrics()
	geolocation = Geolocation(cfg.app_name, cfg.project_url, cfg.nominatim_cache_file)
	telem_seq = Sequence(cfg.lib_dir, name='telem_sequence', modulo=1000)
	aprs_sender = APRSSender(cfg, tg_logger, sys_stats, gps_handler, geolocation, telem_seq)
	await aprs_sender.connect()
	timer = Timer(cfg.tmp_dir)
	sb = SmartBeaconing(cfg)
	scheduled_msg_handler = ScheduledMessageHandler(cfg, gps_handler)
	return aprs_sender, tg_logger, timer, sb, sys_stats, scheduled_msg_handler, gps_handler


def should_send_position(cfg, timer_tick, sb, gps_data):
	"""Determine if a position update is needed."""
	return (cfg.gpsd_enabled and cfg.smartbeaconing_enabled and sb.should_send(gps_data)) or (timer_tick % 1200 == 1)


def _get_tasks(cfg, timer_tick, sb, gps_data, aprs_sender):
	class Task(NamedTuple):
		condition: bool
		func: Callable
		args: tuple
		kwargs: dict

	loc_data, _ = gps_data if gps_data else None
	return [
		Task(timer_tick % 21600 == 1, aprs_sender.send_header, (), {}),
		Task(
			should_send_position(cfg, timer_tick, sb, loc_data),
			aprs_sender.send_position,
			(),
			{'gps_data': gps_data, 'is_moving': sb.is_moving, 'symbt': sb.symbt, 'symb': sb.symb},
		),
		Task(timer_tick % cfg.sleep == 1, aprs_sender.send_telemetry, (), {'gps_data': gps_data}),
	]


async def process_loop(cfg, aprs_sender, timer, sb, sys_stats, reload_event, scheduled_msg_handler, gps_handler, gps_data):
	"""Run the main processing loop."""
	while True:
		timer_tick = next(timer)
		if reload_event.is_set():
			break
		if timer_tick % 20 == 0:
			sys_stats.update_metrics()
		packet_sent_this_cycle = False
		position_packet_was_sent = False
		tasks_to_run = _get_tasks(cfg, timer_tick, sb, gps_data, aprs_sender)
		for task in tasks_to_run:
			if task.condition:
				try:
					res = await task.func(*task.args, **task.kwargs)
					sent = res if isinstance(res, bool) else True
					if sent:
						packet_sent_this_cycle = True
						if task.func == aprs_sender.send_position:
							position_packet_was_sent = True
				except Exception as e:
					logging.error('Error executing task %s: %s', task.func.__name__, e, exc_info=True)
		if position_packet_was_sent and scheduled_msg_handler.messages:
			try:
				res = await scheduled_msg_handler.send_all(aprs_sender, gps_data=gps_data)
				if res:
					packet_sent_this_cycle = True
			except Exception as e:
				logging.error('Error executing scheduled message: %s', e, exc_info=True)
		if packet_sent_this_cycle:
			await aprs_sender.send_status(gps_data=gps_data)
		await asyncio.sleep(1)
		gps_data = await gps_handler.get_loc_and_sat()


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
			await asyncio.sleep(2)
		gps_data = await gps_handler.get_loc_and_sat()
		async with tg_logger:
			await tg_logger.log(f'🚀 {cfg.app_name.split("-")[0]} Started')
			try:
				await process_loop(cfg, aprs_sender, timer, sb, sys_stats, reload_event, scheduled_msg_handler, gps_handler, gps_data)
			finally:
				if reload_event.is_set():
					await tg_logger.log(f'🔄 {cfg.app_name.split("-")[0]} Reloaded')
				else:
					await tg_logger.log(f'🛑 {cfg.app_name.split("-")[0]} Stopped')
				await tg_logger.stop_location()
				if health_check_task:
					health_check_task.cancel()
				if gps_polling_task:
					gps_polling_task.cancel()
				aprs_sender.close()
		if not reload_event.is_set():
			break


if __name__ == '__main__':
	cfg = Config()
	configure_logging(cfg)
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
