    :param block:
        If set to True, no more than ``maxsize`` connections will be used at
        a time. When no free connections are available, the call will block
        until a connection has been released. This is a useful side effect for
        particular multithreaded situations where one does not want to use more
        than maxsize connections per host to prevent flooding.

    :param headers:
        Headers to include with all requests, unless other headers are given
        explicitly.

    :param retries:
        Retry configuration to use by default with requests in this pool.

    :param _proxy:
        Parsed proxy URL, should not be used directly, instead, see
        :class:`urllib3.connectionpool.ProxyManager`"

    :param _proxy_headers:
        A dictionary with proxy headers, should not be used directly,
        instead, see :class:`urllib3.connectionpool.ProxyManager`"

    :param \\**conn_kw:
        Additional parameters are used to create fresh :class:`urllib3.connection.HTTPConnection`,
        :class:`urllib3.connection.HTTPSConnection` instances.
    """

    scheme = "http"
    ConnectionCls = HTTPConnection
    ResponseCls = HTTPResponse

    def __init__(
        self,
        host,
        port=None,
        strict=False,
        timeout=Timeout.DEFAULT_TIMEOUT,
        maxsize=1,
        block=False,
        headers=None,
        retries=None,
        _proxy=None,
        _proxy_headers=None,
        **conn_kw
    ):
        ConnectionPool.__init__(self, host, port)
        RequestMethods.__init__(self, headers)

        self.strict = strict

        if not isinstance(timeout, Timeout):
            timeout = Timeout.from_float(timeout)

        if retries is None:
            retries = Retry.DEFAULT

        self.timeout = timeout
        self.retries = retries

        self.pool = self.QueueCls(maxsize)
        self.block = block

        self.proxy = _proxy
        self.proxy_headers = _proxy_headers or {}

        # Fill the queue up so that doing get() on it will block properly
        for _ in xrange(maxsize):
            self.pool.put(None)

        # These are mostly for testing and debugging purposes.
        self.num_connections = 0
        self.num_requests = 0
        self.conn_kw = conn_kw

        if self.proxy:
            # Enable Nagle's algorithm for proxies, to avoid packet fragmentation.
            # We cannot know if the user has added default socket options, so we cannot replace the
            # list.
            self.conn_kw.setdefault("socket_options", [])

    def _new_conn(self):
        """
        Return a fresh :class:`HTTPConnection`.
        """
        self.num_connections += 1
        log.debug(
            "Starting new HTTP connection (%d): %s:%s",
            self.num_connections,
            self.host,
            self.port or "80",
        )

        conn = self.ConnectionCls(
            host=self.host,
            port=self.port,
            timeout=self.timeout.connect_timeout,
            strict=self.strict,
            **self.conn_kw
        )
        return conn

    def _get_conn(self, timeout=None):
        """
        Get a connection. Will return a pooled connection if one is available.

        If no connections are available and :prop:`.block` is ``False``, then a
        fresh connection is returned.

        :param timeout:
            Seconds to wait before giving up and raising
            :class:`urllib3.exceptions.EmptyPoolError` if the pool is empty and
            :prop:`.block` is ``True``.
        """
        conn = None
        try:
            conn = self.pool.get(block=self.block, timeout=timeout)

        except AttributeError:  # self.pool is None
            raise ClosedPoolError(self, "Pool is closed.")

        except queue.Empty:
            if self.block:
                raise EmptyPoolError(
                    self,
                    "Pool reached maximum size and no more " "connections are allowed.",
                )
            pass  # Oh well, we'll create a new connection then

        # If this is a persistent connection, check if it got disconnected
        if conn and is_connection_dropped(conn):
            log.debug("Resetting dropped connection: %s", self.host)
            conn.close()
            if getattr(conn, "auto_open", 1) == 0:
                # This is a proxied connection that has been mutated by
                # httplib._tunnel() and cannot be reused (since it would
                # attempt to bypass the proxy)
                conn = None

        return conn or self._new_conn()