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
