    def _put_conn(self, conn):
        """
        Put a connection back into the pool.

        :param conn:
            Connection object for the current host and port as returned by
            :meth:`._new_conn` or :meth:`._get_conn`.

        If the pool is already full, the connection is closed and discarded
        because we exceeded maxsize. If connections are discarded frequently,
        then maxsize should be increased.

        If the pool is closed, then the connection will be closed and discarded.
        """
        try:
            self.pool.put(conn, block=False)
            return  # Everything is dandy, done.
        except AttributeError:
            # self.pool is None.
            pass
        except queue.Full:
            # This should never happen if self.block == True
            log.warning("Connection pool is full, discarding connection: %s", self.host)

        # Connection never got put back into the pool, close it.
        if conn:
            conn.close()

    def _validate_conn(self, conn):
        """
        Called right before a request is made, after the socket is created.
        """
        pass

    def _prepare_proxy(self, conn):
        # Nothing to do for HTTP connections.
        pass

    def _get_timeout(self, timeout):
        """ Helper that always returns a :class:`urllib3.util.Timeout` """
        if timeout is _Default:
            return self.timeout.clone()

        if isinstance(timeout, Timeout):
            return timeout.clone()
        else:
            # User passed us an int/float. This is for backwards compatibility,
            # can be removed later
            return Timeout.from_float(timeout)

    def _raise_timeout(self, err, url, timeout_value):
        """Is the error actually a timeout? Will raise a ReadTimeout or pass"""

        if isinstance(err, SocketTimeout):
            raise ReadTimeoutError(
                self, url, "Read timed out. (read timeout=%s)" % timeout_value
            )

        # See the above comment about EAGAIN in Python 3. In Python 2 we have
        # to specifically catch it and throw the timeout error
        if hasattr(err, "errno") and err.errno in _blocking_errnos:
            raise ReadTimeoutError(
                self, url, "Read timed out. (read timeout=%s)" % timeout_value
            )

        # Catch possible read timeouts thrown as SSL errors. If not the
        # case, rethrow the original. We need to do this because of:
        # http://bugs.python.org/issue10272
        if "timed out" in str(err) or "did not complete (read)" in str(
            err
        ):  # Python < 2.7.4
            raise ReadTimeoutError(
                self, url, "Read timed out. (read timeout=%s)" % timeout_value
            )

    def _make_request(
        self, conn, method, url, timeout=_Default, chunked=False, **httplib_request_kw
    ):
        """
        Perform a request on a given urllib connection object taken from our
        pool.

        :param conn:
            a connection from one of our connection pools

        :param timeout:
            Socket timeout in seconds for the request. This can be a
            float or integer, which will set the same timeout value for
            the socket connect and the socket read, or an instance of
            :class:`urllib3.util.Timeout`, which gives you more fine-grained
            control over your timeouts.
        """
        self.num_requests += 1

        timeout_obj = self._get_timeout(timeout)
        timeout_obj.start_connect()
        conn.timeout = timeout_obj.connect_timeout

        # Trigger any extra validation we need to do.
        try:
            self._validate_conn(conn)
        except (SocketTimeout, BaseSSLError) as e:
            # Py2 raises this as a BaseSSLError, Py3 raises it as socket timeout.
            self._raise_timeout(err=e, url=url, timeout_value=conn.timeout)
            raise

        # conn.request() calls httplib.*.request, not the method in
        # urllib3.request. It also calls makefile (recv) on the socket.
        if chunked:
            conn.request_chunked(method, url, **httplib_request_kw)
        else:
            conn.request(method, url, **httplib_request_kw)

        # Reset the timeout for the recv() on the socket
        read_timeout = timeout_obj.read_timeout

        # App Engine doesn't have a sock attr
        if getattr(conn, "sock", None):
            # In Python 3 socket.py will catch EAGAIN and return None when you
            # try and read into the file pointer created by http.client, which
            # instead raises a BadStatusLine exception. Instead of catching
            # the exception and assuming all BadStatusLine exceptions are read
            # timeouts, check for a zero timeout before making the request.
            if read_timeout == 0:
                raise ReadTimeoutError(
                    self, url, "Read timed out. (read timeout=%s)" % read_timeout
                )
            if read_timeout is Timeout.DEFAULT_TIMEOUT:
                conn.sock.settimeout(socket.getdefaulttimeout())
            else:  # None or a value
                conn.sock.settimeout(read_timeout)

        # Receive the response from the server
        try:
            try:
                # Python 2.7, use buffering of HTTP responses
                httplib_response = conn.getresponse(buffering=True)
            except TypeError:
                # Python 3
                try:
                    httplib_response = conn.getresponse()
                except BaseException as e:
                    # Remove the TypeError from the exception chain in
                    # Python 3 (including for exceptions like SystemExit).
                    # Otherwise it looks like a bug in the code.
                    six.raise_from(e, None)
        except (SocketTimeout, BaseSSLError, SocketError) as e:
            self._raise_timeout(err=e, url=url, timeout_value=read_timeout)
            raise

        # AppEngine doesn't have a version attr.
        http_version = getattr(conn, "_http_vsn_str", "HTTP/?")
        log.debug(
            '%s://%s:%s "%s %s %s" %s %s',
            self.scheme,
            self.host,
            self.port,
            method,
            url,
            http_version,
            httplib_response.status,
            httplib_response.length,
        )

        try:
            assert_header_parsing(httplib_response.msg)
        except (HeaderParsingError, TypeError) as hpe:  # Platform-specific: Python 3
            log.warning(
                "Failed to parse headers (url=%s): %s",
                self._absolute_url(url),
                hpe,
                exc_info=True,
            )

        return httplib_response

    def _absolute_url(self, path):
        return Url(scheme=self.scheme, host=self.host, port=self.port, path=path).url

    def close(self):
        """
        Close all pooled connections and disable the pool.
        """
        if self.pool is None:
            return
        # Disable access to the pool
        old_pool, self.pool = self.pool, None

        try:
            while True:
                conn = old_pool.get(block=False)
                if conn:
                    conn.close()

        except queue.Empty:
            pass  # Done.

    def is_same_host(self, url):
        """
        Check if the given ``url`` is a member of the same host as this
        connection pool.
        """
        if url.startswith("/"):
            return True

        # TODO: Add optional support for socket.gethostbyname checking.
        scheme, host, port = get_host(url)
        if host is not None:
            host = _normalize_host(host, scheme=scheme)

        # Use explicit default port for comparison when none is given
        if self.port and not port:
            port = port_by_scheme.get(scheme)
        elif not self.port and port == port_by_scheme.get(scheme):
            port = None

        return (scheme, host, port) == (self.scheme, self.host, self.port)

    def urlopen(
        self,
        method,
        url,
        body=None,
        headers=None,
        retries=None,
        redirect=True,
        assert_same_host=True,
        timeout=_Default,
        pool_timeout=None,
        release_conn=None,
        chunked=False,
        body_pos=None,
        **response_kw
    ):
        """
        Get a connection from the pool and perform an HTTP request. This is the
        lowest level call for making a request, so you'll need to specify all
        the raw details.

        .. note::

           More commonly, it's appropriate to use a convenience method provided
           by :class:`.RequestMethods`, such as :meth:`request`.

        .. note::

           `release_conn` will only behave as expected if
           `preload_content=False` because we want to make
           `preload_content=False` the default behaviour someday soon without
           breaking backwards compatibility.