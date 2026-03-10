"""
Entry point for `python -m akarins_gateway`.

Loads .env file first, then starts the Hypercorn server.
"""

from dotenv import load_dotenv
load_dotenv()

from akarins_gateway.server import main

main()
