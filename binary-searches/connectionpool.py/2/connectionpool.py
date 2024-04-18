
        :param method:
            HTTP request method (such as GET, POST, PUT, etc.)

        :param body:
            Data to send in the request body (useful for creating
            POST requests, see HTTPConnectionPool.post_url for
            more convenience).

        :param headers:
            Dictionary of custom headers to send, such as User-Agent,
            If-None-Match, etc. If None, pool headers are used. If provided,
            these headers completely replace any pool-specific headers.

        :param retries:
            Configure the number of retries to allow before raising a
            :class:`~urllib3.exceptions.MaxRetryError` exception.

            Pass ``None`` to retry until you receive a response. Pass a
            :class:`~urllib3.util.retry.Retry` object for fine-grained control
            over different types of retries.
            Pass an integer number to retry connection errors that many times,
            but no other types of errors. Pass zero to never retry.

            If ``False``, then retries are disabled and any exception is raised
            immediately. Also, instead of raising a MaxRetryError on redirects,
            the redirect response will be returned.

        :type retries: :class:`~urllib3.util.retry.Retry`, False, or an int.

        :param redirect:
            If True, automatically handle redirects (status codes 301, 302,
            303, 307, 308). Each redirect counts as a retry. Disabling retries
            will disable redirect, too.

        :param assert_same_host:
            If ``True``, will make sure that the host of the pool requests is
            consistent else will raise HostChangedError. When False, you can
            use the pool on an HTTP proxy and request foreign hosts.

        :param timeout:
            If specified, overrides the default timeout for this one
            request. It may be a float (in seconds) or an instance of
            :class:`urllib3.util.Timeout`.

        :param pool_timeout:
            If set and the pool is set to block=True, then this method will
            block for ``pool_timeout`` seconds and raise EmptyPoolError if no
            connection is available within the time period.

        :param release_conn:
            If False, then the urlopen call will not release the connection
            back into the pool once a response is received (but will release if
            you read the entire contents of the response such as when
            `preload_content=True`). This is useful if you're not preloading
            the response's content immediately. You will need to call
            ``r.release_conn()`` on the response ``r`` to return the connection
            back into the pool. If None, it takes the value of
            ``response_kw.get('preload_content', True)``.

        :param chunked:
            If True, urllib3 will send the body using chunked transfer
            encoding. Otherwise, urllib3 will send the body using the standard
            content-length form. Defaults to False.

        :param int body_pos:
            Position to seek to in file-like body in the event of a retry or
            redirect. Typically this won't need to be set because urllib3 will
            auto-populate the value when needed.

        :param \\**response_kw:
            Additional parameters are passed to
            :meth:`urllib3.response.HTTPResponse.from_httplib`
        """
        if headers is None:
            headers = self.headers

        if not isinstance(retries, Retry):
            retries = Retry.from_int(retries, redirect=redirect, default=self.retries)

        if release_conn is None:
            release_conn = response_kw.get("preload_content", True)

        # Check host
        if assert_same_host and not self.is_same_host(url):
            raise HostChangedError(self, url, retries)

        # Ensure that the URL we're connecting to is properly encoded
        if url.startswith("/"):
            url = six.ensure_str(_encode_target(url))
        else:
            url = six.ensure_str(parse_url(url).url)

        conn = None

        # Track whether `conn` needs to be released before
        # returning/raising/recursing. Update this variable if necessary, and
        # leave `release_conn` constant throughout the function. That way, if
        # the function recurses, the original value of `release_conn` will be
        # passed down into the recursive call, and its value will be respected.
        #
        # See issue #651 [1] for details.
        #
        # [1] <https://github.com/shazow/urllib3/issues/651>
        release_this_conn = release_conn

        # Merge the proxy headers. Only do this in HTTP. We have to copy the
        # headers dict so we can safely change it without those changes being
        # reflected in anyone else's copy.
        if self.scheme == "http":
            headers = headers.copy()
            headers.update(self.proxy_headers)

        # Must keep the exception bound to a separate variable or else Python 3
        # complains about UnboundLocalError.
        err = None

        # Keep track of whether we cleanly exited the except block. This
        # ensures we do proper cleanup in finally.
        clean_exit = False

        # Rewind body position, if needed. Record current position
        # for future rewinds in the event of a redirect/retry.
        body_pos = set_file_position(body, body_pos)

        try:
            # Request a connection from the queue.
            timeout_obj = self._get_timeout(timeout)
            conn = self._get_conn(timeout=pool_timeout)

            conn.timeout = timeout_obj.connect_timeout

            is_new_proxy_conn = self.proxy is not None and not getattr(
                conn, "sock", None
            )
            if is_new_proxy_conn:
                self._prepare_proxy(conn)

            # Make the request on the httplib connection object.
            httplib_response = self._make_request(
                conn,
                method,
                url,
                timeout=timeout_obj,
                body=body,
                headers=headers,
                chunked=chunked,
            )

            # If we're going to release the connection in ``finally:``, then
            # the response doesn't need to know about the connection. Otherwise
            # it will also try to release it and we'll have a double-release
            # mess.
            response_conn = conn if not release_conn else None

            # Pass method to Response for length checking
            response_kw["request_method"] = method

            # Import httplib's response into our own wrapper object
            response = self.ResponseCls.from_httplib(
                httplib_response,
                pool=self,
                connection=response_conn,
                retries=retries,
                **response_kw
            )

            # Everything went great!
            clean_exit = True

        except queue.Empty:
            # Timed out by queue.
            raise EmptyPoolError(self, "No pool connections are available.")

        except (
            TimeoutError,
            HTTPException,
            SocketError,
            ProtocolError,
            BaseSSLError,
            SSLError,
            CertificateError,
        ) as e:
            # Discard the connection for these exceptions. It will be
            # replaced during the next _get_conn() call.
            clean_exit = False
            if isinstance(e, (BaseSSLError, CertificateError)):
                e = SSLError(e)
            elif isinstance(e, (SocketError, NewConnectionError)) and self.proxy:
                e = ProxyError("Cannot connect to proxy.", e)
            elif isinstance(e, (SocketError, HTTPException)):
                e = ProtocolError("Connection aborted.", e)

            retries = retries.increment(
                method, url, error=e, _pool=self, _stacktrace=sys.exc_info()[2]
            )
            retries.sleep()

            # Keep track of the error for the retry warning.
            err = e

        finally:
            if not clean_exit:
                # We hit some kind of exception, handled or otherwise. We need
                # to throw the connection away unless explicitly told not to.
                # Close the connection, set the variable to None, and make sure
                # we put the None back in the pool to avoid leaking it.
                conn = conn and conn.close()
                release_this_conn = True

            if release_this_conn:
                # Put the connection back to be reused. If the connection is
                # expired then it will be None, which will get replaced with a
                # fresh connection during _get_conn.
                self._put_conn(conn)

        if not conn:
            # Try again
            log.warning(
                "Retrying (%r) after connection " "broken by '%r': %s",
                retries,
                err,
                url,
            )
            return self.urlopen(
                method,
                url,
                body,
                headers,
                retries,
                redirect,
                assert_same_host,
                timeout=timeout,
                pool_timeout=pool_timeout,
                release_conn=release_conn,
                body_pos=body_pos,
                **response_kw
            )

        def drain_and_release_conn(response):
            try:
                # discard any remaining response body, the connection will be
                # released back to the pool once the entire response is read
                response.read()
            except (
                TimeoutError,
                HTTPException,
                SocketError,
                ProtocolError,
                BaseSSLError,
                SSLError,
            ):
                pass

        # Handle redirect?
        redirect_location = redirect and response.get_redirect_location()
        if redirect_location:
            if response.status == 303:
                method = "GET"

            try:
                retries = retries.increment(method, url, response=response, _pool=self)
            except MaxRetryError:
                if retries.raise_on_redirect:
                    # Drain and release the connection for this response, since
                    # we're not returning it to be released manually.
                    drain_and_release_conn(response)
                    raise
                return response

            # drain and return the connection to the pool before recursing
            drain_and_release_conn(response)

            retries.sleep_for_retry(response)
            log.debug("Redirecting %s -> %s", url, redirect_location)
            return self.urlopen(
                method,
                redirect_location,
                body,
                headers,
                retries=retries,
                redirect=redirect,
                assert_same_host=assert_same_host,
                timeout=timeout,
                pool_timeout=pool_timeout,
                release_conn=release_conn,
                body_pos=body_pos,
                **response_kw
            )

        # Check if we should retry the HTTP response.
        has_retry_after = bool(response.getheader("Retry-After"))
        if retries.is_retry(method, response.status, has_retry_after):
            try:
                retries = retries.increment(method, url, response=response, _pool=self)
            except MaxRetryError:
                if retries.raise_on_status:
                    # Drain and release the connection for this response, since
                    # we're not returning it to be released manually.
                    drain_and_release_conn(response)
                    raise
                return response

            # drain and return the connection to the pool before recursing
            drain_and_release_conn(response)

            retries.sleep(response)
            log.debug("Retry: %s", url)
            return self.urlopen(
                method,
                url,
                body,
                headers,
                retries=retries,
                redirect=redirect,
                assert_same_host=assert_same_host,
                timeout=timeout,
                pool_timeout=pool_timeout,
                release_conn=release_conn,
                body_pos=body_pos,
                **response_kw
            )

        return response


class HTTPSConnectionPool(HTTPConnectionPool):
    """
    Same as :class:`.HTTPConnectionPool`, but HTTPS.

    When Python is compiled with the :mod:`ssl` module, then
    :class:`.VerifiedHTTPSConnection` is used, which *can* verify certificates,
    instead of :class:`.HTTPSConnection`.

    :class:`.VerifiedHTTPSConnection` uses one of ``assert_fingerprint``,
    ``assert_hostname`` and ``host`` in this order to verify connections.
    If ``assert_hostname`` is False, no verification is done.

    The ``key_file``, ``cert_file``, ``cert_reqs``, ``ca_certs``,
    ``ca_cert_dir``, ``ssl_version``, ``key_password`` are only used if :mod:`ssl`
    is available and are fed into :meth:`urllib3.util.ssl_wrap_socket` to upgrade
    the connection socket into an SSL socket.
    """

    scheme = "https"
    ConnectionCls = HTTPSConnection

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
        key_file=None,
        cert_file=None,
        cert_reqs=None,
        key_password=None,
        ca_certs=None,
        ssl_version=None,
        assert_hostname=None,
        assert_fingerprint=None,
        ca_cert_dir=None,
        **conn_kw
    ):

        HTTPConnectionPool.__init__(
            self,
            host,
            port,
            strict,
            timeout,
            maxsize,
            block,
            headers,
            retries,
            _proxy,
            _proxy_headers,
            **conn_kw
        )

        self.key_file = key_file
        self.cert_file = cert_file
        self.cert_reqs = cert_reqs
        self.key_password = key_password
        self.ca_certs = ca_certs
        self.ca_cert_dir = ca_cert_dir
        self.ssl_version = ssl_version
        self.assert_hostname = assert_hostname
        self.assert_fingerprint = assert_fingerprint

    def _prepare_conn(self, conn):
        """
        Prepare the ``connection`` for :meth:`urllib3.util.ssl_wrap_socket`
        and establish the tunnel if proxy is used.
        """

        if isinstance(conn, VerifiedHTTPSConnection):
            conn.set_cert(
                key_file=self.key_file,
                key_password=self.key_password,
                cert_file=self.cert_file,
                cert_reqs=self.cert_reqs,
                ca_certs=self.ca_certs,
                ca_cert_dir=self.ca_cert_dir,
                assert_hostname=self.assert_hostname,
                assert_fingerprint=self.assert_fingerprint,
            )
            conn.ssl_version = self.ssl_version
        return conn

    def _prepare_proxy(self, conn):
        """
        Establish tunnel connection early, because otherwise httplib
        would improperly set Host: header to proxy's IP:port.
        """
        conn.set_tunnel(self._proxy_host, self.port, self.proxy_headers)
        conn.connect()

    def _new_conn(self):
        """
        Return a fresh :class:`httplib.HTTPSConnection`.
        """
        self.num_connections += 1
        log.debug(
            "Starting new HTTPS connection (%d): %s:%s",
            self.num_connections,
            self.host,
            self.port or "443",
        )

        if not self.ConnectionCls or self.ConnectionCls is DummyConnection:
            raise SSLError(
                "Can't connect to HTTPS URL because the SSL " "module is not available."
            )

        actual_host = self.host
        actual_port = self.port
        if self.proxy is not None:
            actual_host = self.proxy.host
            actual_port = self.proxy.port

        conn = self.ConnectionCls(
            host=actual_host,
            port=actual_port,
            timeout=self.timeout.connect_timeout,
            strict=self.strict,
            cert_file=self.cert_file,
            key_file=self.key_file,
            key_password=self.key_password,
            **self.conn_kw
        )

        return self._prepare_conn(conn)

    def _validate_conn(self, conn):
        """
        Called right before a request is made, after the socket is created.
        """
        super(HTTPSConnectionPool, self)._validate_conn(conn)

        # Force connect early to allow us to validate the connection.
        if not getattr(conn, "sock", None):  # AppEngine might not have  `.sock`
            conn.connect()

        if not conn.is_verified:
            warnings.warn(
                (
                    "Unverified HTTPS request is being made. "
                    "Adding certificate verification is strongly advised. See: "
                    "https://urllib3.readthedocs.io/en/latest/advanced-usage.html"
                    "#ssl-warnings"
                ),
                InsecureRequestWarning,
            )


def connection_from_url(url, **kw):
    """
    Given a url, return an :class:`.ConnectionPool` instance of its host.

    This is a shortcut for not having to parse out the scheme, host, and port
    of the url before creating an :class:`.ConnectionPool` instance.

    :param url:
        Absolute URL string that must include the scheme. Port is optional.

    :param \\**kw:
        Passes additional parameters to the constructor of the appropriate
        :class:`.ConnectionPool`. Useful for specifying things like
        timeout, maxsize, headers, etc.

    Example::

        >>> conn = connection_from_url('http://google.com/')
        >>> r = conn.request('GET', '/')
    """
    scheme, host, port = get_host(url)
    port = port or port_by_scheme.get(scheme, 80)
    if scheme == "https":
        return HTTPSConnectionPool(host, port=port, **kw)
    else:
        return HTTPConnectionPool(host, port=port, **kw)


def _normalize_host(host, scheme):
    """
    Normalize hosts for comparisons and use with sockets.
    """

    host = normalize_host(host, scheme)

    # httplib doesn't like it when we include brackets in IPv6 addresses
    # Specifically, if we include brackets but also pass the port then
    # httplib crazily doubles up the square brackets on the Host header.
    # Instead, we need to make sure we never pass ``None`` as the port.
    # However, for backward compatibility reasons we can't actually
    # *assert* that.  See http://bugs.python.org/issue28539
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host
