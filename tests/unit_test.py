import os
import sys
import unittest
from unittest.mock import MagicMock, mock_open, patch

# Adjust path to import modules from parent directory
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ..src.main import main
from ..src.symbols import symbols as aprs_symbols


class TestAprsSymbols(unittest.TestCase):
	def test_get_symbol_description_valid(self):
		"""Test retrieving a valid symbol description."""
		description = aprs_symbols.get_symbol_description('/', '!')
		self.assertEqual(description, 'Police, Sheriff')

	def test_get_symbol_description_unknown(self):
		"""Test retrieving an unknown symbol description."""
		description = aprs_symbols.get_symbol_description('/', 'unknown_symbol')
		self.assertEqual(description, 'Unknown Symbol')


class TestMainUtils(unittest.TestCase):
	def test_latlon_to_grid(self):
		"""Test Maidenhead grid locator conversion."""
		# Test coordinates for Greenwich (approx)
		grid = main.latlon_to_grid(51.4779, 0.0015)
		self.assertTrue(grid.startswith('JO01'))

		# Test coordinates for New York
		grid = main.latlon_to_grid(40.7128, -74.0060)
		self.assertTrue(grid.startswith('FN20'))

	def test_mps_to_kmh(self):
		"""Test m/s to km/h conversion."""
		self.assertEqual(main._mps_to_kmh(0), '000')
		self.assertEqual(main._mps_to_kmh(10), '036')  # 36 km/h
		self.assertEqual(main._mps_to_kmh(27.7778), '100')  # ~100 km/h


class TestConfig(unittest.TestCase):
	@patch('main.dotenv.load_dotenv')
	@patch('os.getenv')
	def test_config_initialization(self, mock_getenv, mock_load_dotenv):
		"""Test Config object initialization with environment variables."""
		# Setup mock environment
		env_vars = {'APRS_CALL': 'N0TEST', 'APRS_SSID': '5', 'APRSIS_SERVER': 'test.server', 'APRSIS_PORT': '12345'}
		mock_getenv.side_effect = lambda k, d=None: env_vars.get(k, d)

		cfg = main.Config()

		self.assertEqual(cfg.call, 'N0TEST-5')
		self.assertEqual(cfg.server, 'test.server')
		self.assertEqual(cfg.port, 12345)


class TestSequence(unittest.TestCase):
	def test_sequence_increment(self):
		"""Test Sequence class increments and wraps."""
		# Mock reading '10' from file
		with patch('builtins.open', mock_open(read_data='10')) as m_open:
			seq = main.Sequence()
			self.assertEqual(seq._count, 10)

			# Mock writing back to file
			val = seq.next()
			self.assertEqual(val, 11)
			m_open().write.assert_called_with('11')

	def test_sequence_wrap(self):
		"""Test Sequence wraps at 999."""
		with patch('builtins.open', mock_open(read_data='999')):
			seq = main.Sequence()
			val = seq.next()
			self.assertEqual(val, 0)


class TestSmartBeaconing(unittest.TestCase):
	@patch('os.getenv')
	def test_should_send_rate(self, mock_getenv):
		"""Test SmartBeaconing rate limiting logic."""

		# Mock config values
		def getenv_side_effect(key, default=None):
			defaults = {
				'SMARTBEACONING_FASTSPEED': 100,  # km/h
				'SMARTBEACONING_SLOWSPEED': 10,  # km/h
				'SMARTBEACONING_FASTRATE': 60,  # sec
				'SMARTBEACONING_SLOWRATE': 600,  # sec
				'SMARTBEACONING_MINTURNANGLE': 28,
				'SMARTBEACONING_TURNSLOPE': 255,
				'SMARTBEACONING_MINTURNTIME': 5,
			}
			return defaults.get(key, default)

		mock_getenv.side_effect = getenv_side_effect

		sb = main.SmartBeaconing()

		# Mock time
		current_time = 10000
		sb.last_beacon_time = current_time - 601  # Expired slow rate

		with patch('time.time', return_value=current_time):
			# Stopped (0 m/s), should trigger slow rate (600s)
			# gps_data: (utc, lat, lon, alt, spd, cse)
			gps_data = (None, 0, 0, 0, 0, 0)
			self.assertTrue(sb.should_send(gps_data))

			# Reset timer
			sb.last_beacon_time = current_time

			# Fast speed (30 m/s ~ 108 km/h), should trigger fast rate (60s)
			# But time diff is 0, so False
			gps_data = (None, 0, 0, 0, 30, 0)
			self.assertFalse(sb.should_send(gps_data))

			# Fast speed, time diff 61s
			sb.last_beacon_time = current_time - 61
			self.assertTrue(sb.should_send(gps_data))

	@patch('os.getenv')
	def test_should_send_turn(self, mock_getenv):
		"""Test SmartBeaconing turn detection."""

		# Mock config values
		def getenv_side_effect(key, default=None):
			defaults = {
				'SMARTBEACONING_FASTSPEED': 100,
				'SMARTBEACONING_SLOWSPEED': 10,
				'SMARTBEACONING_FASTRATE': 60,
				'SMARTBEACONING_SLOWRATE': 600,
				'SMARTBEACONING_MINTURNANGLE': 28,
				'SMARTBEACONING_TURNSLOPE': 255,
				'SMARTBEACONING_MINTURNTIME': 5,
			}
			return defaults.get(key, default)

		mock_getenv.side_effect = getenv_side_effect

		sb = main.SmartBeaconing()
		current_time = 10000

		with patch('time.time', return_value=current_time):
			sb.last_beacon_time = current_time - 10  # > min turn time (5s)
			sb.last_course = 0

			# Speed 20 m/s (~72 km/h).
			# Turn threshold = 28 + (255/72) = ~31.5 degrees

			# Small turn (10 deg) -> False
			gps_data = (None, 0, 0, 0, 20, 10)
			self.assertFalse(sb.should_send(gps_data))

			# Large turn (40 deg) -> True
			gps_data = (None, 0, 0, 0, 20, 40)
			self.assertTrue(sb.should_send(gps_data))


class TestSystemInfo(unittest.TestCase):
	def test_get_osinfo(self):
		"""Test OS info parsing."""
		mock_os_release = 'PRETTY_NAME="Raspbian GNU/Linux 10 (buster)"\nNAME="Raspbian GNU/Linux"\nVERSION_ID="10"\nVERSION="10 (buster)"\nID=raspbian\nID_LIKE=debian\nHOME_URL="http://www.raspbian.org/"\nSUPPORT_URL="http://www.raspbian.org/RaspbianForums"\nBUG_REPORT_URL="http://www.raspbian.org/RaspbianBugs"\nDEBIAN_VERSION_FULL=10.9\nVERSION_CODENAME=buster\n'

		with patch('builtins.open', mock_open(read_data=mock_os_release)):
			with patch('os.uname') as mock_uname:
				mock_uname.return_value = MagicMock(sysname='Linux', release='5.10.17-v7+', version='#1414 SMP Fri Apr 30 13:18:35 BST 2021', machine='armv7l')
				info = main.get_osinfo()
				self.assertIn('Debian 10.9 (buster)', info)
				self.assertIn('[Linux 5.10.17-v7+#1414 armv7l]', info)


if __name__ == '__main__':
	unittest.main()
