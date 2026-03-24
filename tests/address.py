#!/usr/bin/python3

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
	print(
		'        RasPiAPRS  Copyright (C) 2026  HafiziRuslan'
		'      This program comes with ABSOLUTELY NO WARRANTY.'
		'      This is free software, and you are welcome to redistribute it under certain conditions.'
	)
	# Example usage
	lat = float(input('Enter latitude: '))
	lon = float(input('Enter longitude: '))

	address = get_address_from_coordinates(lat, lon)

	if address:
		print(f'Address for ({lat}, {lon}): {address}')
	else:
		print(f'Could not find address for ({lat}, {lon}).')
