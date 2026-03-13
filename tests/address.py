#!/usr/bin/python3

from geopy.geocoders import Nominatim


def get_address_from_coordinates(latitude, longitude):
	"""
	Get the address from latitude and longitude using Nominatim.

	Args:
		latitude (float): The latitude of the location.
		longitude (float): The longitude of the location.

	Returns:
		str: The formatted address string, or None if not found.
	"""
	geolocator = Nominatim(user_agent='RasPiAPRS-app')
	try:
		location = geolocator.reverse((latitude, longitude), exactly_one=True)
		if location:
			address = location.raw['address']
			return address
		else:
			return None
	except Exception as e:
		print(f'Error getting address: {e}')
		return None


if __name__ == '__main__':
	# Example usage
	lat = float(input('Enter latitude: '))
	lon = float(input('Enter longitude: '))

	address = get_address_from_coordinates(lat, lon)

	if address:
		print(f'Address for ({lat}, {lon}): {address}')
	else:
		print(f'Could not find address for ({lat}, {lon}).')
