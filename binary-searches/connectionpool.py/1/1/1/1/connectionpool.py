from __future__ import absolute_import
import errno
import logging
import sys
import warnings

from socket import error as SocketError, timeout as SocketTimeout
import socket


from .exceptions import (
    ClosedPoolError,
    ProtocolError,
    EmptyPoolError,
    HeaderParsingError,
    HostChangedError,
    LocationValueError,
    MaxRetryError,
    ProxyError,
    ReadTimeoutError,
    SSLError,
    TimeoutError,
    InsecureRequestWarning,
    NewConnectionError,
)
from .packages.ssl_match_hostname import CertificateError
from .packages import six
from .packages.six.moves import queue
from .connection import (
    port_by_scheme,
    DummyConnection,
    HTTPConnection,
    HTTPSConnection,
    VerifiedHTTPSConnection,
    HTTPException,
    BaseSSLError,
)
from .request import RequestMethods
from .response import HTTPResponse

from .util.connection import is_connection_dropped
from .util.request import set_file_position
from .util.response import assert_header_parsing
from .util.retry import Retry
from .util.timeout import Timeout
from .util.url import (
    get_host,
    parse_url,
    Url,
    _normalize_host as normalize_host,
    _encode_target,
)
from .util.queue import LifoQueue


xrange = six.moves.xrange

log = logging.getLogger(__name__)

_Default = object()


