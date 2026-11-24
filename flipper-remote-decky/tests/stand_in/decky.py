"""Stand-in for Decky's `decky` module, so the plugin's backend can be tested outside Decky."""
import logging
import os

logging.basicConfig(level=logging.INFO, format="%(levelname)s decky: %(message)s")
logger = logging.getLogger("decky-stand-in")
EVENTS = []   # (event, args) as the panel would get them
DECKY_PLUGIN_DIR = os.environ.get("DECKY_PLUGIN_DIR", "")


async def emit(event, *args):
    EVENTS.append((event, args))
