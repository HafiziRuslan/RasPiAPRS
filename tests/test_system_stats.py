import datetime as dt
import os
import sys
import unittest
from unittest import mock

# Add src directory to path to allow importing main
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

from main import SystemStats


class TestSystemStats(unittest.TestCase):
	"""Unit tests for the SystemStats class."""

	def setUp(self):
		"""Set up for tests."""
		self.stats = SystemStats()
		# Patch constants that point to file paths to avoid filesystem interactions.
		self.os_release_patcher = mock.patch('main.OS_RELEASE_FILE', 'dummy/os-release')
		self.mmdvm_host_patcher = mock.patch('main.MMDVMHOST_FILE', 'dummy/mmdvmhost')
		self.os_release_patcher.start()
		self.mmdvm_host_patcher.start()
		self.addCleanup(self.os_release_patcher.stop)
		self.addCleanup(self.mmdvm_host_patcher.stop)

	@mock.patch('psutil.getloadavg', return_value=(0.1, 0.2, 0.5))
	@mock.patch('psutil.cpu_count', return_value=4)
	def test_avg_cpu_load_success(self, mock_cpu_count, mock_getloadavg):
		"""Test successful CPU load calculation."""
		# (0.5 / 4) * 100 * 1000 = 12500
		self.assertEqual(self.stats.avg_cpu_load(), 12500)
		mock_getloadavg.assert_called_once()
		mock_cpu_count.assert_called_once()

	@mock.patch('psutil.getloadavg', side_effect=Exception('Test error'))
	def test_avg_cpu_load_failure(self, mock_getloadavg):
		"""Test CPU load calculation failure."""
		self.assertEqual(self.stats.avg_cpu_load(), 0)

	@mock.patch('psutil.virtual_memory')
	def test_memory_used_success(self, mock_virtual_memory):
		"""Test successful memory usage calculation."""
		mock_vm = mock.Mock()
		mock_vm.total = 8000
		mock_vm.free = 2000
		mock_vm.buffers = 500
		mock_vm.cached = 1500
		mock_virtual_memory.return_value = mock_vm
		# 8000 - 2000 - 500 - 1500 = 4000
		self.assertEqual(self.stats.memory_used(), 4000)

	@mock.patch('psutil.virtual_memory', side_effect=Exception('Test error'))
	def test_memory_used_failure(self, mock_virtual_memory):
		"""Test memory usage calculation failure."""
		self.assertEqual(self.stats.memory_used(), 0)

	@mock.patch('psutil.disk_usage')
	def test_storage_used_success(self, mock_disk_usage):
		"""Test successful disk usage calculation."""
		mock_du = mock.Mock()
		mock_du.used = 50000
		mock_disk_usage.return_value = mock_du
		self.assertEqual(self.stats.storage_used(), 50000)
		mock_disk_usage.assert_called_once_with('/')

	@mock.patch('psutil.disk_usage', side_effect=Exception('Test error'))
	def test_storage_used_failure(self, mock_disk_usage):
		"""Test disk usage calculation failure."""
		self.assertEqual(self.stats.storage_used(), 0)

	@mock.patch('psutil.sensors_temperatures')
	def test_cur_temp_success(self, mock_sensors_temperatures):
		"""Test successful temperature reading."""
		mock_sensor = mock.Mock()
		mock_sensor.current = 45.6
		mock_sensors_temperatures.return_value = {'cpu_thermal': [mock_sensor]}
		# int(45.6 * 10) = 456
		self.assertEqual(self.stats.cur_temp(), 456)

	@mock.patch('psutil.sensors_temperatures', side_effect=Exception('Test error'))
	def test_cur_temp_failure(self, mock_sensors_temperatures):
		"""Test temperature reading failure."""
		self.assertEqual(self.stats.cur_temp(), 0)

	@mock.patch('main.dt.datetime')
	@mock.patch('psutil.boot_time', return_value=1704067200.0)  # 2024-01-01 00:00:00 UTC
	@mock.patch('humanize.naturaldelta')
	def test_uptime_success(self, mock_naturaldelta, mock_boot_time, mock_datetime):
		"""Test successful uptime calculation."""
		mock_now = dt.datetime(2024, 1, 2, 12, 0, 0, tzinfo=dt.timezone.utc)
		mock_datetime.now.return_value = mock_now
		mock_naturaldelta.return_value = 'a day, 12 hours'

		expected_uptime_str = 'up: a day, 12 hours'
		self.assertEqual(self.stats.uptime(), expected_uptime_str)

		expected_delta = dt.timedelta(seconds=129600)  # 1 day and 12 hours
		mock_naturaldelta.assert_called_once_with(expected_delta)

	@mock.patch('psutil.boot_time', side_effect=Exception('Test error'))
	def test_uptime_failure(self, mock_boot_time):
		"""Test uptime calculation failure."""
		self.assertEqual(self.stats.uptime(), '')

	@mock.patch('os.uname')
	def test_os_info_success(self, mock_uname):
		"""Test successful OS info retrieval."""
		os_release_content = """
ID_LIKE="debian"
VERSION_CODENAME="bookworm"
DEBIAN_VERSION_FULL="12 (bookworm)"
"""
		mock_uname_result = mock.Mock()
		mock_uname_result.sysname = 'Linux'
		mock_uname_result.release = '6.1.0-rpi7-rpi-v8+'
		mock_uname.return_value = mock_uname_result

		with mock.patch('builtins.open', mock.mock_open(read_data=os_release_content)):
			# Note: the original function has a slight redundancy in the output string
			expected_info = ' Debian12 (bookworm) (bookworm) [Linux 6.1.0-rpi7-rpi-v8]'
			self.assertEqual(self.stats.os_info(), expected_info)

	@mock.patch('os.uname')
	def test_os_info_file_not_found(self, mock_uname):
		"""Test OS info retrieval when os-release file is not found."""
		mock_uname_result = mock.Mock()
		mock_uname_result.sysname = 'Linux'
		mock_uname_result.release = '6.1.0-rpi7-rpi-v8+'
		mock_uname.return_value = mock_uname_result

		with mock.patch('builtins.open', side_effect=FileNotFoundError):
			expected_info = '  [Linux 6.1.0-rpi7-rpi-v8]'
			self.assertEqual(self.stats.os_info(), expected_info)

	def test_mmdvm_info_success_dmr_enabled(self):
		"""Test successful MMDVM info retrieval with DMR enabled."""
		mmdvm_content = """
[DMR]
Enable=1
[Info]
RXFrequency=438800000
TXFrequency=431200000
ColorCode=1
"""
		with mock.patch('builtins.open', mock.mock_open(read_data=mmdvm_content)):
			expected_info = '431.2MHz (+7.6MHz) CC1,'
			self.assertEqual(self.stats.mmdvm_info(), expected_info)

	def test_mmdvm_info_success_dmr_disabled(self):
		"""Test successful MMDVM info retrieval with DMR disabled."""
		mmdvm_content = """
[DMR]
Enable=0
[Info]
RXFrequency=438800000
TXFrequency=431200000
ColorCode=1
"""
		with mock.patch('builtins.open', mock.mock_open(read_data=mmdvm_content)):
			expected_info = '431.2MHz (+7.6MHz),'
			self.assertEqual(self.stats.mmdvm_info(), expected_info)

	def test_mmdvm_info_file_not_found(self):
		"""Test MMDVM info retrieval when mmdvmhost file is not found."""
		with mock.patch('builtins.open', side_effect=FileNotFoundError):
			expected_info = '0.0MHz,'
			self.assertEqual(self.stats.mmdvm_info(), expected_info)


if __name__ == '__main__':
	unittest.main()