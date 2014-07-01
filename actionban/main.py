import yaml
import socket
import argparse
import setproctitle
import gevent
import signal
import time
import sys
import subprocess
import shelve
import os

from subprocess import check_output
from gevent import spawn, sleep
from gevent.monkey import patch_all
from gevent.event import Event
from gevent.server import DatagramServer
patch_all()
import logging
from pprint import pformat
from collections import deque
from copy import copy

from .utils import time_format, recursive_update
from .monitor import MonitorWSGI, root
import actionban


def _config():
    parser = argparse.ArgumentParser(description='Run ActionBan')
    parser.add_argument('config', type=argparse.FileType('r'),
                        help='yaml configuration file to run with')
    args = parser.parse_args()

    # override those defaults with a loaded yaml config
    raw_config = yaml.load(args.config) or {}
    raw_config.setdefault('actionban', {})
    return raw_config


def main():
    raw_config = _config()

    # check that config has a valid address
    server = ActionBan(raw_config, **raw_config['actionban'])
    server.run()


def send():
    parser = argparse.ArgumentParser(description='Send a message to ActionBan')
    parser.add_argument('-p', '--port', default=9000)
    parser.add_argument('-s', '--host', default='127.0.0.1')
    parser.add_argument('message', help='the raw UDP message to send')
    args = parser.parse_args()

    UDP_IP = args.host
    UDP_PORT = args.port

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 32)
    sock.sendto(args.message, (UDP_IP, UDP_PORT))


class ActionBan(object):
    def __init__(self, raw_config, procname="actionban", term_timeout=3,
                 loggers=None, config_db_file=None, members_db_file=None):
        if not config_db_file:
            config_db_file = os.path.join(root, "config_database")
        if not members_db_file:
            members_db_file = os.path.join(root, "members_database")
        if not loggers:
            loggers = [{'type': 'StreamHandler', 'level': 'DEBUG'}]
        self.log_handlers = []

        # setup all our log handlers
        for log_cfg in loggers:
            if log_cfg['type'] == "StreamHandler":
                kwargs = dict(stream=sys.stdout)
            else:
                kwargs = dict()
            handler = getattr(logging, log_cfg['type'])(**kwargs)
            log_level = getattr(logging, log_cfg['level'].upper())
            handler.setLevel(log_level)
            fmt = log_cfg.get('format', '%(asctime)s [%(name)s] [%(levelname)s] %(message)s')
            formatter = logging.Formatter(fmt)
            handler.setFormatter(formatter)
            self.log_handlers.append((log_cfg.get('listen'), handler))
        self.logger = self.register_logger('manager')

        self.logger.info("=" * 80)
        self.logger.info("Actionban daemon starting up...".format(procname))
        self.logger.debug(pformat(raw_config))

        setproctitle.setproctitle(procname)
        self.term_timeout = term_timeout
        self.raw_config = raw_config
        self.version = actionban.__version__
        self.version_info = actionban.__version_info__
        # Allow putting them in the init file for a built version
        self.sha = getattr(actionban, '__sha__', "unknown")
        self.rev_date = getattr(actionban, '__rev_date__', "unknown")
        if self.sha == "unknown":
            # try and fetch the git version information
            try:
                output = subprocess.check_output("git show -s --format='%ci %h'",
                                                 shell=True).strip().rsplit(" ", 1)
                self.sha = output[1]
                self.rev_date = output[0]
            # celery won't work with this, so set some default
            except Exception as e:
                self.logger.info("Unable to fetch git hash info: {}".format(e))

        # bookkeeping for things to request exit from at exit time
        # A list of all the greenlets that are running
        self.greenlets = []
        # A list of all the StreamServers
        self.servers = []

        # Primary data structures
        self.jails = {}
        self.jails_members = shelve.open(members_db_file, writeback=True)
        self.logger.debug("Loaded {} from shelve".format(dict(self.jails_members)))
        self.jails_config = shelve.open(config_db_file, writeback=True)
        self.stats = {'actions': Windower()}
        self.rotation_stats = deque([], 20)

    def register_logger(self, name):
        logger = logging.getLogger(name)
        for keys, handler in self.log_handlers:
            # If the keys are blank then we assume it wants all loggers
            # registered
            if not keys or name in keys:
                logger.addHandler(handler)
                # handlers will manage level, so just propogate everything
                logger.setLevel(logging.DEBUG)

        return logger

    def __getitem__(self, key):
        """ Allow convenient access to stat counters"""
        return self.stats[key]

    def run(self):
        """ Start all components and register them so we can request their
        graceful termination at exit time. """
        # Start the main chain network monitor and aux chain monitors
        serv = ActionServer(self, **self.raw_config['action_server'])
        self.servers.append(serv)
        serv.start()

        # a simple greenlet that rotates all jail counters
        self.stat_rotater = spawn(self.tick_stats)
        self.greenlets.append(self.stat_rotater)

        # the monitor server. a simple flask http server that lets you view
        # internal data structures to monitor server health
        self.monitor_server = MonitorWSGI(self, **self.raw_config.get('monitor', {}))
        if self.monitor_server:
            self.monitor_server.start()
            self.servers.append(self.monitor_server)

        # Register shutdown signals
        gevent.signal(signal.SIGINT, self.exit, "SIGINT")
        gevent.signal(signal.SIGHUP, self.exit, "SIGHUP")

        self._exit_signal = Event()
        # Wait for the exit signal to be called
        self._exit_signal.wait()

        # stop all stream servers
        for server in self.servers:
            # timeout is actually the time we wait before killing the greenlet,
            # so don't bother waiting, no cleanup is needed from our servers
            spawn(server.stop)

        # stop all greenlets
        for gl in self.greenlets:
            gl.kill(timeout=self.term_timeout, block=False)

        try:
            if gevent.wait(timeout=self.term_timeout):
                self.logger.info("All threads exited normally")
            else:
                self.logger.info("Timeout reached, shutting down forcefully")

        # Allow a force exit from multiple exit signals
        except KeyboardInterrupt:
            self.logger.info("Shutdown requested again by system, "
                             "exiting without cleanup")

        self.logger.info("=" * 80)

    def commit_sync_bans(self, new_bans):
        # Unjail expired members
        t = int(time.time())

        for jail_name, dct in self.jails_members.iteritems():
            expire_duration = self.jails_config[jail_name][2]
            expire_time = t - expire_duration
            d = []
            for ip, t in dct.iteritems():
                if t < expire_time:
                    d.append(ip)

            for ip in d:
                self.logger.info("Removing {} from jail {}".format(ip, jail_name))
                check_output(["sudo", "ipset", "del", jail_name, ip])
                del dct[ip]

        for jail_name, ip in new_bans:
            self.logger.info("Jailing ip {} on jail {}".format(ip, jail_name))
            self.jails_members[jail_name][ip] = t
            check_output(["sudo", "ipset", "create", jail_name, "iphash", "-exist"])
            check_output(["sudo", "ipset", "add", jail_name, ip, "-exist"])
        self.jails_members.sync()

    def exit(self, signal=None):
        """ Handle an exit request """
        self.logger.info("*" * 80)
        self.logger.info("Exiting requested via {}, allowing {} seconds for cleanup."
                         .format(signal, self.term_timeout))
        self._exit_signal.set()

    @property
    def status(self):
        """ For display in the http monitor """
        return dict()

    def tick_stats(self):
        """ A greenlet that handles rotation of statistics """
        try:
            self.logger.info("Jail rotater starting up")
            last_tick = int(time.time())
            while True:
                t = time.time()
                tot_jails = 0
                new_bans = []
                for jail_key, jail in self.jails.iteritems():
                    tot_jails += len(jail)
                    sec_thresh, min_thresh, _ = self.jails_config[jail_key]
                    d = []
                    for ip, can in jail.iteritems():
                        if (can.sum >= min_thresh or can.slices[-1] >= sec_thresh) and ip not in self.jails_members[jail_key]:
                            new_bans.append((jail_key, ip))
                        can.tick()
                        if can.sum == 0:
                            d.append(ip)

                    for key in d:
                        del jail[key]

                for stat in self.stats.itervalues():
                    stat.tick()

                last_tick += 1

                self.logger.info("{} Jails rotated in {}"
                                 .format(tot_jails, time_format(time.time() - t)))
                self.commit_sync_bans(new_bans)
                self.rotation_stats.append((tot_jails, time.time() - t))
                sleep(last_tick - time.time() + 1.0)
        except gevent.GreenletExit:
            self.logger.info("Jail manager exiting...")


class ActionServer(DatagramServer):
    def _set_config(self, **config):
        self.config = dict(port=9000,
                           host='127.0.0.1')
        recursive_update(self.config, config)

    def __init__(self, server, **config):
        self._set_config(**config)
        self.server = server
        self.jails = server.jails
        self.jails_members = server.jails_members
        self.jails_config = server.jails_config
        self.stats = server.stats
        self.logger = server.register_logger("action_server")
        DatagramServer.__init__(self, (self.config['host'], self.config['port']))

    def handle(self, data, address):
        parts = data.split(" ")
        command, args = parts[0], parts[1:]
        if command == "action":
            # args: [jail_name, ip_address, action_count, min_thresh, sec_thresh]
            if args[0] not in self.jails:
                self.jails[args[0]] = {}
                self.jails_members[args[0]] = {}
                self.jails_config[args[0]] = [int(i) for i in args[3:]]
                self.jails_config.sync()
            jail = self.jails[args[0]]
            if args[1] not in jail:
                jail[args[1]] = Windower()
            jail[args[1]].incr(int(args[2]))
            self.stats['actions'].incr()
            return
        if command == "jail":
            self.jails[args[0]] = {}
            self.jails_members[args[0]] = {}
            self.jails_config[args[0]] = [int(i) for i in args[1:]]
            self.jails_config.sync()
            self.logger.info("Updated/created jail {} config with args {}"
                             .format(args[0], args[1:]))


class Windower(object):
    empty = [0 for i in xrange(60)]
    __slots__ = ['_val', 'slices', 'sum']

    def __init__(self):
        self._val = 0
        self.slices = deque(copy(self.empty), 60)
        self.sum = 0

    def incr(self, amount=1):
        self.slices[-1] += amount
        self.sum += amount

    def tick(self):
        self.sum -= self.slices[0]
        self.slices.append(0)
