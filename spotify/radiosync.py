from concurrent import futures
import json as json_lib
import logging
import requests
import threading
import time
import urllib
import urlparse

WINDOW = 5.0

SCHEME = "https"
HOST = "radio-sync.appspot.com"


def log():
    return logging.getLogger(__name__)



class AgedStatus(object):

    def __init__(self, status, age=None):
        self.status = status
        self.base_age = age
        self.created = time.time()

    @property
    def age(self):
        return time.time() - self.created + (self.base_age or 0)

    @property
    def pos(self):
        return self.get("playing_position")

    @property
    def track(self):
        return self.get("track")

    @property
    def track_uri(self):
        track = self.track
        return track["track_resource"]["uri"] if track else None

    @property
    def track_length(self):
        track = self.track
        return track.get("length") if track else None

    @property
    def overtime(self):
        if self.track_length is None or self.pos is None:
            return 0
        return self.pos - self.track_length

    @property
    def running(self):
        return self.get("running")

    @property
    def playing(self):
        return self.get("playing")

    @property
    def stale(self):
        return not self.running or self.overtime > 0

    def __getitem__(self, key):
        if key not in self.status:
            raise KeyError(key)
        return self.get(key)

    def get(self, key, default=None):
        if key not in self.status:
            return default
        if key == "playing_position":
            position = self.status["playing_position"]
            if self.get("playing"):
                return position + self.age
            else:
                return position
        if key == "server_time":
            return self.status["server_time"] + int(self.age)
        else:
            return self.status.get(key, default)


class Broadcast(object):

    SCHEME = SCHEME
    HOST = HOST
    PATH = "playerstate"

    def __init__(self, id, spotify):
        self.id = id
        self.spotify = spotify
        self.thread = None
        self.running = False

    def start(self):
        if self.running:
            return
        log().debug("Signaling start.")
        self.running = True
        self.thread = threading.Thread(
            name="Broadcast-%s" % self.id, target=self.run)
        self.thread.daemon = True
        self.thread.start()

    def stop(self):
        log().debug("Signaling stop.")
        self.running = False
        self.thread = None

    def run(self):
        log().debug("Starting.")
        while self.running:
            try:
                for status in self.spotify.remote_status_shortlong(
                        returnafter=1800, returnon=self.spotify.EVENTS):
                    if not self.running:
                        break
                    status = json_lib.dumps(status)
                    url = urlparse.urlunparse((
                        self.SCHEME, self.HOST, self.PATH, None, None, None))
                    requests.post(url, data=dict(id=self.id, status=status))
            except Exception:
                log().exception("While posting update")
        log().debug("Stopping.")


class LocalStatusGetter(object):

    def __init__(self, spotify, cv, **kwargs):
        self.spotify = spotify
        self.cv = cv
        self.kwargs = kwargs
        self.status = None
        self.thread = None
        self.running = False

    def start(self):
        self.running = True
        self.thread = threading.Thread(
            name="LocalStatusGetter", target=self.run)
        self.thread.daemon = True
        self.thread.start()

    def stop(self):
        self.running = False

    def run(self):
        log().debug("Starting.")
        while self.running:
            for status in self.spotify.remote_status_shortlong(**self.kwargs):
                status = AgedStatus(status)
                log().debug("Got local status.")
                with self.cv:
                    self.status = status
                    self.cv.notify()
        log().debug("Stopping.")


class TargetStatusGetter(object):

    PATH = "playerstate"

    def __init__(self, cv, target_id, window=None, rapid_poll_interval=None,
                 target_timeout=None):
        self.cv = cv
        self.target_id = target_id
        self.window = window or 0
        self.rapid_poll_interval = rapid_poll_interval or 0
        self.target_timeout = target_timeout
        self.status = None
        self.thread = None
        self.running = False

    def start(self):
        self.running = True
        self.thread = threading.Thread(
            name="TargetStatusGetter", target=self.run)
        self.thread.daemon = True
        self.thread.start()

    def stop(self):
        self.running = False

    def get_target_status(self):
        log().debug("Getting target status.")
        query = urllib.urlencode(dict(id=self.target_id))
        url = urlparse.urlunparse((
            SCHEME, HOST, self.PATH, None, query, None))
        json = requests.get(url).json()
        if json and json["status"]:
            return AgedStatus(json["status"], age=json["age"])
        log().debug("No status for target.")
        return AgedStatus({"running": False})

    def run(self):
        log().debug("Starting.")
        status = self.get_target_status()
        with self.cv:
            self.status = status
            self.cv.notify()
        while self.running:
            # Poor man's long-polling. Poll slowly, but speed up around track
            # changes, to catch changes with less delay. Hopefully goes away
            # when we change out the backend...
            with self.cv:
                if not self.status.playing:
                    log().debug("Target not playing.")
                    wait_time = self.window
                elif self.status.overtime > self.target_timeout:
                    log().debug("Target looks timed out.")
                    wait_time = self.window
                elif self.status.overtime > 0:
                    log().debug("Target is overtime.")
                    wait_time = self.rapid_poll_interval
                elif self.status.overtime > -self.window:
                    log().debug("Target coming up on overtime.")
                    wait_time = (
                        -self.status.overtime + self.rapid_poll_interval)
                else:
                    wait_time = self.window
            log().debug("Waiting %.3fs before polling.", wait_time)
            time.sleep(wait_time)
            if not self.running:
                break
            status = self.get_target_status()
            with self.cv:
                self.status = status
                self.cv.notify()
        log().debug("Stopping.")


class Follow(object):

    SCHEME = SCHEME
    HOST = HOST
    PATH = "playerstate"

    TARGET_TIMEOUT = 10.0
    WINDOW = 5.0
    RAPID_POLL_INTERVAL = 0.5

    def __init__(self, spotify, target_id):
        self.spotify = spotify
        self.target_id = target_id
        self.last_local_status = None
        self.local_status = None
        self.target_status = None
        self.thread = None
        self.running = False

    def start(self):
        log().debug("Signaling start.")
        self.running = True
        self.thread = threading.Thread(
            name="Follow-%s" % self.target_id, target=self.run)
        self.thread.daemon = True
        self.thread.start()

    def stop(self):
        log().debug("Signaling stop.")
        self.running = False
        self.thread = None

    def is_synced(self):
        """Decide if we should catch up to the target."""
        if bool(self.target_status) != bool(self.local_status):
            return False
        if not self.local_status:
            return True
        if self.target_status.running != self.local_status.running:
            log().debug(
                "Local %srunning, target %srunning",
                "" if self.local_status.running else "not ",
                "" if self.target_status.running else "not ")
            return False
        target_playing = (
            self.target_status.playing and self.target_status.overtime < 0)
        if target_playing != self.local_status.playing:
            log().debug(
                "Local %splaying, target %splaying",
                "" if self.local_status.playing else "not ",
                "" if target_playing else "not ")
            return False
        if not self.local_status.playing:
            return True

        if self.target_status.track_uri != self.local_status.track_uri:
            # We have a next-track for the target, and we just switched tracks
            # (probably automatically went to the next track). It's okay to
            # switch tracks.
            if self.local_status.pos < 0.1:
                log().debug("We just switched tracks, catching up.")
                return False
            # If we're just finishing up a track, let it finish, before
            # switching to the target's next track.
            delta = (
                self.local_status.track_length - self.local_status.pos +
                self.target_status.pos)
            log().debug("Target on different track, delta = %.3f", delta)
            if delta > self.WINDOW:
                return False
        else:
            # Seek to the right position if we're too far off.
            delta = self.target_status.pos - self.local_status.pos
            log().debug("Delta = %.3f", delta)
            if abs(delta) > self.WINDOW:
                return False
        return True

    def try_update_status(self, update_func):
        start = time.time()
        status = update_func()
        elapsed = time.time() - start
        log().debug("New status: %s", json_lib.dumps(status))
        status = AgedStatus(status, age=elapsed / 2)
        if not status.get("error"):
            self.last_local_status = None
            self.local_status = status
            return True
        else:
            return False

    def do_update(self, update_func):
        if not self.try_update_status(update_func):
            self.try_update_status(lambda: self.spotify.remote_status())


    def sync(self):
        playing = self.target_status.playing
        if self.target_status.overtime > 0:
            log().debug("Target is overtime.")
            playing = False
        if not playing:
            modified = self.local_status and self.local_status.playing
            if modified:
                log().debug("Pausing.")
                self.do_update(lambda: self.spotify.remote_pause(True))
                return True
            else:
                log().debug("Continuing to do nothing.")
                return False
        target_pos = self.target_status.pos
        # Smoother transitions.
        # TODO: Web Helper connections seem to always take several seconds, we
        # often have to seek after a track change. Maybe subtract some time
        # here.
        if target_pos < self.WINDOW:
            target_pos = 0
        target_uri = "%s#%d:%.3f" % (
            self.target_status.track_uri, int(target_pos / 60),
            target_pos % 60)
        log().debug("Syncing to %s", target_uri)
        self.do_update(lambda: self.spotify.remote_play(target_uri))
        return True

    def maybe_sync(self):
        if not self.target_status or not self.local_status:
            return False
        if self.is_synced():
            return False
        else:
            self.sync()
            return True
        """
        elif self.local_status_is_user_stop_action():
            log().debug("User wants to take control.")
            self.stop()
            return
        """

    def run(self):
        log().debug("Starting.")
        cv = threading.Condition()
        self.local_status = None
        self.target_status = None

        target_getter = None
        local_getter = None

        try:
            with cv:
                while self.running:
                    try:
                        if not local_getter:
                            local_getter = LocalStatusGetter(
                                self.spotify, cv, returnon=self.spotify.EVENTS,
                                returnafter=3600)
                            local_getter.start()
                        if not target_getter:
                            target_getter = TargetStatusGetter(
                                cv, self.target_id, window=self.WINDOW,
                                rapid_poll_interval=self.RAPID_POLL_INTERVAL,
                                target_timeout=self.TARGET_TIMEOUT)
                            target_getter.start()

                        if local_getter.status:
                            self.local_status = local_getter.status
                        if target_getter.status:
                            self.target_status = target_getter.status

                        if self.maybe_sync():
                            # If we made any status changes, any old long-polls
                            # might return outdated data. Ignore them and start
                            # new polls.
                            log().debug("Changed local status, resetting getter.")
                            local_getter.stop()
                            local_getter = None
                        else:
                            log().debug("Waiting for changes.")
                            cv.wait()
                    except:
                        log().exception("While following, resetting.")
                        if local_getter:
                            local_getter.stop()
                            local_getter = None
                        if target_getter:
                            target_getter.stop()
                            target_getter = None
        finally:
            if local_getter:
                local_getter.stop()
            if target_getter:
                target_getter.stop()
        log().debug("Stopping.")
