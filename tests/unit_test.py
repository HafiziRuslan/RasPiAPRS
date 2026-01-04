import os
import unittest
from unittest.mock import MagicMock, mock_open, patch

# Import functions from main.py
from main import (Config, Sequence, Timer, _mps_to_kmh, configure_logging,get_add_from_pos, get_coordinates, latlon_to_grid)


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