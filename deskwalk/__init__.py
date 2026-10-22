"""deskwalk: minimal web controller for the Kingsmith WalkingPad A1 Pro.

Modules:
    protocol   byte frames to and from the belt (see PROTOCOL.md)
    fitness    stride length, cadence, calories
    store      data.json (settings and sessions), written atomically
    sessions   session tracking, daily goal, streaks, records
    links      Bluetooth link (bleak) and simulated belt
    worker     Bluetooth thread with its own asyncio loop and command queue
    settings   validation of settings sent by the UI
    web        HTTP server: static files, JSON API, Server-Sent Events
    logs       rotating log file plus the last lines in memory
"""

__version__ = "1.1.0"
