# TeslaPriceBasedCharging
Python script to charge Tesla vehicle based on dynamic electricity price, and upcoming Google calendar entries with location

Script rather specific for car (Tesla with Tessie), database (database with energy prices per hour, database with vehicle status), calendar (Google), home battery (SolarEdge), but still maybe interesting for people to build derivatives to achieve a similar functionality.

Example Command line:
`./price_based_charging.py -l debug -c config_example.ini`

# Details
Having multiple Tesla vehicles with one home charger, dynamic electricity prices, a home battery, I wanted to optimize the time and amount of charging of my vehicles.
Starting point was to typically charge the car to 32%, and only upon cheap energy or very cheap energy (categorizations by Tibber) charge the car to 50% or 98% repectively.
Then added functionality to read trips from Google Calendar (I made a special calendar for each car, which I would invite to existing personal calendar entries when plannint to drive there), calculate the neede battery charge, and making an 'ideal' charge plan (cheapest blocks of hours until the required departure time, and blocking out times hen the car would not be at home (based on same calendar).
Finally added that my SolarEdge home battery would not discharge when charging my car; now I put the battery in idle mode so it does not charge nor discharge.

Result is that my cars are always charged enough for my trips, and only charge during the cheapest times to minimize energy costs. What more do you want?

# Reference log file
```
Starting iteration for Model 3:
Charging at 52 km/h (at 74% of 98%)
Car is at home
Cable connected
Controlling charge limit (no user override)
Current Electricity costs (from 2025-06-08 12:00:00+00:00 UTC) is VERY_CHEAP, E 0.083700/kWh
Calendar Tesla Model 3 found
At 2025-06-10T07:30:00+02:00 until 2025-06-10T16:30:00+02:00 Werken @ NS Station Eindhoven, Nederland
Amount of charge needed for appointments: 24 kWh
Car needs 0.0 hour(s) to charge from 74% to needed level (34.0%)
Charging slot 2025-06-08 12:00:00+00:00 used None at level VERY_CHEAP costs 0.083700
Charging slot 2025-06-08 13:00:00+00:00 used None at level VERY_CHEAP costs 0.083600
Charging slot 2025-06-08 14:00:00+00:00 used None at level VERY_CHEAP costs 0.104100
Charging slot 2025-06-08 15:00:00+00:00 used None at level CHEAP costs 0.130700
Charging slot 2025-06-08 16:00:00+00:00 used None at level CHEAP costs 0.147700
Charging slot 2025-06-08 17:00:00+00:00 used None at level NORMAL costs 0.196100
Charging slot 2025-06-08 18:00:00+00:00 used None at level EXPENSIVE costs 0.247700
Charging slot 2025-06-08 19:00:00+00:00 used None at level EXPENSIVE costs 0.262200
Charging slot 2025-06-08 20:00:00+00:00 used None at level VERY_EXPENSIVE costs 0.305100
Charging slot 2025-06-08 21:00:00+00:00 used None at level EXPENSIVE costs 0.284900
Charging slot 2025-06-08 22:00:00+00:00 used None at level EXPENSIVE costs 0.281900
Charging slot 2025-06-08 23:00:00+00:00 used None at level EXPENSIVE costs 0.251700
Charging slot 2025-06-09 00:00:00+00:00 used None at level NORMAL costs 0.226300
Charging slot 2025-06-09 01:00:00+00:00 used None at level NORMAL costs 0.221400
Charging slot 2025-06-09 02:00:00+00:00 used None at level NORMAL costs 0.232200
Charging slot 2025-06-09 03:00:00+00:00 used None at level NORMAL costs 0.232400
Charging slot 2025-06-09 04:00:00+00:00 used None at level NORMAL costs 0.220100
Charging slot 2025-06-09 05:00:00+00:00 used None at level NORMAL costs 0.214000
Charging slot 2025-06-09 06:00:00+00:00 used None at level CHEAP costs 0.153900
Charging slot 2025-06-09 07:00:00+00:00 used None at level CHEAP costs 0.147700
Charging slot 2025-06-09 08:00:00+00:00 used None at level CHEAP costs 0.146500
Charging slot 2025-06-09 09:00:00+00:00 used None at level CHEAP costs 0.143600
Charging slot 2025-06-09 10:00:00+00:00 used None at level CHEAP costs 0.136700
Charging slot 2025-06-09 11:00:00+00:00 used None at level CHEAP costs 0.125300
Charging slot 2025-06-09 12:00:00+00:00 used None at level CHEAP costs 0.128700
Charging slot 2025-06-09 13:00:00+00:00 used None at level CHEAP costs 0.142600
Charging slot 2025-06-09 14:00:00+00:00 used None at level CHEAP costs 0.146600
Charging slot 2025-06-09 15:00:00+00:00 used None at level CHEAP costs 0.150300
Charging slot 2025-06-09 16:00:00+00:00 used None at level NORMAL costs 0.227300
Charging slot 2025-06-09 17:00:00+00:00 used None at level EXPENSIVE costs 0.282300
Charging slot 2025-06-09 18:00:00+00:00 used None at level VERY_EXPENSIVE costs 0.310100
Charging slot 2025-06-09 19:00:00+00:00 used None at level VERY_EXPENSIVE costs 0.325200
Charging slot 2025-06-09 20:00:00+00:00 used None at level VERY_EXPENSIVE costs 0.305000
Charging slot 2025-06-09 21:00:00+00:00 used None at level EXPENSIVE costs 0.264000
Car needs 3 hour(s) to charge to very cheap level
Done, ongoing charge until 98%
```
