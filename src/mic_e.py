import asyncio
import logging
import os
import re
from time import time


class MMDVMLogWatcher:
	def __init__(self, mmdvmhost_file):
		self.mmdvmhost_file = mmdvmhost_file
		self.mmdvm_log_dir = self._get_log_dir()
		self.mmdvm_file_root = self._get_file_root()
		self.mmdvm_last_file = None
		self.last_pos = 0
		self.active_transmissions = {}  # Track active transmissions by callsign
		self.last_transmission_time = 0  # Track last transmission time
		self.transmission_source = None  # Track if transmission is RF or network
		self.is_moving = False  # Track if station is moving

	def _get_log_dir(self):
		"""Extract log directory from MMDVM configuration file."""
		if not self.mmdvmhost_file or not os.path.isfile(self.mmdvmhost_file):
			logging.warning('MMDVMHost file not found: %s. Using default log directory.', self.mmdvmhost_file)
			return '/var/log/pi-star'
		try:
			log_dir = None
			file_root = None
			with open(self.mmdvmhost_file, 'r', encoding='utf-8', errors='replace') as f:
				current_section = ''
				for line in f:
					line = line.strip()
					if not line or line.startswith(('#', ';', '!')):
						continue
					# Check for section headers
					if line.startswith('[') and ']' in line:
						current_section = line[1 : line.find(']')].strip().upper()
					elif '=' in line and current_section == 'LOG':
						key, val = line.split('=', 1)
						key = key.strip().upper()
						val = val.split('#', 1)[0].split(';', 1)[0].strip()
						if key == 'FILEPATH':
							log_dir = os.path.dirname(val) if val else None
						elif key == 'FILEROOT':
							file_root = val
			# Prefer FilePath, fallback to FileRoot if available
			if log_dir and os.path.isdir(log_dir):
				logging.info('Log directory from FilePath: %s', log_dir)
				return log_dir
			elif file_root and os.path.isdir(file_root):
				logging.info('Log directory from FileRoot: %s', file_root)
				return file_root
			else:
				logging.warning('Could not find valid FilePath or FileRoot in [LOG] section. Using default.')
				return '/var/log/pi-star'
		except Exception as e:
			logging.error('Error reading MMDVMHost config: %s', e)
			return '/var/log/pi-star'

	def _get_file_root(self):
		"""Extract FileRoot value from MMDVM configuration file."""
		if not self.mmdvmhost_file or not os.path.isfile(self.mmdvmhost_file):
			return 'MMDVM-'
		try:
			with open(self.mmdvmhost_file, 'r', encoding='utf-8', errors='replace') as f:
				current_section = ''
				for line in f:
					line = line.strip()
					if not line or line.startswith(('#', ';', '!')):
						continue
					# Check for section headers
					if line.startswith('[') and ']' in line:
						current_section = line[1 : line.find(']')].strip().upper()
					elif '=' in line and current_section == 'LOG':
						key, val = line.split('=', 1)
						key = key.strip().upper()
						val = val.split('#', 1)[0].split(';', 1)[0].strip()
						if key == 'FILEROOT':
							logging.info('FileRoot from MMDVMHost config: %s', val)
							return val
			logging.debug('FileRoot not found in [LOG] section. Using default.')
			return 'MMDVM-'
		except Exception as e:
			logging.error('Error reading FileRoot from MMDVMHost config: %s', e)
			return 'MMDVM-'

	def _get_latest_log(self):
		"""Get the most recently modified MMDVM log file."""
		try:
			files = [os.path.join(self.mmdvm_log_dir, f) for f in os.listdir(self.mmdvm_log_dir) if f.startswith(self.mmdvm_file_root) and f.endswith('.log')]
			return max(files, key=os.path.getmtime) if files else None
		except Exception as e:
			logging.debug('Error getting latest MMDVM log: %s', e)
			return None

	def _get_status_bits(self):
		"""Determine Mic-E status bits based on transmission state.

		M0 (Off Duty):    111 - idle 15+ min after transmission
		M1 (En Route):    110 - transmitting while moving
		M2 (In Service):  101 - idle (default)
		M3 (Returning):   100 - RF transmitting
		M4 (Committed):   011 - network transmitting
		"""
		current_time = time()
		idle_threshold = 900  # 15 minutes in seconds
		# M0: Off Duty - idle for 15+ minutes after last transmission
		if self.last_transmission_time and (current_time - self.last_transmission_time) >= idle_threshold:
			return '111'
		# M4: Committed - network transmitting
		if self.transmission_source == 'network':
			return '011'
		# M3: Returning - RF transmitting
		if self.transmission_source == 'rf':
			return '100'
		# M1: En Route - transmitting while moving
		if self.transmission_source and self.is_moving:
			return '110'
		# M2: In Service - idle (default)
		return '101'

	def set_is_moving(self, is_moving):
		"""Update whether the station is moving."""
		self.is_moving = is_moving

	async def watch(self):
		"""Async generator that yields callsigns and status from new voice transmissions."""
		while True:
			current_log = self._get_latest_log()
			if not current_log:
				await asyncio.sleep(10)
				continue
			if current_log != self.mmdvm_last_file:
				self.mmdvm_last_file = current_log
				self.last_pos = os.path.getsize(current_log)
			try:
				with open(current_log, 'r', errors='replace') as f:
					f.seek(self.last_pos)
					lines = f.readlines()
					self.last_pos = f.tell()
					for line in lines:
						# Look for start of transmission patterns
						match_start = re.search(r'received ((?:network|RF)) voice transmission from ([\w\d-]+)', line)
						if match_start:
							source = match_start.group(1).lower()
							callsign = match_start.group(2)
							self.active_transmissions[callsign] = asyncio.get_event_loop().time()
							self.transmission_source = source
							msg_bits = self._get_status_bits()
							yield ('start', callsign, source, msg_bits)
						# Look for end of transmission patterns
						match_end = re.search(r'received (?:network|RF) end of voice transmission from ([\w\d-]+)', line)
						if match_end:
							callsign = match_end.group(1)
							if callsign in self.active_transmissions:
								duration = asyncio.get_event_loop().time() - self.active_transmissions[callsign]
								# Ignore kerchunks (transmissions shorter than 2 seconds)
								if duration > 2:
									self.last_transmission_time = time()
									self.transmission_source = None
									msg_bits = self._get_status_bits()
									yield ('end', callsign, duration, msg_bits)
									logging.debug('Transmission from %s duration: %.2f seconds', callsign, duration)
								else:
									logging.debug('Ignoring kerchunk from %s (duration: %.2f seconds)', callsign, duration)
								del self.active_transmissions[callsign]
			except Exception as e:
				logging.debug('Error reading MMDVM log: %s', e)
			await asyncio.sleep(1)


class MicEEncoder:
	"""
	Mic-E encoder for compressed APRS packets.
	Encodes position, speed, and course into the destination field and information field.
	"""

	def encode(self, lat, lon, course=0, speed=0, status_bits='111', symbol='/', table='['):
		"""
		Encodes coordinates into Mic-E format.
		Returns (destination, information_field)
		"""
		# Encode Latitude and Message Bits into Destination Field
		abs_lat = abs(lat)
		lat_deg = int(abs_lat)
		lat_min = (abs_lat - lat_deg) * 60
		lat_min_int = int(lat_min)
		lat_hun = int((lat_min - lat_min_int) * 100)
		lat_digits = [lat_deg // 10, lat_deg % 10, lat_min_int // 10, lat_min_int % 10, lat_hun // 10, lat_hun % 10]
		# Message bits A, B, C from status_bits
		bits = [int(b) for b in status_bits]  # [A, B, C]
		# North/South, Longitude Offset, West/East
		bits.append(1 if lat >= 0 else 0)  # Bit 4: N=1, S=0
		bits.append(1 if abs(lon) >= 100 else 0)  # Bit 5: Offset +100
		bits.append(0 if lon >= 0 else 1)  # Bit 6: E=0, W=1
		dest = ''
		for i, d in enumerate(lat_digits):
			if bits[i] == 0:
				dest += chr(48 + d) if i < 3 else (chr(76 + d) if i == 3 else (chr(80 + d) if i == 4 else chr(76 + d)))
			else:
				dest += chr(65 + d) if i < 3 else (chr(80 + d) if i == 3 else (chr(65 + d) if i == 4 else chr(80 + d)))
		# Encode Longitude into Information Field
		abs_lon = abs(lon)
		lon_deg = int(abs_lon)
		if 100 <= lon_deg <= 179:
			lon_deg -= 100
		elif 0 <= lon_deg <= 9:
			lon_deg += 80
		lon_min = (abs_lon - int(abs_lon)) * 60
		lon_min_int = int(lon_min)
		lon_hun = int((lon_min - lon_min_int) * 100)
		info = chr(lon_deg + 28) + chr(lon_min_int + 28) + chr(lon_hun + 28)
		# Encode Speed and Course
		speed_knots = int(speed)
		course_deg = int(course)
		s1 = (speed_knots // 10) + 28
		s2 = (speed_knots % 10) * 10 + (course_deg // 100) + 28
		s3 = (course_deg % 100) + 28
		info += chr(s1) + chr(s2) + chr(s3)
		info += symbol + table
		return dest, info
