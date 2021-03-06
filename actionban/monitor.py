import os

from datetime import timedelta
from werkzeug.local import LocalProxy
from gevent.wsgi import WSGIServer, WSGIHandler
from jinja2 import FileSystemLoader
from flask import (current_app, request, render_template, Blueprint, abort,
                   jsonify, g, session, Response, Flask)

from .utils import time_format


root = os.path.abspath(os.path.dirname(__file__) + '/../')
main = Blueprint('main', __name__)
logger = LocalProxy(
    lambda: getattr(current_app, 'real_logger', None))
server = LocalProxy(
    lambda: getattr(current_app, 'server', None))


class Logger(object):
    """ A dummp file object to allow using a logger to log requests instead
    of sending to stderr like the default WSGI logger """
    logger = None

    def write(self, s):
        self.logger.info(s.strip())


class CustomWSGIHandler(WSGIHandler):
    """ A simple custom handler allows us to provide more helpful request
    logging format. Format designed for easy profiling """
    def format_request(self):
        length = self.response_length or '-'
        delta = time_format(self.time_finish - self.time_start)
        client_address = self.client_address[0] if isinstance(self.client_address, tuple) else self.client_address
        return '%s "%s" %s %s %s' % (
            client_address or '-',
            getattr(self, 'requestline', ''),
            (getattr(self, 'status', None) or '000').split()[0],
            length,
            delta)


class MonitorWSGI(WSGIServer):
    # Use our custom wsgi handler
    handler_class = CustomWSGIHandler

    def __init__(self, server, debug=False, address='127.0.0.1', port=3855, enabled=True, **kwargs):
        """ Handles implementing default configurations """
        logger = server.register_logger('monitor')
        wsgi_logger = server.register_logger('monitor_wsgi')
        if not enabled:
            logger.info("HTTP monitor not enabled, not starting up...")
            return
        else:
            logger.info("HTTP monitor enabled, starting up...")
        app = Flask('monitor')
        app = Flask('monitor', static_folder='../static', static_url_path='/static')
        # set our template path and configs
        app.jinja_loader = FileSystemLoader(os.path.join(root, 'templates'))
        app.config.update(kwargs)
        app.config['DEBUG'] = debug
        app.register_blueprint(main)

        # Monkey patch the wsgi logger
        Logger.logger = wsgi_logger
        app.real_logger = logger

        @app.template_filter('duration')
        def time_format(seconds):
            # microseconds
            if seconds > 3600:
                return "{}".format(timedelta(seconds=seconds))
            if seconds > 60:
                return "{:,.2f} mins".format(seconds / 60.0)
            if seconds <= 1.0e-3:
                return "{:,.4f} us".format(seconds * 1000000.0)
            if seconds <= 1.0:
                return "{:,.4f} ms".format(seconds * 1000.0)
            return "{:,.4f} sec".format(seconds)

        # setup localproxy refs
        app.server = server
        WSGIServer.__init__(self, (address, port), app, log=Logger())

        @app.template_filter('datetime')
        def jinja_format_datetime(value, fmt='medium'):
            if fmt == 'full':
                fmt = "EEEE, MMMM d y 'at' HH:mm"
            elif fmt == 'medium':
                fmt = "EE MM/dd/y HH:mm"
            return value.strftime(fmt)

    def stop(self, *args, **kwargs):
        self.application.real_logger.info("Stopping monitoring server")
        WSGIServer.stop(self, *args, **kwargs)


@main.route('/')
def general():
    return render_template('home.html',
                           jails_config=server.jails_config,
                           jails_members=server.jails_members,
                           jails=server.jails)


@main.route('/timing')
def timing():
    return render_template('timing.html',
                           timing=server.rotation_stats)


@main.route('/action_ips/<jail>')
def jail(jail=None):
    if jail not in server.jails:
        abort(404)
    return render_template('action_ips.html', ips=server.jails[jail])


@main.route('/banned_ips/<jail>')
def banned_ips(jail=None):
    if jail not in server.jails:
        abort(404)
    return render_template('banned_ips.html', ips=server.jails_members[jail])
