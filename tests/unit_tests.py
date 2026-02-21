"""Unit tests for RasPiAPRS."""

import asyncio
import datetime as dt
import os
import sys
import unittest
from unittest.mock import MagicMock, mock_open, patch

# Ensure src is in the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

import main
import symbols


class TestSymbols(unittest.TestCase):
	def test_get_desc(self):
		self.assertEqual(symbols.get_desc('/', '!'), 'Police, Sheriff')
		self.assertEqual(symbols.get_desc('\\', '!'), 'Emergency (E=ELT/EPIRB, V=Volcanic Eruption/Lava)')
		self.assertEqual(symbols.get_desc('/', 'unknown'), 'Unknown')


class TestConfig(unittest.TestCase):
	@patch('os.getenv')
	def test_defaults(self, mock_getenv):
		mock_getenv.side_effect = lambda k, d=None: d
		cfg = main.Config()
		self.assertEqual(cfg.call, 'N0CALL')
		self.assertEqual(cfg.sleep, 600)
		self.assertEqual(cfg.symbol_table, '/')
		self.assertEqual(cfg.symbol, 'n')
		self.assertEqual(cfg.server, 'rotate.aprs2.net')
		self.assertEqual(cfg.port, 14580)

	@patch('os.getenv')
	@patch('aprslib.passcode')
	def test_custom_values(self, mock_passcode, mock_getenv):
		env = {
			'APRS_CALL': 'MYCALL',
			'APRS_SSID': '5',
			'SLEEP': '300',
			'APRS_SYMBOL_TABLE': '\\',
			'APRS_SYMBOL': '>',
			'APRS_LATITUDE': '12.34',
			'APRS_LONGITUDE': '56.78',
			'APRS_ALTITUDE': '100',
			'APRSIS_SERVER': 'my.server.net',
			'APRSIS_PORT': '10000',
			'GPSD_ENABLE': '1',
			'SMARTBEACONING_ENABLE': '1',
			'TELEGRAM_ENABLE': '1',
		}
		mock_getenv.side_effect = lambda k, d=None: env.get(k, d)
		mock_passcode.return_value = '12345'

		cfg = main.Config()
		self.assertEqual(cfg.call, 'MYCALL-5')
		self.assertEqual(cfg.sleep, 300)
		self.assertEqual(cfg.symbol_table, '\\')
		self.assertEqual(cfg.symbol, '>')
		self.assertEqual(cfg.latitude, '12.34')
		self.assertEqual(cfg.server, 'my.server.net')
		self.assertEqual(cfg.port, 10000)
		self.assertTrue(cfg.gpsd_enabled)
		self.assertTrue(cfg.smartbeaconing_enabled)
		self.assertTrue(cfg.telegram_enabled)


class TestSequence(unittest.TestCase):
	def test_sequence_increment(self):
		with patch('builtins.open', mock_open(read_data='10')) as m:
			seq = main.Sequence()
			self.assertEqual(seq.count, 11)
			m().write.assert_called_with('11')

	def test_sequence_wrap(self):
		with patch('builtins.open', mock_open(read_data='999')) as m:
			seq = main.Sequence()
			self.assertEqual(seq.count, 0)
			m().write.assert_called_with('0')


class TestSmartBeaconing(unittest.TestCase):
	def setUp(self):
		self.cfg = MagicMock()
		self.cfg.smartbeaconing_fast_speed = 100
		self.cfg.smartbeaconing_slow_speed = 10
		self.cfg.smartbeaconing_fast_rate = 60
		self.cfg.smartbeaconing_slow_rate = 600
		self.cfg.smartbeaconing_min_turn_angle = 30
		self.cfg.smartbeaconing_turn_slope = 255
		self.cfg.smartbeaconing_min_turn_time = 5
		self.sb = main.SmartBeaconing(self.cfg)

	def test_should_send_stopped(self):
		# Speed 0
		gps_data = (0, 0, 0, 0, 0, 0)
		self.assertFalse(self.sb.should_send(gps_data))

	def test_should_send_rate_expired(self):
		# Speed 50km/h (13.88 m/s)
		gps_data = (0, 0, 0, 0, 13.88, 0)
		# Rate calc: 10 < 50 < 100.
		# rate = 600 - ((50-10)*(600-60)/(100-10)) = 600 - (40*540/90) = 600 - 240 = 360s

		with patch('time.time', return_value=1000):
			self.sb.last_beacon_time = 1000 - 361  # Expired
			self.assertTrue(self.sb.should_send(gps_data))

			self.sb.last_beacon_time = 1000 - 350  # Not expired
			self.assertFalse(self.sb.should_send(gps_data))

	def test_should_send_turn(self):
		# Speed 50km/h
		gps_data = (0, 0, 0, 0, 13.88, 90)
		self.sb.last_course = 0
		self.sb.last_beacon_time = 1000

		# Turn threshold: 30 + 255/50 = 35.1 deg
		# Heading change: 90 > 35.1 -> Turn detected

		with patch('time.time', return_value=1000 + 6):  # > min_turn_time (5)
			self.assertTrue(self.sb.should_send(gps_data))


class TestSystemStats(unittest.TestCase):
	@patch('psutil.getloadavg')
	@patch('psutil.cpu_count')
	def test_avg_cpu_load(self, mock_count, mock_load):
		mock_load.return_value = (0.0, 0.0, 0.4)  # 15 min load
		mock_count.return_value = 4
		stats = main.SystemStats()
		# (0.4 / 4) * 100 * 1000 = 0.1 * 100000 = 10000
		self.assertEqual(stats.avg_cpu_load(), 10000)

	@patch('psutil.virtual_memory')
	def test_memory_used(self, mock_mem):
		mock_mem.return_value.total = 1000
		mock_mem.return_value.free = 200
		mock_mem.return_value.buffers = 100
		mock_mem.return_value.cached = 100
		stats = main.SystemStats()
		self.assertEqual(stats.memory_used(), 600)


class TestAPRSFormatting(unittest.TestCase):
	def test_lat_to_aprs(self):
		self.assertEqual(main._lat_to_aprs(37.7749), '3746.49N')
		self.assertEqual(main._lat_to_aprs(-37.7749), '3746.49S')

	def test_lon_to_aprs(self):
		self.assertEqual(main._lon_to_aprs(122.4194), '12225.16E')
		self.assertEqual(main._lon_to_aprs(-122.4194), '12225.16W')

	def test_alt_to_aprs(self):
		self.assertEqual(main._alt_to_aprs(100), '/A=000328')

	def test_spd_to_knots(self):
		# 10 m/s = 19.438 knots
		self.assertEqual(main._spd_to_knots(10), '019')

	def test_cse_to_aprs(self):
		self.assertEqual(main._cse_to_aprs(180.5), '180')


class TestMainAsync(unittest.IsolatedAsyncioTestCase):
	@patch('main._get_current_location_data')
	@patch('main.send_status')
	async def test_send_position(self, mock_send_status, mock_get_loc):
		# Setup
		ais = MagicMock()
		cfg = MagicMock()
		cfg.call = 'N0CALL'
		cfg.symbol_table = '/'
		cfg.symbol = '>'
		cfg.symbol_overlay = None
		cfg.smartbeaconing_enabled = False

		tg_logger = MagicMock()
		tg_logger.log = MagicMock(return_value=asyncio.Future())
		tg_logger.log.return_value.set_result(None)

		sys_stats = MagicMock()
		sys_stats.mmdvm_info.return_value = 'DMR,'
		sys_stats.os_info.return_value = ' Linux'

		# Mock location: time, lat, lon, alt, spd, cse
		mock_get_loc.return_value = (dt.datetime(2023, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc), 10.0, 20.0, 100.0, 10.0, 180.0)

		# Mock send_status to be awaitable
		f = asyncio.Future()
		f.set_result(ais)
		mock_send_status.return_value = f

		# Run
		await main.send_position(ais, cfg, tg_logger, sys_stats)

		# Verify
		ais.sendall.assert_called()
		call_args = ais.sendall.call_args[0][0]
		self.assertIn('N0CALL>APP642:/011200z1000.00N/02000.00E>', call_args)  # 10.0 lat, 20.0 lon, symbol >


if __name__ == '__main__':
	unittest.main()
