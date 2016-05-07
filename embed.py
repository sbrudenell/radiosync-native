import logging
import sys
from spotify import http_remote
from spotify import radiosync

logging.basicConfig(
    stream=sys.stdout, level=logging.DEBUG,
    format="%(asctime)s %(levelname)5s %(threadName)s %(name)s "
    "%(filename)s:%(lineno)d:%(message)s")
remote = http_remote.SpotifyRemote()
