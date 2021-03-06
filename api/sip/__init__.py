from datetime import datetime
from nose.tools import set_trace
from api.authenticator import (
    BasicAuthenticationProvider,
    PatronData,
)
from api.sip.client import SIPClient
from core.util.http import RemoteIntegrationException
from core.util import MoneyUtility

class SIP2AuthenticationProvider(BasicAuthenticationProvider):

    NAME = "SIP2"

    DATE_FORMATS = ["%Y%m%d", "%Y%m%d%Z%H%M%S", "%Y%m%d    %H%M%S"]

    def __init__(self, server, port, login_user_id,
                 login_password, location_code, field_separator='|',
                 client=None,
                 **kwargs):
        """An object capable of communicating with a SIP server.

        :param server: Hostname of the SIP server.
        :param port: The port number to connect to on the SIP server.

        :param login_user_id: SIP field CN; the user ID to use when
         initiating a SIP session, if necessary. This is _not_ a
         patron identifier (SIP field AA); it identifies the SC
         creating the SIP session. SIP2 defines SC as "...any library
         automation device dealing with patrons or library materials."

        :param login_password: Sip field CO; the password to use when
         initiating a SIP session, if necessary.

        :param location_code: SIP field CP; the location code to use
         when initiating a SIP session. A location code supposedly
         refers to the physical location of a self-checkout machine
         within a library system. Some libraries require a special
         location code to be provided when authenticating patrons;
         others may require the circulation manager to be treated as
         its own special 'location'.

        :param field_separator: The field delimiter (see
        "Variable-length fields" in the SIP2 spec). If no value is
        specified, the default (the pipe character) will be used.

        :param client: A drop-in replacement for the SIPClient
        object. Only intended for use during testing.

        """
        super(SIP2AuthenticationProvider, self).__init__(**kwargs)
        try:
            if client:
                if callable(client):
                    client = client()
            else:
                client = SIPClient(
                    target_server=server, target_port=port,
                    login_user_id=login_user_id, login_password=login_password,
                    location_code=location_code, separator=field_separator
                )
        except IOError, e:
            raise RemoteIntegrationException(
                server or 'unknown server', e.message
            )
        self.client = client

    def remote_authenticate(self, username, password):
        """Authenticate a patron with the SIP2 server.

        :param username: The patron's username/barcode/card
            number/authorization identifier.
        :param password: The patron's password/pin/access code.
        """
        try:
            info = self.client.patron_information(username, password)
        except IOError, e:
            raise RemoteIntegrationException(
                self.client.target_server or 'unknown server',
                e.message
            )
        return self.info_to_patrondata(info)

    @classmethod
    def info_to_patrondata(cls, info):

        """Convert the SIP-specific dictionary obtained from
        SIPClient.patron_information() to an abstract,
        authenticator-independent PatronData object.
        """
        if info.get('valid_patron_password') == 'N':
            # The patron did not authenticate correctly. Don't
            # return any data.
            return None

            # TODO: I'm not 100% convinced that a missing CQ field
            # always means "we don't have passwords so you're
            # authenticated," rather than "you didn't provide a
            # password so we didn't check."
        patrondata = PatronData()
        if 'sipserver_internal_id' in info:
            patrondata.permanent_id = info['sipserver_internal_id']
        if 'patron_identifier' in info:
            patrondata.authorization_identifier = info['patron_identifier']
        if 'email_address' in info:
            patrondata.email_address = info['email_address']
        if 'personal_name' in info:
            patrondata.personal_name = info['personal_name']
        if 'fee_amount' in info:
            fines = info['fee_amount']
        else:
            fines = '0'
        patrondata.fines = MoneyUtility.parse(fines)
        if 'sipserver_patron_class' in info:
            patrondata.external_type = info['sipserver_patron_class']
        for expire_field in ['sipserver_patron_expiration', 'polaris_patron_expiration']:
            if expire_field in info:
                value = info.get(expire_field)
                value = cls.parse_date(value)
                if value:
                    patrondata.authorization_expires = value
                    break
        return patrondata

    @classmethod
    def parse_date(cls, value):
        """Try to parse `value` using any of several common date formats."""
        date_value = None
        for format in cls.DATE_FORMATS:
            try:
                date_value = datetime.strptime(value, format)
                break
            except ValueError, e:
                continue
        return date_value
        
    # NOTE: It's not necessary to implement remote_patron_lookup
    # because authentication gets patron data as a side effect.

AuthenticationProvider = SIP2AuthenticationProvider
