#!/usr/bin/env python
#
#    Proce-based charging - Based on electricity prices charge battery
#
#    Author E Zuidema
#
# Release 2020-07-28 Initial version
# Release 2021-01-04 Updated teslajson to 1.5.1, trying to get rid of pastebin request
# Release 2023-09-30 Moved to TeslaPy, added Energy prices and flow
# Release 2023-10-01 Do not pull vehicle online, check status first
# Release 2023-12-30 TESLA API changed, fixed some fields like latitude
# Release 2024-01-03 Cleaned up and added infinite loop for running as a systemd service
# Release 2024-01-07 Overhauled the main flow
# Release 2024-01-26 Fixed vehicle list error due to Tesla changing to Fleet API
# Release 2024-02-03 Added one wake-up on service start to get vehicle location
# Release 2024-02-10 Replaced get_vehicle_summary in some cases with get_vehicle_data to not get cached data
# Release 2024-02-16 Put Tesla API calls in functions
# Release 2024-02-19 Added Google Calendar API
# Release 2024-03-04 Fixed Maps NOT FOUND result
# Release 2024-05-08 Fixed Python 3.12 utcnow() depreciation
# Release 2024-05-27 Fixed Charge Limit issues
# Release 2024-07-12 Misc bug fixes including additional exception handling
# Release 2024-10-20 Added Home Battery setting to prevent fast discharging to charge car
# Release 2024-11-18 Replaced Tesla API with Tessie API as Model 3 only supports Fleet API
# Release 2024-12-09 Replaced arguments with config file
# Release 2024-12-15 Upgraded to MySQL
# Release 2024-12-30 Changed: No agenda or electricity acton when car not at home, changed table to electricity_prices
# Release 2024-12-30 Fixed multi-attendee appointment by checking acceptance status
# Release 2025-02-14 Moved Tessie token to config file
# Release 2025-02-19 Fixed meeting duration overwriting charging slot
# Release 2025-06-09 Fixed setting home battery to idle and back
#
# To Do:
# * Warn with overlapping events (conflict for use of the car!)
#
# Note from teslapy author Tim Dorr: https://github.com/timdorr/tesla-api/discussions/525
#     GET /api/1/vehicles/{id}/data_request/charge_state does keep the vehicle awake
# but GET /api/1/vehicles/{id}/vehicle_data              does NOT.
#
# Used some parts on Google Calendar and Maps from https://raw.githubusercontent.com/samsipe/intelligent-charge-scheduler/main/scheduler.py

progname = "price_based_charging_Tessie_MySQL.py"
version  = "2025-06-09"

import sys
import os
import math
import logging
from time import sleep
from datetime import datetime as DateTime
from datetime import timedelta, timezone
import argparse
from configparser import ConfigParser
import mysql.connector
import teslapy
import solaredge_modbus
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import requests, json
from urllib.parse import quote

CHECK_LOOP_SLEEP_TIME = 600 # loop to check the car (seconds)

HOME_LAT = 51.387
HOME_LON = 5.578

# API always returns miles, so need to recalc
MILES_TO_KM = 1.609344

# Typically using 170-230 Wh per km, use 'worst' case
KWH_PER_KILOMETER = 0.250
# Battery capacity in kWh
BATTERY_CAPACITY  = 100

CHARGE_RETURNING  = 10 # Percent to always have left afer trip
CHARGE_MINIMUM    = 32 # Percent to fill always (even if expensive electricity)
CHARGE_CHEAP      = 49 # Percent to fill with cheap energy (keep buffer for very cheap energy)
CHARGE_DEFAULT    = 50 # Percent set in app for this script to take over
CHARGE_VERY_CHEAP = 98 # Percent to fill with very cheap energy (keep 2% for re-gen braking)

CHARGE_VOLTAGE  = 0.230 # Average charging voltage (kVolt)
CHARGE_PHASES   = 3     # Always charging with 3 phases in parallel
CHARGE_AMPS_MAX = 13    # Max Amps to charge the car with
CHARGE_POWER    = CHARGE_AMPS_MAX * CHARGE_PHASES * CHARGE_VOLTAGE # in kW
CHARGE_TIME_MAX = BATTERY_CAPACITY / CHARGE_POWER # kWh / kW = h

# Globals
logger = None
stored_battery_mode = [1, 0] # Maximize self-consumption; should be overridden in init

# Tessie basics; credentials in config file
TESSIE_URL      = "https://api.tessie.com/"

# Google Maps
MAPS_BASE_URL = 'https://maps.googleapis.com/maps/api/distancematrix/json?'

# Cache data to not wake up vehicle
last_seen = None
last_lat = None
last_lon = None
last_level = None
my_limit = None

def parse_arguments(logger):
  # Commandline arguments parsing
  parser = argparse.ArgumentParser(description='Charge Tesla based on charge level and energy price', epilog="Copyright (c) E. Zuidema")
  parser.add_argument("-l", "--log", help="Logging level, can be 'none', 'info', 'warning', 'debug', default='warning'", default='warning', type=str.lower)
  parser.add_argument("-f", "--logfile", help="Logging output, can be 'stdout', or filename with path, default='stdout'", default='stdout')
  parser.add_argument("-c", "--configfile", help="Filename with path to ini configuration file, default='config.ini'", default='config.ini')
  args = parser.parse_args()

  if (args.logfile == 'stdout'):
    if (args.log == 'info'):
      # info logging to systemd which already lists timestamp
      logging.basicConfig(format='%(name)s - %(message)s', level=logging.WARNING)
    else:
      logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(lineno)d - %(message)s', level=logging.WARNING)
  else:
    logging.basicConfig(filename=args.logfile, format='%(asctime)s - %(levelname)s - %(lineno)d - %(message)s', level=logging.WARNING)

  if (args.log == 'debug'):
    logger.setLevel(logging.DEBUG)
  if (args.log == 'info'):
    logger.setLevel(logging.INFO)
  if (args.log == 'warning'):
    logger.setLevel(logging.WARNING)
  if (args.log == 'error'):
    logger.setLevel(logging.ERROR)

  # return config file
  return args.configfile

def get_electricity_prices():
  # Connect to DB and get upcoming hourly electricity costs
  try:
    db = mysql.connector.connect(user="p1user", password="P1Password", host="localhost", database="energy")
    c = db.cursor()
    logger.debug("Connected to energy database")
  except mysql.connector.Error as e:
    logger.error("ERROR: %s" % e)
    return -1

  # Get prices from Tibber database
  query = "SELECT datetime_from, cost_kWh_level, cost_kWh_total FROM `electricity_prices` WHERE datetime_to > UTC_TIMESTAMP ORDER BY datetime_from ASC"
  c.execute(query)
  records = c.fetchall()
  prices = []
  for record in records:
    prices.append({'datetime':record[0].replace(tzinfo=timezone.utc), 'level':record[1], 'price':record[2], 'charge':None})
    logger.debug("Electricity costs from %s (UTC) is %s, E %f/kWh" % (record[0], record[1], record[2]))
  logger.info("Current Electricity costs (from %s UTC) is %s, E %f/kWh" % (prices[0]['datetime'], prices[0]['level'], prices[0]['price']))
  return prices

def update_charge(prices, hours_needed, hours_budget, hours_duration):
  # Mark the optimal hours in prices for charging, based on how many hours needed
  #  and which hours are within the budget (by when the actual charge is needed)
  #  and mark duration hours as no charging (as car is away) (counting from hours_budget)
  #  where optimal means lowest price
  # Make sure the budget is positive (sometimes called with negative number of (too) late for charging)
  #  and current hour slot always candidate for charging if needed
  hours_budget = max(1, hours_budget)
  logger.debug("hours needed %d, hours_budget %d, hours_duration %d" % (hours_needed, hours_budget, hours_duration))

  # First create new pricing array and remove the slots already booked for charging
  newprices = [i for i in prices if i['charge'] == None]
  logger.debug("Charging slots left: %d" % len(newprices))

  # Then order the hours ascending
  orderednewprices = sorted(newprices, key=lambda d: d['datetime'],reverse=False)
  logger.debug("Amount of ordered charging slots: %d" % len(orderednewprices))
#  logger.debug("Ordered charging slots: %s" % orderednewprices)

  # Then cut the prices to the budget (when we need to have charged the car)
  cutprices = orderednewprices[:min(len(orderednewprices), int(hours_budget))]
  logger.debug("Amount of charging slots in budget (before the event): %d" % len(cutprices))
#  logger.debug("Charging slots in budget (before the event): %s" % cutprices)

  # Then order the remaining hours ascending by price (and for same prices ascending per hour)
  orderedcutprices = sorted(cutprices, key=lambda d: (d['price'], d['datetime']),reverse=False)
  logger.debug("Amount of ordered charging slots in budget: %d" % len(orderedcutprices))
#  logger.debug("Ordered charging slots in budget: %s" % orderedcutprices)

  # Then select the amount of hours (=slots) needed
  cutorderedcutprices = orderedcutprices[:min(len(orderedcutprices), int(hours_needed))]
  logger.debug("Amount of ordered needed charging slots : %d" % len(cutorderedcutprices))
#  logger.debug("Ordered needed charging slots : %s" % cutorderedcutprices)

  # Now enable the charging checkbox in the original array
  for price in prices:
    for selected_price in cutorderedcutprices:
      if price['datetime'] == selected_price['datetime']:
        price['charge'] = True
        logger.debug("Marking slot for charging %s" % price['datetime'])

  # Now mark the duration slots as not fit for charging
  # First order the hours ascending
  orderedprices = sorted(prices, key=lambda d: d['datetime'],reverse=False)
  logger.debug("Amount of ordered charging slots: %d" % len(orderedprices))
#  logger.debug("Ordered charging slots: %s" % orderedprices)

  # Then cut the prices from the budget (when we need to have charged the car) to the duration (when the car is back)
  cutprices = orderedprices[min(len(orderedprices), int(hours_budget+1)):min(len(orderedprices), int(hours_budget+hours_duration+1))]
  logger.debug("Amount of charging slots car is away: %d" % len(cutprices))
#  logger.debug("Charging slots in budget (before the event): %s" % cutprices)

  # Now enable the charging checkbox in the original array
  for price in prices:
    for selected_price in cutprices:
      if price['datetime'] == selected_price['datetime'] and price['charge'] != True:
        price['charge'] = False
        logger.debug("Marking slot for NOT charging %s" % price['datetime'])

  # Return the list of dicts with the new charging slots marked
  return prices

def mark_price_time(prices, datetime, charge):
  # Mark the datetime hour in prices for charging (True) or not (False), or no decision yet ('')
  for price in prices:
    if price['datetime'] == datetime:
      if charge == True:
        logger.debug("Marking slot %s for charging" % price['datetime'])
        price['charge'] = True
      elif charge == False:
        logger.debug("Marking slot %s for NOT charging" % price['datetime'])
        price['charge'] = False
      else: # None, unmark slot
        logger.debug("Unmarking slot %s for charging" % price['datetime'])
        price['charge'] = None
  # Return the list of dicts with the new charging slots marked
  return prices


def auth_google(calendar_token_file = "google_token.json"):
  creds = None
  SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
  # The file token.json stores the user's access and refresh tokens, and is
  # created automatically when the authorization flow completes for the first
  # time.
  if os.path.exists(calendar_token_file):
    creds = Credentials.from_authorized_user_file(calendar_token_file, SCOPES)
  # If there are no (valid) credentials available, let the user log in
  if not creds or not creds.valid:
    if creds and creds.expired and creds.refresh_token:
      try:
        creds.refresh(Request())
      except Exception as e:
        logger.error("Could not refresh Google credentials: %s" % e)
        return None
    else:
      logger.warning("Need to refresh Google credentials")
      # EZ: the credentials.json file has been downloaded from https://console.cloud.google.com/apis/credentials
      flow = InstalledAppFlow.from_client_secrets_file("google_credentials.json", SCOPES)
      # EZ: This needs to run on venserver and opens a browser window
      # EZ: The URL localhost:8888 has been explicitly allowed on https://console.cloud.google.com/apis/credentials
#      creds = flow.run_local_server(host='localhost', port=8888)
#      creds = flow.run_local_server(bind_addr="0.0.0.0", open_browser=False, port=8081)
    # Save the credentials for the next run
#    with open(calendar_token_file, "w") as token:
#      token.write(creds.to_json())
  return creds

def get_directions(key, dest):
  # Returns the distance from HOME to dest in meters and duration in seconds
  total_url = MAPS_BASE_URL + 'origins=' + str(HOME_LAT) + quote(",") + str(HOME_LON) + '&destinations=' + quote(dest) + '&key=' + key
  response = requests.get(total_url).json()
  logger.debug("Maps response: %s" % response)
  if response['rows'][0]['elements'][0]['status'] == 'OK':
    # Return the distance in meters and duration in seconds
    distance = response['rows'][0]['elements'][0]['distance']['value']
    duration = response['rows'][0]['elements'][0]['duration']['value']
    logger.debug("Distance: %d meters, duration %d seconds" % (distance, duration))
    return (distance, duration)
  else:
    logger.warning("No valid route found for destination '%s'" % dest)
    return (0, 0)

def get_calendar_events(calendar_credentials, calendar_name, maps_key, hours=24, max_results=10):
  try:
    service = build("calendar", "v3", credentials=calendar_credentials)
  except Exception as e:
    logger.error("An error occurred: %s" % e)
  # utcnow() apparenly depricated
  now = DateTime.now(timezone.utc).isoformat()
  logger.debug("Calendar Now: %s" % now)
  time_limit = (DateTime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
  logger.debug("Limit loading events until %s" % time_limit)

  # Find the correct calendar in the calendars the user has access too
  found = False
  calendar_list = service.calendarList().list().execute()
  for calendar_list_entry in calendar_list['items']:
    logger.debug("Found calendar: %s (id: %s)" % (calendar_list_entry['summary'], calendar_list_entry['id']))
    if calendar_list_entry['summary'] == calendar_name:
      calendar_id = calendar_list_entry['id']
      logger.info("Calendar %s found" % calendar_name)
      found = True
      break
  if not found:
    logger.warning("Calendar %s NOT found, continuing without events" % calendar_name)
    return (0, [])

  events_result = (
      service.events().list(
        calendarId=calendar_id,
        timeMin=now,
        timeMax=time_limit,
        maxResults=max_results,
        singleEvents=True,
        orderBy="startTime",
      ).execute()
  )
  events = events_result.get("items", [])

  # Only keep events with a location
  valid_events = []
  total_distance = 0
  for event in events:
    if "location" in event and "http" not in event["location"] and 'T' in str(event["start"]):
      start = event["start"].get("dateTime", event["start"].get("date"))
      end = event["end"].get("dateTime", event["end"].get("date"))
      location = event["location"].replace("\n", " ")
      summary = event["summary"]
      logger.debug("At '%s' until '%s' '%s' @ '%s'" % (str(start), str(end), summary, location))
      # Get the distance (m) and time (s) it takes to get there
      event["distance"], event["time"] = get_directions(maps_key, location)
      if event["time"] > 100:
        recharges = math.floor(event["distance"] / 1000 * KWH_PER_KILOMETER / BATTERY_CAPACITY)
        if recharges > 0:
          # Need to charge on the way; assume 1 hour per full charge
          event["time"] += recharges * 3600
          logger.debug("Added %d recharges to time to arrive at event" % recharges)
        valid_events.append(event)
        total_distance += event["distance"]
        distance = round(event["distance"] / 1000, 1)
        time = round(event["time"] / 60, 1)
        kwh = distance * KWH_PER_KILOMETER
        logger.info("At %s until %s %s @ %s (will take %.1f minutes at %.1f km distance using %d kWh)" % (str(start), str(end), summary, location, time, distance, kwh))
      else:
        logger.debug("Not added to events as too close: At '%s' until '%s' '%s' @ '%s'" % (str(start), str(end), summary, location))
    else:
      logger.debug("Not added to events as no time or location: At '%s' '%s'" % (event["start"].get("dateTime", event["start"].get("date")), event["summary"]))

  if not valid_events:
    logger.info("No valid upcoming events found.")
  return (total_distance, valid_events)

def vehicle_exists_db(vin, cursor):
  query  = "SELECT datetime FROM `tesla_data` WHERE vin = '" + vin + "' ORDER BY datetime DESC LIMIT 1"
  try:
    cursor.execute(query)
    records = cursor.fetchall()
  except Exception as e:
    logger.error("Cannot execute query on vehicle database: %s" % e)
    return None
  logger.debug("Amount of records %d" % len(records))
  return len(records) == 1

def vehicle_exists_tessie(vin, headers):
  # get_vehicle should not wake up the car
  candidate = None
  try:
    url = TESSIE_URL + "vehicles?only_active=false"
    response = requests.get(url, headers=headers)
    products = response.json()['results']
  except Exception as e:
    logger.error("Product List not available: %s" % e)
    return None
  for product in products:
    # Find a product with VIN (car) and then the right VIN
    if 'vin' in product and product['vin'] == vin:
      logger.debug("Vehicle found")
      return True
  # Not found
  logger.error("No vehicle found")
  return False

def get_vehicle_data_from_tessie(vin, headers):
  # Only used for realtime feedback when executing command. Otherwisa data comes from DB
  # get_vehicle_data should not wake up the car, using server side cache
  # Returns a hierarchical list of different parameters, translated in flat structure
  # Note that (with Tessie) '/state' does not represent the current 'status', use get_vehicle_status for that!
  try:
    url = TESSIE_URL + vin + "/state"
    response = requests.get(url, headers=headers)
  except Exception as e:
    logger.error("Vehicle state not available: %s" % e)
    return None
  carstate = response.json()
#  logger.debug("Vehicle state: %s" % carstate)
  # Translate to our flat python dictionary
  cardata = {}
  cardata['display_name']               = carstate['display_name']
  cardata['last_seen']                  = carstate['charge_state']['timestamp']
  cardata['battery_level']              = carstate['charge_state']['battery_level']
  cardata['battery_limit']              = carstate['charge_state']['charge_limit_soc']
  cardata['charge_port']                = carstate['charge_state']['charge_port_latch']
  cardata['charge_rate']                = carstate['charge_state']['charge_rate']
  cardata['charge_amps']                = carstate['charge_state']['charge_amps']
  cardata['charger_actual_current']     = carstate['charge_state']['charger_actual_current']
  cardata['charge_current_request']     = carstate['charge_state']['charge_current_request']
  cardata['charge_current_request_max'] = carstate['charge_state']['charge_current_request_max']
  cardata['latitude']                   = carstate['drive_state']['latitude']
  cardata['longitude']                  = carstate['drive_state']['longitude']

  logger.debug("Cardata from Tessie: %s" % cardata)
  return cardata

def get_vehicle_data_from_db(vin, cursor):
  # get_vehicle_state should not wake up the car, using server side cache
  # `vehicle_id`, `vin`, `display_name`, `car_type`, `datetime`, `state`, `charge_port`, `battery_level`, `battery_limit`,
  # `battery_range`, `battery_heater`, `charge_rate`, `speed`, `latitude`, `longitude`, `heading`, `odometer`

  # returning cardata
  # cardata['display_name'] String
  # cardata['vin'] 5Y...
  # cardata['charge_state']
  #            ['charge_amps']
  #            ['charger_actual_current']
  #            ['charge_rate'] up to 44 (km/h or miles/h?)
  #            ['charge_port'] Latched?
  #            ['charge_current_request']
  #            ['charge_current_request_max']
  #            ['battery_level'] current level in %
  #            ['charge_limit_soc'] max level in %
  #            ['timestamp'] (last seen)
  # cardata['drive_state']
  #            ['latitude'] ['longitude'] Floats
  # cardata['gui_settings']
  #            ['gui_charge_rate_units'] 

  # Get the last entry with data
  query  = "SELECT `display_name`, `datetime`, `state`, `charge_port`, `battery_level`, `battery_limit`, "
  query += "`battery_range`, `charge_rate`, `latitude`, `longitude` FROM `tesla_data` "
  query += "WHERE vin = '" + vin + "' AND battery_level IS NOT NULL ORDER BY datetime DESC LIMIT 1"
  try:
    cursor.execute(query)
    record = cursor.fetchall()[0]
  except Exception as e:
    logger.error("Cannot get vehicle data from DB: %s" % e)

  logger.debug("Vehicle data: %s (last seen %s UTC)" % (str(record), record[1]))
  # Translate to our flat python dictionary
  cardata = {}
  cardata['display_name']               = record[0]
  cardata['last_seen']                  = record[1]
  cardata['battery_level']              = record[4]
  cardata['battery_limit']              = record[5]
  cardata['battery_range']              = record[6]
  cardata['charge_port']                = record[3]
  cardata['charge_rate']                = record[7]
  #cardata['charge_amps']                = None
  #cardata['charger_actual_current']     = None
  #cardata['charge_current_request']     = None
  #cardata['charge_current_request_max'] = None
  cardata['latitude']                   = float(record[8])
  cardata['longitude']                  = float(record[9])

  logger.debug("Vehicle data from DB: %s" % cardata)
  return cardata


def get_vehicle_status_from_tessie(vin, headers):
  # Gives the car status with Tessie (asleep, waiting_for_sleep or awake)
  try:
    url = TESSIE_URL + vin + "/status"
    response = requests.get(url, headers=headers)
  except Exception as e:
    logger.error("Vehicle state not available: %s" % e)
    return None
  carstatus = response.json()['status']
  # Translate Tessie status to online or offline
  if carstatus in {'awake', 'waiting_for_sleep'}: carstatus = 'online'
  elif carstatus in {'asleep'}: carstatus = 'offline'
  else:
    logger.warning("Unrecognized car status from Tessie %s" % carstatus)
    carstatus = None
  return carstatus

def get_vehicle_status_from_db(vin, cursor):
  # Gives the car status (online or offline) from DB (state: parked, offline, driving, charging, asleep)
  query  = "SELECT datetime, state FROM `tesla_data` WHERE vin = '" + vin + "' ORDER BY datetime DESC LIMIT 1"
  try:
    cursor.execute(query)
    records = cursor.fetchall()
  except Exception as e:
    logger.error("Cannot get vehicle status from DB: %s" % e)

  logger.debug("Vehicle state records: %s" % records)
  record = records[0]
  last_update = record[0]
  carstatus = record[1]
  logger.debug("Vehicle state: %s (last check %s UTC)" % (carstatus, last_update))
  # Translate DB status to online or offline
  if carstatus in {'parked', 'driving', 'charging'}: carstatus = 'online'
  elif carstatus in {'offline', 'asleep'}: carstatus = 'offline'
  else:
    logger.warning("Unrecognized car status in DB %s" % carstatus)
    carstatus = None
  return carstatus

def wake_up(vin, headers):
  # Wake up the vehicle
  try:
    url = TESSIE_URL + vin + "/wake"
    response = requests.get(url, headers=headers)
  except Exception as e:
    logger.error("Vehicle did not wake up: %s" % e)
  if response.json()['result']:
    logger.info("Vehicle should now be woken up (response True)")
  else:
    logger.info("Vehicle did not wake up; request response is False")
  # Log 10 times after 5 seconds
  for _ in range(10):
    # Need real-time data, so using Tessie instead of DB
    status = get_vehicle_status_from_tessie(vin, headers)
    logger.debug("Vehicle status: %s" % status)
    if status == "online":
      break
    sleep(5)

# NOT NEEDED??? Not called now...
def set_charge_current(vin, headers, amps):
  try:
    url = TESSIE_URL + vin + "/command/set_charging_amps?amps=" + str(amps) 
    response = requests.get(url, headers=headers)
    logger.info("Charging amps set to %dA" % amps)
  except Exception as e:
    logger.error("Cannot change charging amps: %s" %e)
  # Log 10 times after 10 seconds
  for _ in range(10):
    try:
      cardata = get_vehicle_state(vin)
#      logger.debug("cardata %s" % cardata)
      current = cardata['charge_amps']
      logger.info("Charging current %dA" % current)
      if current == amps:
        break
    except Exception as e:
      logger.error("set_charge_current: Vehicle data not available: %s" %e)
    sleep(10)

def set_charge_limit(vin, headers, limit):
  # It seems we cannot set a charge limit <50%?!
  try:
    # NOTE that limit needs to be an int to work...
    url = TESSIE_URL + vin + "/command/set_charge_limit?percent=" + str(int(limit))
    response = requests.get(url, headers=headers)
    logger.info("Charge limit set to %d%%" % limit)
  except Exception as e:
    logger.error("Cannot change charge limit: %s" %e)

def set_start_charging(vin, headers):
  try:
    url = TESSIE_URL + vin + "/command/start_charging"
    response = requests.get(url, headers=headers)
    logger.info("Charging started")
  except Exception as e:
    logger.error("Cannot start charging: %s" %e)

  logger.info("Progress:")
  # Log 10 times after 10 seconds
  for _ in range(10):
    # Need real-time data, so using Tessie instead of DB
    cardata = get_vehicle_data_from_tessie(vin, headers)
#      logger.debug("cardata %s" % cardata)
    try:
      rate = cardata['charger_actual_current'] * MILES_TO_KM
      logger.info("Charging with %d km/h" % rate)
      if rate > 0:
        break
    except Exception as e:
      logger.warning("Charge rate not available")
    sleep(10)

def set_stop_charging(vin, headers):
  try:
    url = TESSIE_URL + vin + "/command/stop_charging"
    response = requests.get(url, headers=headers)
    logger.info("Stopping charging")
  except Exception as e:
    logger.error("Cannot stop charging: %s" %e)

  logger.info("Progress:")
  # Log 10 times after 10 seconds
  for _ in range(10):
    # Need real-time data, so using Tessie instead of DB
    cardata = get_vehicle_data_from_tessie(vin, headers)
#    logger.debug("cardata %s" % cardata)
    try:
      rate = cardata['charger_actual_current'] * MILES_TO_KM
      logger.info("Charging with %d km/h" % rate)
      if rate == 0:
        break
    except Exception as e:
      logger.warning("Charge rate not available")
    sleep(10)

def set_inverter_mode(inverter, mode):
  global stored_battery_mode
  # mode is either 'active' or 'idle'
  logger.debug("setting inverter to %s" % mode)
  
  # First make sure we are still connected
  inverter.connect()

  if mode == 'active':
    # Not charging car, so set to mode that was active when started up (global 'stored_battery_mode')
    try:
      inverter.write("storage_control_mode", stored_battery_mode[0])
      inverter.write("rc_cmd_mode", stored_battery_mode[1])
    except Exception as e:
      logger.error("Cannot write inverter: %s" %e)
      return
  else:
    # Charging car, set battery to idle
    # Check if the Inverter is in the Remote Control mode (4)
    storage_control_mode = inverter.read("storage_control_mode")
    if storage_control_mode["storage_control_mode"] != 4:
      logger.info("Inverter not in Remote Control mode, re-setting to this mode")
      inverter.write("storage_control_mode", 4)
      sleep(2)
    # Now set the command mode to idle (0)
    try:
      inverter.write("rc_cmd_mode", 0)
    except Exception as e:
      logger.error("Cannot write inverter cmd mode: %s" %e)
      return
    # Check if mode setting worked
    sleep(2)
    try:
      value = inverter.read("rc_cmd_mode")
    except Exception as e:
      logger.error("Cannot read inverter cmd mode: %s" %e)
      return
    if value["rc_cmd_mode"] != 0:
      # Setting failed
      logger.warning("Failed setting rc_cmd_mode: trying to set to 0, current value %s" % (mode, value["rc_cmd_mode"]))


def main():
  # Main program
  global logger, stored_battery_mode
  global VIN, HOME_LAT, HOME_LON, KWH_PER_KILOMETER, BATTERY_CAPACITY, CHARGE_RETURNING, CHARGE_MINIMUM
  global CHARGE_CHEAP, CHARGE_DEFAULT, CHARGE_VERY_CHEAP, CHARGE_PHASES, CHARGE_AMPS_MAX
  global my_limit, last_lat, last_lon, last_seen, last_level

  print("%s - %s Version %s" % (DateTime.now().strftime("%Y-%m-%d %H:%M:%S"), progname, version))

  logger = logging.getLogger(progname)
  logger.info("Started program %s, version %s" % (progname, version))
  # Parse arguments
  config_file = parse_arguments(logger)
  logging.getLogger("teslapy").setLevel(logger.getEffectiveLevel())
  # Load config file and assign values
  parser = ConfigParser(inline_comment_prefixes="#")
  parser.read(config_file)

  # Calendar to read trips from
  calendar = parser.get("Calendar", "calendar", fallback = None)
  # Inverter to steer home battery with
  inverter_host = parser.get("Inverter", "inverter_host", fallback = None)
  inverter_port = parser.getint("Inverter", "inverter_port", fallback = 1502)
  inverter_unit = parser.getint("Inverter", "inverter_unit", fallback = 1)
  # Database to ready vehicle status from
  mysql_host     = parser.get("Database", "mysql_host", fallback = 'localhost')
  mysql_database = parser.get("Database", "mysql_database", fallback = 'vehicles')
  mysql_user     = parser.get("Database", "mysql_user", fallback = None)
  mysql_passwd   = parser.get("Database", "mysql_passwd", fallback = None)

  # Tessie header to send commands to car
  tessie_token   = parser.get("Tessie", "ACCESS_TOKEN", fallback = None)
  if tessie_token is None:
    logger.warning("No Tessie token provided, cannot control the car")
  tessie_headers = { "accept": "application/json", "authorization": "Bearer " + str(tessie_token) }

  # Google Maps key to calculate trip time & distance
  maps_key = parser.get("Directions", "MAPS_API_KEY", fallback = None)
  if maps_key is None:
    logger.warning("No Google Maps token provided, cannot calculate trip times & distances")

  # Vehicle to charge
  VIN = parser.get("Tesla", "VIN", fallback = None)
  if VIN is None:
    logger.error("No VIN provided")
    exit(-1)
  HOME_LAT = parser.getfloat("Tesla", "HOME_LAT", fallback = HOME_LAT)
  HOME_LON = parser.getfloat("Tesla", "HOME_LON", fallback = HOME_LON)
  KWH_PER_KILOMETER = parser.getfloat("Tesla", "KWH_PER_KILOMETER", fallback = KWH_PER_KILOMETER)
  BATTERY_CAPACITY  = parser.getint("Tesla", "BATTERY_CAPACITY", fallback = BATTERY_CAPACITY)
  CHARGE_RETURNING  = parser.getint("Tesla", "CHARGE_RETURNING", fallback = CHARGE_RETURNING)
  CHARGE_MINIMUM    = parser.getint("Tesla", "CHARGE_MINIMUM", fallback = CHARGE_MINIMUM)
  CHARGE_CHEAP      = parser.getint("Tesla", "CHARGE_CHEAP", fallback = CHARGE_CHEAP)
  CHARGE_DEFAULT    = parser.getint("Tesla", "CHARGE_DEFAULT", fallback = CHARGE_DEFAULT)
  CHARGE_VERY_CHEAP = parser.getint("Tesla", "CHARGE_VERY_CHEAP", fallback = CHARGE_VERY_CHEAP)
  CHARGE_PHASES     = parser.getint("Tesla", "CHARGE_PHASES", fallback = CHARGE_PHASES)
  CHARGE_AMPS_MAX   = parser.getint("Tesla", "CHARGE_AMPS_MAX", fallback = CHARGE_AMPS_MAX)

  # Prepare for loading Google Calendar entries
  logger.debug("Initiating Google Calendar connection")
  google = auth_google()
  if google is None:
    logger.error("Not authorized, skipping Calendar events")

  # Connecting with Solar Inverter for Home Battery control
  if inverter_host:
    try:
      inverter = solaredge_modbus.Inverter(host=inverter_host, port=inverter_port, unit=inverter_unit)
      storage_control_mode = inverter.read("storage_control_mode")
      rc_cmd_mode = inverter.read("rc_cmd_mode")
      logger.info("Inverter Storage Control Mode %d, Remote Command Mode %d" % (storage_control_mode["storage_control_mode"], rc_cmd_mode["rc_cmd_mode"]))
      stored_battery_mode = [storage_control_mode["storage_control_mode"], rc_cmd_mode["rc_cmd_mode"]]
      inverter.disconnect()
    except Exception as e:
      logger.error("Can't connect to the inverter: %s" % str(e))
      exit(-1)

  # Connect to vehicle database
  logger.info("Opening vehicle status MySQL Database %s on %s...", mysql_database, mysql_host)
  try:
    db = mysql.connector.connect(user=mysql_user, password=mysql_passwd, host=mysql_host, database=mysql_database)
    cursor = db.cursor()
    logger.info("Opened MySQL Database successfully")
  except Exception as e:
    logger.error("Can't open the database: %s" % str(e))
    exit(-1)

  logger.debug("Initiating Tessie and vehicle database")
  # See if the car with VIN can be found in Tessie API and vehicle database
  # This does not wake up the car, but just a Cloud API call...
  if not vehicle_exists_tessie(VIN, tessie_headers) or not vehicle_exists_db(VIN, cursor):
    logger.error("Could not find the car, exiting...")
    sys.exit(1)

  # Load the dictionary with vehicle data from DB
  cardata = get_vehicle_data_from_db(VIN, cursor)
  logger.info("Vehicle information:")
  logger.info("Car name: %s" % cardata['display_name'])
  logger.info("VIN: %s" % VIN)

  # Initiating with car at home, and minimum charge needed
  car_away = False
  charge_needed = CHARGE_MINIMUM
  first_run = True

  # Main loop
  try: # To catch potential keyboard interrupt used while debugging
    while True:
      # Apparently cursors do not see table updates, so closing cursor every loop
      cursor.close()
      # skip commit and waiting if first loop 
      if not first_run:
        # Check if MySQL is still connected
        if not db.is_connected():
          logger.debug("DB not anymore connected, using ping to reconnect")
          db.ping(True)
          logger.debug("DB connection status: %s" % db.is_connected())
        else:
          # Apparently also need to do commit to not get same data in db fetch...
          logger.debug("Committing DB")
          db.commit()
        # Wait some time before doing next car probe
        sleep(CHECK_LOOP_SLEEP_TIME)
      else:
        # If first run, skip the above
        logger.debug("First run")
        first_run = False

      logger.info("Starting iteration for %s:" % cardata['display_name'])

      if inverter_host:
        # First connect to the inverter
        inverter = solaredge_modbus.Inverter(host=inverter_host, port=inverter_port, unit=inverter_unit)
        try:
          # Show active Inverter / Battery mode
          storage_control_mode = inverter.read("storage_control_mode")
          rc_cmd_mode = inverter.read("rc_cmd_mode")
          logger.debug("Inverter Storage Control Mode %d, Remote Command Mode %d" % (storage_control_mode["storage_control_mode"], rc_cmd_mode["rc_cmd_mode"]))
          inverter.disconnect()
        except Exception as e:
          logger.warning("Can't read the inverter mode: %s" % str(e))

      # Get new cursor on DB
      cursor = db.cursor()
      # Refresh vehicle data
      cardata = get_vehicle_data_from_db(VIN, cursor)

      # Get the car battery level
      battery_level = cardata['battery_level']

      if (get_vehicle_status_from_db(VIN, cursor) != "online"):
        logger.info("Car offline, last seen at %s (UTC)" % cardata['last_seen'])

      logger.debug("Latest Location: %f, %f" % (cardata['latitude'], cardata['longitude']))

      current_limit = cardata['battery_limit']
#      logger.debug("charge_rate %d km/h, charge_amps %d, charge_current_request %d, charge_current_request_max %d" % \
#        (cardata['charge_rate'], cardata['charge_amps'], cardata['charge_current_request'], cardata['charge_current_request_max']))
      logger.debug("charge_rate %d km/h, charge_amps X, charge_current_request X, charge_current_request_max X" % cardata['charge_rate'])
      if (cardata['charge_rate'] > 0):
        logger.info("Charging at %d km/h (at %d%% of %d%%)" % (cardata['charge_rate'], battery_level, current_limit))
      else:
        logger.info("Not charging (battery level %d%% of %d%%)" % (battery_level, current_limit))

      # All data loaded as much as possible, now see if we should charge
      # Is car at home? LAT or LON delta of 0.001 ~ 100 meter, 0.0001 ~ 10m
      if (abs(cardata['latitude'] - HOME_LAT) > 0.001 or abs(cardata['longitude']- HOME_LON) > 0.001):
        logger.info("No action: Cannot control charging as car not at home")
        logger.debug("Home vs Actual LAT %f vs %f, LON %f vs %f" % (HOME_LAT, cardata['latitude'], HOME_LON, cardata['longitude']))
        if (not car_away):
          logger.info("Car drove out, resetting charge limit to default")
          # Car came home, reset charge limit
          set_charge_limit(VIN, tessie_headers, CHARGE_DEFAULT)
          car_away = True
        continue
      else:
        if (car_away):
          logger.info("Car came home, resetting charge limit to default")
          # Car came home, reset charge limit
          set_charge_limit(VIN, tessie_headers, CHARGE_DEFAULT)
          car_away = False
        else:
          logger.info("Car is at home")

      # Is cable connected?
      if (cardata['charge_port'] != "Engaged"):
        logger.info("No action: No charging possible as cable not connected (charge port %s)" % (cardata['charge_port']))
        continue
      else:
        logger.info("Cable connected")

      # Check if charge limit was overruled by user (and charging to that limit), then not engage
      # EZ: No need to see if charging, as sometimes you want to override charging
#      if (cardata['charge_rate'] > 0):
#        logger.info("Car is charging, checking user override")
      if(current_limit != CHARGE_DEFAULT and current_limit != CHARGE_CHEAP and current_limit != CHARGE_VERY_CHEAP and current_limit != charge_needed):
        logger.info("Limit (%.0f) different than default/cheap/very_cheap and needed (%.0f), assuming user override: No action from this script" % (current_limit, charge_needed))
        continue
      else:
        logger.info("Controlling charge limit (no user override)")
#      else:
#        logger.info("Not charging (no user override)")

      # All requirements met, now charge to needed or electricity price limits

      # Get electricity prices and vehicle data first
      prices = get_electricity_prices()

      # Get Google Calendar entries for next 24 hours, and distance to travel (in meters)
      if google is not None:
        # Get events until our price visbility plus the maximum charge time needed + 2 hours potential driving before event
        (total_distance, events) = get_calendar_events(google, calendar, maps_key, len(prices) + CHARGE_TIME_MAX + 2)
        # Need 4kWh per event (for warming up) and kWh per kilometer to travel (and vv, so x2)
        # total_distance is in meters, so x 1000
        if len(events) > 0:
          kwh_needed = (4*len(events) + total_distance/1000 * KWH_PER_KILOMETER) * 2 + CHARGE_RETURNING
          logger.info("Amount of charge needed for appointments: %d kWh" % kwh_needed)
        else:
          kwh_needed = 0
        # charge_needed in procent, so / BATTERY_CAPACITY x 100%
        charge_needed = math.ceil(max(min(kwh_needed / BATTERY_CAPACITY * 100, 100), CHARGE_MINIMUM))
        logger.debug("Total Distance of %d events is %d km (including vv)" % (len(events), 2*total_distance/1000))
        hours = max((charge_needed - battery_level) / CHARGE_POWER, 0)
        logger.info("Car needs %.1f hour(s) to charge from %d%% to needed level (%.1f%%)" % (hours, battery_level, charge_needed))
        # Mark the prices list with charging slots needed for events
        # charge_left is current battery level minus returning charge, in kWh
        charge_left = (battery_level - CHARGE_RETURNING) * BATTERY_CAPACITY / 100 
        for event in events:
          event_depart = DateTime.strptime(event["start"]['dateTime'], "%Y-%m-%dT%H:%M:%S%z") - timedelta(seconds = event['time'])
          event_home = DateTime.strptime(event["end"]['dateTime'], "%Y-%m-%dT%H:%M:%S%z") + timedelta(seconds = event['time'])
          event_hours_duration = math.ceil((event_home - event_depart).total_seconds() / 3600)
          event_hours_budget = math.floor((event_depart - DateTime.now().astimezone(timezone.utc)).total_seconds() / 3600)
          # event_charge_needed in kWh
          event_charge_needed = (4 + event["distance"]/1000 * KWH_PER_KILOMETER) * 2
          # Can't charge more kWh than battery capacity
          event_charge = math.ceil(min(event_charge_needed, BATTERY_CAPACITY))
          event_hours_needed = math.ceil(max(event_charge - charge_left, 0) / CHARGE_POWER)
          logger.debug("Charge_left (amount of kWh not slotted yet) is %d kWh" % charge_left)
          logger.debug("Event start: %s" % event["start"]['dateTime'])
          logger.debug("Event end: %s" % event["end"]['dateTime'])
          logger.debug("Event distance: %d km" % (event["distance"]/1000))
          logger.debug("Event depart: %s" % event_depart)
          logger.debug("Event home: %s" % event_home)
          logger.debug("Event duration: %d hours" % event_hours_duration)
          # Check if we can charge the car on time
          if event_hours_budget < event_hours_needed:
            logger.warning("Event (%s) charge hours needed %.1f, while budget only %d !!!" % (event["summary"], event_hours_needed, event_hours_budget))            
          if (event_charge_needed > event_charge):
            logger.debug("Event charge needed: %d kWh (maximized to battery capacity %d), hours needed %.1f, budget %d" % (event_charge_needed, event_charge, event_hours_needed, event_hours_budget))
          else:
            logger.debug("Event charge: %d kWh, hours needed %.1f, budget %d" % (event_charge, event_hours_needed, event_hours_budget))
          # Update the price / charging slots with the event needs and budget
          prices = update_charge(prices, event_hours_needed, event_hours_budget, event_hours_duration)
          charge_left = max(charge_left - event_charge, 0)
          #
          # If battery 100% (charge_left = 0), mark slots until meeting as no-charing (for other event)
          #
          if (charge_left == 0):
            logger.debug("Battery fully charged for event, marking events")
            # Skip until True for charging
            for price in prices:
              if price['charge'] == False:
                # We are at the event, so no more marking to be done
                logger.debug("Got start (driving for) event: %s" % price['datetime'])
                break
              if price['charge'] == None:
                # As battery is full, mark the slots as False for charging
                logger.debug("Got not-able-to-charge-anymore slot: %s" % price['datetime'])
                price['charge'] = False
                continue
            
        for price in prices:
          logger.info("Charging slot %s used %s at level %s costs %f" % (price['datetime'], price['charge'], price['level'], price['price']))
      else:
        logger.info("No appointments info as not authenticated with Google")
        # Do not touch the charge_needed
        #charge_needed = 0
      
      # Is the current 1-hour charging slot enabled?
      charge_slot = False
      for price in prices:
        if (DateTime.now(timezone.utc) >= price['datetime']) and (DateTime.now(timezone.utc) < (price['datetime'] + timedelta(hours = 1))) and price['charge'] == True:
          charge_slot = True
          logger.debug("Should charge this slot (slot True)")
          break
      if (battery_level < charge_needed) and charge_slot:
        logger.info("Need to charge this slot (charge needed %d%% > battery level)" % charge_needed)
        # Need to charge
        if (get_vehicle_status_from_db(VIN, cursor) != "online"):
          # Wake up the vehicle
          wake_up(VIN, tessie_headers)

        # Set the Home Battery to Idle, otherwise it will discharge
        if inverter_host: set_inverter_mode(inverter, 'idle')

        if(cardata['battery_limit'] != charge_needed):
          logger.info("Changing charge limit")
          set_charge_limit(VIN, tessie_headers, charge_needed)
        my_limit = charge_needed
#        if(cardata['charge_amps'] != CHARGE_AMPS_MAX):
#          logger.info("Set charging amps to max")
#          set_charge_current(VIN, CHARGE_AMPS_MAX)
        if (cardata['charge_rate'] == 0):
          logger.info("Starting charge")
          set_start_charging(VIN, tessie_headers)
        logger.info("Done, ongoing charge until %d%%" % charge_needed)

      # Remaining 3 charging options (minimum, cheap, very cheap) should start immediately with charging
      # as you never know when the car is unplugged. So no prices / charging list / ordering needed
      elif (battery_level < CHARGE_MINIMUM):
        # Need to charge, independent of electricity price
        hours = (CHARGE_MINIMUM - battery_level) / (CHARGE_AMPS_MAX * 3 * .230)
        logger.info("Car needs %d hour(s) to charge to minimum level" % math.ceil(hours))
        if (get_vehicle_status_from_db(VIN, cursor) != "online"):
          # Wake up the vehicle
          wake_up(VIN, tessie_headers)
        # Unfortunately cannot set to limit below 50%...

        # Set the Home Battery to Idle, otherwise it will discharge
        if inverter_host: set_inverter_mode(inverter, 'idle')

        if(cardata['battery_limit'] != CHARGE_MINIMUM):
          logger.info("Changing charge limit")
          set_charge_limit(VIN, tessie_headers, CHARGE_MINIMUM)
        my_limit = CHARGE_MINIMUM
#        if(cardata['charge_amps'] != CHARGE_AMPS_MAX):
#          logger.info("Set charging amps to max")
#          set_charge_current(VIN, CHARGE_AMPS_MAX)
        if (cardata['charge_rate'] == 0):
          logger.info("Starting charge")
          set_start_charging(VIN, tessie_headers)
        logger.info("Done, ongoing charge until %d%%" % CHARGE_MINIMUM)

      elif (prices[0]['level'] == "CHEAP" and battery_level < CHARGE_CHEAP):
        # Opportunity to charge cheaply
        hours = (CHARGE_CHEAP - battery_level) / (CHARGE_AMPS_MAX * 3 * .230)
        logger.info("Car needs %d hour(s) to charge to cheap level" % math.ceil(hours))
        if (get_vehicle_status_from_db(VIN, cursor) != "online"):
          # Wake up the vehicle
          wake_up(VIN, tessie_headers)
        # Unfortunately cannot set to limit below 50%...

        # Set the Home Battery to Idle, otherwise it will discharge
        if inverter_host: set_inverter_mode(inverter, 'idle')

        if(cardata['battery_limit'] != CHARGE_CHEAP):
          logger.info("Changing charge limit")
          set_charge_limit(VIN, tessie_headers, CHARGE_CHEAP)
        my_limit = CHARGE_CHEAP
#        if(cardata['charge_amps'] != CHARGE_AMPS_MAX):
#          logger.info("Set charging amps to max")
#          set_charge_current(VIN, CHARGE_AMPS_MAX)
        if (cardata['charge_rate'] == 0):
          logger.info("Starting charge")
          set_start_charging(VIN, tessie_headers)
        logger.info("Done, ongoing charge until %d%%" % CHARGE_CHEAP)

      elif (prices[0]['level'] == "VERY_CHEAP" and battery_level < CHARGE_VERY_CHEAP):
        # Opportunity to charge very cheaply
        hours = (CHARGE_VERY_CHEAP - battery_level) / (CHARGE_AMPS_MAX * 3 * .230)
        logger.info("Car needs %d hour(s) to charge to very cheap level" % math.ceil(hours))
        if (get_vehicle_status_from_db(VIN, cursor) != "online"):
          # Wake up the vehicle
          wake_up(VIN, tessie_headers)

        # Set the Home Battery to Idle, otherwise it will discharge
        if inverter_host: set_inverter_mode(inverter, 'idle')

        if(cardata['battery_limit'] != CHARGE_VERY_CHEAP):
          logger.info("Changing charge limit")
          set_charge_limit(VIN, tessie_headers, CHARGE_VERY_CHEAP)
        my_limit = CHARGE_VERY_CHEAP
#        if(cardata['charge_amps'] != CHARGE_AMPS_MAX):
#          logger.info("Set charging amps to max")
#          set_charge_current(VIN, CHARGE_AMPS_MAX)
        if (cardata['charge_rate'] == 0):
          logger.info("Starting charge")
          set_start_charging(VIN, tessie_headers)
        logger.info("Done, ongoing charge until %d%%" % CHARGE_VERY_CHEAP)

      else:
        # All requirements met, but no cheap energy or above minimum charge
        logger.info("No charging window")
        if (cardata["charge_rate"] > 0):
          logger.info("Stopping charging")
          set_stop_charging(VIN, tessie_headers)
          logger.info("Done, stopped charging until in a window")
        else:
          logger.info("Done, car is not charging, waiting for a charging window")

        # Set the Home Battery back to previously active mode
        if inverter_host: set_inverter_mode(inverter, 'active')

      # end if/elif/else battery level
    # Somehow broke out of the while True loop
    logger.debug("Done while (True)")
  except KeyboardInterrupt:
    print("SIGINT (CRTL-C or systemctl stop), closing")
  finally:
    if (db.is_connected()):
      db.close()
      cursor.close()
      logger.info("MySQL connection is closed")
    # Set the Home Battery back to previously active mode and close the connection
    if inverter_host:
      inverter = solaredge_modbus.Inverter(host=inverter_host, port=inverter_port, unit=inverter_unit)
      set_inverter_mode(inverter, 'active')
      inverter.disconnect()
    print(progname, " DONE.")

if __name__ == '__main__':
  main()
