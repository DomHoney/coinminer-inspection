# Pool objects
class ConnectionPool(object):
    """
    Base class for all connection pools, such as
    :class:`.HTTPConnectionPool` and :class:`.HTTPSConnectionPool`.
    """

    scheme = None
    QueueCls = LifoQueue

    def __init__(self, host, port=None):
        if not host:
            raise LocationValueError("No host specified.")

        self.host = _normalize_host(host, scheme=self.scheme)
        self._proxy_host = host.lower()
        self.port = port

    def __str__(self):
        return "%s(host=%r, port=%r)" % (type(self).__name__, self.host, self.port)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        # Return False to re-raise any potential exceptions
        return False

    def close(self):
        """
        Close all pooled connections and disable the pool.
        """
        pass


# This is taken from http://hg.python.org/cpython/file/7aaba721ebc0/Lib/socket.py#l252
_blocking_errnos = {errno.EAGAIN, errno.EWOULDBLOCK}


class HTTPConnectionPool(ConnectionPool, RequestMethods):
    """
    Thread-safe connection pool for one host.

    :param host:
        Host used for this HTTP Connection (e.g. "localhost"), passed into
        :class:`httplib.HTTPConnection`.

    :param port:
        Port used for this HTTP Connection (None is equivalent to 80), passed
        into :class:`httplib.HTTPConnection`.

    :param strict:
        Causes BadStatusLine to be raised if the status line can't be parsed
        as a valid HTTP/1.0 or 1.1 status line, passed into
        :class:`httplib.HTTPConnection`.

        .. note::
           Only works in Python 2. This parameter is ignored in Python 3.

    :param timeout:
        Socket timeout in seconds for each individual connection. This can
        be a float or integer, which sets the timeout for the HTTP request,
        or an instance of :class:`urllib3.util.Timeout` which gives you more
        fine-grained control over request timeouts. After the constructor has
        been parsed, this is always a `urllib3.util.Timeout` object.

    :param maxsize:
        Number of connections to save that can be reused. More than 1 is useful
        in multithreaded situations. If ``block`` is set to False, more
        connections will be created but they will not be saved once they've
        been used.

    