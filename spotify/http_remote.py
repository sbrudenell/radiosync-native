import httplib
import json
import random
import string
import urllib
import urllib2
import urlparse


class SpotifyRemote(object):

    SCHEME = "https"

    EVENTS = ("login", "logout", "play", "pause", "error", "ap")

    DEFAULT_PORT = 4370
    DEFAULT_HEADERS = {
        "Origin": "https://open.spotify.com",
        "Referer": "https://open.spotify.com"}

    TOO_FAST_OR_SOMETHING = 4204

    def __init__(self, port=None, ref=None, headers=None):
        self.port = port or self.DEFAULT_PORT
        self.ref = ref
        self.headers = headers or self.DEFAULT_HEADERS
        self.scheme = self.SCHEME

        self._oauth_token = None
        self._csrf_token = None

    @property
    def host(self):
        name = "".join(
            random.choice(string.ascii_lowercase) for _ in range(10))
        return "%s.spotilocal.com" % name

    def qdict_no_tokens(self, **qdict):
        if self.ref:
            qdict["ref"] = self.ref
        return qdict

    def qstr_no_tokens(self, **qdict):
        qdict = self.qdict_no_tokens(**qdict)
        return urllib.urlencode(sorted(qdict.iteritems()))

    def qdict(self, **qdict):
        qdict["oauth"] = self.oauth_token
        qdict["csrf"] = self.csrf_token
        return self.qdict_no_tokens(**qdict)

    def qstr(self, **qdict):
        qdict = self.qdict(**qdict)
        return self.qstr_no_tokens(**qdict)

    def ucall(self, url):
        request = urllib2.Request(url, headers=self.headers)
        return json.loads(urllib2.urlopen(request).read())

    def call_no_tokens(self, path, **qdict):
        qstr = self.qstr_no_tokens(**qdict)
        url = urlparse.urlunparse((
            self.scheme, "%s:%s" % (self.host, self.port), path, None, qstr,
            None))
        return self.ucall(url)

    def call(self, path, **qdict):
        return self.call_no_tokens(path, **self.qdict(**qdict))

    @property
    def oauth_token(self):
        if self._oauth_token is None:
            token_json = self.ucall("https://open.spotify.com/token")
            self._oauth_token = token_json["t"]
        return self._oauth_token

    @property
    def csrf_token(self):
        if self._csrf_token is None:
            token_json = self.call_no_tokens("/simplecsrf/token.json")
            self._csrf_token = token_json["token"]
        return self._csrf_token

    def service_version(self):
        return self.call_no_tokens(
            "/service/version.json", service="remote")

    def status_qdict(self, **qdict):
        if "returnon" in qdict:
            qdict["returnon"] = ",".join(qdict["returnon"])
        return qdict

    def remote_status(self, **qdict):
        return self.call("/remote/status.json", **self.status_qdict(**qdict))

    def remote_status_shortlong(self, returnon=None, returnafter=None,
                                **qdict):
        long_qdict = self.status_qdict(
            returnon=returnon, returnafter=returnafter, **qdict)
        long_qstr = self.qstr(**long_qdict)
        conn = httplib.HTTPSConnection(self.host, self.port)
        conn.request(
            "GET", "/remote/status.json?%s" % long_qstr, headers=self.headers)
        # Wait for socket writable?
        yield self.remote_status(**qdict)
        yield json.loads(conn.getresponse().read())

    def remote_pause(self, pause):
        pause = "true" if pause else "false"
        return self.call("/remote/pause.json", pause=pause)

    def remote_play(self, uri, context=None):
        if not context:
            context = uri
        return self.call("/remote/play.json", uri=uri, context=context)

    def remote_open(self):
        return self.call("/remote/open.json")
