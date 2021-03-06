from nose.tools import set_trace
from flask.ext.babel import lazy_gettext as _
import requests
import logging
from authenticator import (
    BasicAuthenticationProvider,
    PatronData,
)
from config import (
    Configuration,
    CannotLoadConfiguration,
)
from circulation_exceptions import RemoteInitiatedServerError
import urlparse
import urllib
from core.model import (
    get_one_or_create,
    Patron,
)

class FirstBookAuthenticationAPI(BasicAuthenticationProvider):

    NAME = 'First Book'

    LOGIN_LABEL = _("Access Code")

    # If FirstBook sends this message it means they accepted the
    # patron's credentials.
    SUCCESS_MESSAGE = 'Valid Code Pin Pair'

    SECRET_KEY = 'key'

    # Server-side validation happens before the identifier
    # is converted to uppercase, which means lowercase characters
    # are valid.
    DEFAULT_IDENTIFIER_REGULAR_EXPRESSION = '^[A-Za-z0-9@]+$'
    DEFAULT_PASSWORD_REGULAR_EXPRESSION = '^[0-9]+$'
    
    log = logging.getLogger("First Book authentication API")

    def __init__(self, url=None, key=None, **kwargs):
        if not (url and key):
            raise CannotLoadConfiguration(
                "First Book server not configured."
            )
        super(FirstBookAuthenticationAPI, self).__init__(**kwargs)
        if '?' in url:
            url += '&'
        else:
            url += '?'
        self.root = url + 'key=' + key

    # Begin implementation of BasicAuthenticationProvider abstract
    # methods.

    def remote_authenticate(self, username, password):
        # All FirstBook credentials are in upper-case.
        username = username.upper()

        # If they fail a PIN test, there is no authenticated patron.
        if not self.remote_pin_test(username, password):
            return None

        # FirstBook keeps track of absolutely no information
        # about the patron other than the permanent ID,
        # which is also the authorization identifier.
        return PatronData(
            permanent_id=username,
            authorization_identifier=username,
        )
   
    # End implementation of BasicAuthenticationProvider abstract methods.

    def remote_pin_test(self, barcode, pin):
        url = self.root + "&accesscode=%s&pin=%s" % tuple(map(
            urllib.quote, (barcode, pin)
        ))
        try:
            response = self.request(url)
        except requests.exceptions.ConnectionError, e:
            raise RemoteInitiatedServerError(
                str(e.message),
                self.NAME
            )
        if response.status_code != 200:
            msg = "Got unexpected response code %d. Content: %s" % (
                response.status_code, response.content
            )
            raise RemoteInitiatedServerError(msg, self.NAME)
        if self.SUCCESS_MESSAGE in response.content:
            return True
        return False
    
    def request(self, url):
        """Make an HTTP request.

        Defined solely so it can be overridden in the mock.
        """
        return requests.get(url)


class MockFirstBookResponse(object):

    def __init__(self, status_code, content):
        self.status_code = status_code
        self.content = content

class MockFirstBookAuthenticationAPI(FirstBookAuthenticationAPI):

    SUCCESS = '"Valid Code Pin Pair"'
    FAILURE = '{"code":404,"message":"Access Code Pin Pair not found"}'

    def __init__(self, valid={}, bad_connection=False, 
                 failure_status_code=None):
        self.identifier_re = None
        self.password_re = None
        self.root = "http://example.com/"
        self.valid = valid
        self.bad_connection = bad_connection
        self.failure_status_code = failure_status_code

    def request(self, url):
        if self.bad_connection:
            # Simulate a bad connection.
            raise requests.exceptions.ConnectionError("Could not connect!")
        elif self.failure_status_code:
            # Simulate a server returning an unexpected error code.
            return MockFirstBookResponse(
                self.failure_status_code, "Error %s" % self.failure_status_code
            )
        qa = urlparse.parse_qs(url)
        if 'accesscode' in qa and 'pin' in qa:
            [code] = qa['accesscode']
            [pin] = qa['pin']
            if code in self.valid and self.valid[code] == pin:
                return MockFirstBookResponse(200, self.SUCCESS)
            else:
                return MockFirstBookResponse(200, self.FAILURE)


# Specify which of the classes defined in this module is the
# authentication provider.
AuthenticationProvider = FirstBookAuthenticationAPI
