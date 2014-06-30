from flask import Flask, jsonify, Blueprint, current_app
from werkzeug.local import LocalProxy
from gevent.wsgi import WSGIServer, WSGIHandler

from .utils import time_format


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
        app.config.update(kwargs)
        app.config['DEBUG'] = debug
        app.register_blueprint(main)

        # Monkey patch the wsgi logger
        Logger.logger = wsgi_logger
        app.real_logger = logger

        # setup localproxy refs
        app.server = server
        WSGIServer.__init__(self, (address, port), app, log=Logger())

    def stop(self, *args, **kwargs):
        self.application.real_logger.info("Stopping monitoring server")
        WSGIServer.stop(self, *args, **kwargs)


@main.route('/')
def general():
    return jsonify(count=list(server.stats['actions'].slices),
                   jails_memebers=server.jails_memebers,
                   jails_config=server.jails_config)
