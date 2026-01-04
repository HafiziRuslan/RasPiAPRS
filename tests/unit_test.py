import os
import unittest
from unittest.mock import MagicMock, mock_open, patch

# Import functions from main.py
from main import (Config, Sequence, Timer, SmartBeaconing, _mps_to_kmh, configure_logging,get_add_from_pos, get_coordinates, latlon_to_grid)


class TestLatLonToGrid(unittest.TestCase):
  """Test latlon_to_grid function."""

  def test_grid_conversion_basic(self):
    """Test basic grid conversion."""
    result = latlon_to_grid(40.7128, -74.0060, precision=6)
    self.assertEqual(len(result), 6)
    self.assertTrue(result[0].isalpha())

  def test_grid_precision_4(self):
    """Test grid with precision 4."""
    result = latlon_to_grid(0, 0, precision=4)
    self.assertEqual(len(result), 4)

  def test_grid_precision_2(self):
    """Test grid with precision 2."""
    result = latlon_to_grid(0, 0, precision=2)
    self.assertEqual(len(result), 2)


class TestMpsToKmh(unittest.TestCase):
  """Test _mps_to_kmh function."""

  def test_conversion_zero(self):
    """Test conversion of 0 mps."""
    result = _mps_to_kmh(0)
    self.assertEqual(result, '000')

  def test_conversion_positive(self):
    """Test conversion of positive value."""
    result = _mps_to_kmh(10)
    self.assertEqual(result, '036')

  def test_conversion_max_limit(self):
    """Test conversion exceeding max limit."""
    result = _mps_to_kmh(300)
    self.assertEqual(result, '999')


class TestSequence(unittest.TestCase):
  """Test Sequence class."""

  @patch('builtins.open', mock_open(read_data='5'))
  def test_sequence_init_from_file(self):
    """Test Sequence initialization from file."""
    seq = Sequence()
    self.assertEqual(seq._count, 5)

  @patch('builtins.open', mock_open(side_effect=IOError))
  def test_sequence_init_no_file(self):
    """Test Sequence initialization when file doesn't exist."""
    seq = Sequence()
    self.assertEqual(seq._count, 0)

  @patch('builtins.open', mock_open())
  def test_sequence_next(self):
    """Test Sequence next method."""
    with patch('builtins.open', mock_open(read_data='5')):
      seq = Sequence()
    with patch('builtins.open', mock_open()):
      next_val = seq.next()
    self.assertEqual(next_val, 6)

  @patch('builtins.open', mock_open())
  def test_sequence_wraparound(self):
    """Test Sequence wraparound at 999."""
    with patch('builtins.open', mock_open(read_data='998')):
      seq = Sequence()
    with patch('builtins.open', mock_open()):
      next_val = seq.next()
    self.assertEqual(next_val, 999)


class TestTimer(unittest.TestCase):
  """Test Timer class."""

  @patch('builtins.open', mock_open(read_data='100'))
  def test_timer_init_from_file(self):
    """Test Timer initialization from file."""
    tmr = Timer()
    self.assertEqual(tmr._count, 100)

  @patch('builtins.open', mock_open(side_effect=IOError))
  def test_timer_init_no_file(self):
    """Test Timer initialization when file doesn't exist."""
    tmr = Timer()
    self.assertEqual(tmr._count, 0)

  @patch('builtins.open', mock_open())
  def test_timer_next(self):
    """Test Timer next method."""
    with patch('builtins.open', mock_open(read_data='100')):
      tmr = Timer()
    with patch('builtins.open', mock_open()):
      next_val = tmr.next()
    self.assertEqual(next_val, 101)


class TestSmartBeaconing(unittest.TestCase):
  """Test SmartBeaconing class."""

  def setUp(self):
    self.env_patcher = patch.dict(os.environ, {
      'SMARTBEACONING_FASTSPEED': '100',
      'SMARTBEACONING_SLOWSPEED': '10',
      'SMARTBEACONING_FASTRATE': '60',
      'SMARTBEACONING_SLOWRATE': '600',
      'SMARTBEACONING_MINTURNANGLE': '28',
      'SMARTBEACONING_TURNSLOPE': '255',
      'SMARTBEACONING_MINTURNTIME': '5'
    })
    self.env_patcher.start()
    self.sb = SmartBeaconing()

  def tearDown(self):
    self.env_patcher.stop()

  def test_init(self):
    """Test initialization loads config."""
    self.assertEqual(self.sb.fast_speed, 100)
    self.assertEqual(self.sb.slow_speed, 10)

  def test_should_send_no_data(self):
    """Test should_send with no GPS data."""
    self.assertFalse(self.sb.should_send(None))

  @patch('main.time.time')
  def test_should_send_rate_expired_slow(self, mock_time):
    """Test sending when slow rate expired."""
    mock_time.return_value = 1000
    self.sb.last_beacon_time = 300  # 700s ago, > 600s
    # gps_data: (utc, lat, lon, alt, spd, cse)
    # spd = 0 m/s -> 0 km/h (< 10 km/h slow_speed) -> rate = 600
    gps_data = (None, 0, 0, 0, 0, 0)

    self.assertTrue(self.sb.should_send(gps_data))
    self.assertEqual(self.sb.last_beacon_time, 1000)

  @patch('main.time.time')
  def test_should_send_rate_expired_fast(self, mock_time):
    """Test sending when fast rate expired."""
    mock_time.return_value = 1000
    self.sb.last_beacon_time = 900  # 100s ago, > 60s
    # spd = 30 m/s -> 108 km/h (> 100 km/h fast_speed) -> rate = 60
    gps_data = (None, 0, 0, 0, 30, 0)

    self.assertTrue(self.sb.should_send(gps_data))

  @patch('main.time.time')
  def test_should_send_turn_detected(self, mock_time):
    """Test sending when turn detected."""
    mock_time.return_value = 1000
    self.sb.last_beacon_time = 990  # 10s ago (> 5s min_turn_time)
    self.sb.last_course = 0

    # spd = 20 m/s -> 72 km/h.
    # turn_threshold = 28 + 255/72 = ~31.5 degrees.
    # heading_change = 40 degrees (> 31.5).
    gps_data = (None, 0, 0, 0, 20, 40)

    self.assertTrue(self.sb.should_send(gps_data))
    self.assertEqual(self.sb.last_course, 40)

  @patch('main.time.time')
  def test_should_send_turn_too_soon(self, mock_time):
    """Test not sending when turn detected but too soon."""
    mock_time.return_value = 1000
    self.sb.last_beacon_time = 998  # 2s ago (< 5s min_turn_time)
    self.sb.last_course = 0

    # Turn detected
    gps_data = (None, 0, 0, 0, 20, 40)

    self.assertFalse(self.sb.should_send(gps_data))

  @patch('main.time.time')
  def test_should_send_no_turn_not_expired(self, mock_time):
    """Test not sending when no turn and rate not expired."""
    mock_time.return_value = 1000
    self.sb.last_beacon_time = 990  # 10s ago
    self.sb.last_course = 0

    # spd = 20 m/s -> 72 km/h. Rate interpolated between 60 and 600.
    # Definitely > 10s.
    gps_data = (None, 0, 0, 0, 20, 0)  # No course change

    self.assertFalse(self.sb.should_send(gps_data))

  @patch('main.time.time')
  def test_should_send_turn_low_speed(self, mock_time):
    """Test turn detection ignored at low speed."""
    mock_time.return_value = 1000
    self.sb.last_beacon_time = 900
    self.sb.last_course = 0

    # spd = 1 m/s -> 3.6 km/h (< 5 km/h threshold for turn detection in code)
    gps_data = (None, 0, 0, 0, 1, 90)

    # Rate for slow speed is 600. 100s elapsed. Should be False.
    self.assertFalse(self.sb.should_send(gps_data))

  @patch('main.time.time')
  def test_heading_change_wraparound(self, mock_time):
    """Test heading change calculation across 0/360 boundary."""
    mock_time.return_value = 1000
    self.sb.last_beacon_time = 990
    self.sb.last_course = 350

    # New course 10. Diff is 20 (crossing 0).
    # abs(10 - 350) = 340. > 180 -> 360 - 340 = 20.
    # spd = 20 m/s -> 72 km/h. Threshold ~31.5.
    # 20 < 31.5, so no turn detected.
    gps_data = (None, 0, 0, 0, 20, 10)
    self.assertFalse(self.sb.should_send(gps_data))

    # Make it a sharp turn
    # New course 50. Diff 60.
    # abs(50 - 350) = 300. > 180 -> 360 - 300 = 60.
    # 60 > 31.5. Turn detected.
    gps_data_turn = (None, 0, 0, 0, 20, 50)
    self.assertTrue(self.sb.should_send(gps_data_turn))


class TestConfig(unittest.TestCase):
  """Test Config class."""

  @patch.dict(os.environ, {
    'APRS_CALL': 'TEST',
    'APRS_SSID': '1',
    'SLEEP': '300',
    'APRS_SYMBOL_TABLE': '/',
    'APRS_SYMBOL': 'n',
    'APRS_LATITUDE': '40.7128',
    'APRS_LONGITUDE': '-74.0060',
    'APRS_ALTITUDE': '10',
    'GPSD_ENABLE': 'false'
  })
  @patch('main.dotenv.load_dotenv')
  @patch('main.get_coordinates')
  def test_config_init(self, mock_coords, mock_load):
    """Test Config initialization."""
    mock_coords.return_value = (40.7128, -74.0060)
    cfg = Config()
    self.assertEqual(cfg.call, 'TEST-1')
    self.assertEqual(cfg.sleep, 300)

  @patch.dict(os.environ, {'APRS_CALL': 'TEST'})
  @patch('main.dotenv.load_dotenv')
  def test_config_call_property(self, mock_load):
    """Test Config call property."""
    cfg = Config()
    cfg.call = 'NEW'
    self.assertEqual(cfg.call, 'NEW')

  @patch.dict(os.environ, {})
  @patch('main.dotenv.load_dotenv')
  def test_config_sleep_property(self, mock_load):
    """Test Config sleep property with invalid value."""
    cfg = Config()
    cfg.sleep = 'invalid'
    self.assertEqual(cfg.sleep, 600)

  @patch.dict(os.environ, {})
  @patch('main.dotenv.load_dotenv')
  def test_config_port_property(self, mock_load):
    """Test Config port property with invalid value."""
    cfg = Config()
    cfg.port = 'invalid'
    self.assertEqual(cfg.port, 14580)


class TestGetCoordinates(unittest.TestCase):
  """Test get_coordinates function."""

  @patch('main.urlopen')
  def test_get_coordinates_success(self, mock_urlopen):
    """Test successful coordinate retrieval."""
    mock_response = MagicMock()
    mock_response.read.return_value = b'{"lat": 40.7128, "lon": -74.0060}'
    mock_response.__enter__.return_value = mock_response
    mock_urlopen.return_value = mock_response

    result = get_coordinates()
    self.assertEqual(result, (40.7128, -74.0060))

  @patch('main.urlopen')
  def test_get_coordinates_failure(self, mock_urlopen):
    """Test coordinate retrieval failure."""
    mock_urlopen.side_effect = Exception('Connection error')
    result = get_coordinates()
    self.assertEqual(result, (0, 0))

  @patch('main.urlopen')
  def test_get_coordinates_invalid_response(self, mock_urlopen):
    """Test coordinate retrieval with invalid response."""
    mock_response = MagicMock()
    mock_response.read.return_value = b'{"invalid": "data"}'
    mock_response.__enter__.return_value = mock_response
    mock_urlopen.return_value = mock_response

    result = get_coordinates()
    self.assertEqual(result, (0, 0))


class TestGetAddFromPos(unittest.TestCase):
  """Test get_add_from_pos function."""

  @patch('main.os.path.exists')
  @patch('main.pickle.load')
  @patch('main.Nominatim')
  @patch('builtins.open', mock_open())
  def test_get_add_from_pos_from_cache(self, mock_geo, mock_pickle, mock_exists):
    """Test address retrieval from cache."""
    mock_exists.return_value = True
    mock_pickle.return_value = {'40.71,-74.01': {'country_code': 'us'}}

    with patch('builtins.open', mock_open(read_data=b'test')):
      result = get_add_from_pos(40.71, -74.01)

    self.assertEqual(result, {'country_code': 'us'})

  @patch('main.os.path.exists')
  @patch('main.Nominatim')
  def test_get_add_from_pos_error(self, mock_geo, mock_exists):
    """Test address retrieval with error."""
    mock_exists.return_value = False
    mock_geo.return_value.reverse.side_effect = Exception('Geolocation error')

    result = get_add_from_pos(40.71, -74.01)
    self.assertIsNone(result)


class TestConfigureLogging(unittest.TestCase):
  """Test configure_logging function."""

  def test_configure_logging(self):
    """Test logging configuration."""
    configure_logging()
    logger = __import__('logging').getLogger('test')
    self.assertIsNotNone(logger)


if __name__ == '__main__':
  unittest.main()