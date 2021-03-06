from datetime import datetime
from nose.tools import (
    assert_raises_regexp,
    set_trace,
    eq_,
)
from api.sip.client import MockSIPClient
from api.sip import SIP2AuthenticationProvider
from core.util.http import RemoteIntegrationException

class TestSIP2AuthenticationProvider(object):

    # We feed sample data into the MockSIPClient, even though it adds
    # an extra step of indirection, because it lets us use as a
    # starting point the actual (albeit redacted) SIP2 messages we
    # receive from servers.
    
    sierra_valid_login = "64              000201610210000142637000000000000000000000000AOnypl |AA12345|AESHELDON, ALICE|BZ0030|CA0050|CB0050|BLY|CQY|BV0|CC15.00|BEfoo@example.com|AY1AZD1B7"
    sierra_invalid_login = "64Y  YYYYYYYYYYY000201610210000142725000000000000000000000000AOnypl |AA12345|AESHELDON, ALICE|BZ0030|CA0050|CB0050|BLY|CQN|BV0|CC15.00|BEfoo@example.com|AFInvalid PIN entered.  Please try again or see a staff member for assistance.|AFThere are unresolved issues with your account.  Please see a staff member for assistance.|AY1AZ91A8"

    evergreen_active_user = "64  Y           00020161021    142851000000000000000000000000AA12345|AEBooth Active Test|BHUSD|BDAdult Circ Desk 1 Newtown, CT USA 06470|AQNEWTWN|BLY|PA20191004|PCAdult|PIAllowed|XI863715|AOBiblioTest|AY2AZ0000"
    evergreen_expired_card = "64YYYY          00020161021    142937000000000000000000000000AA12345|AEBooth Expired Test|BHUSD|BDAdult Circ Desk #2 Newtown, CT USA 06470|AQNEWTWN|BLY|PA20080907|PCAdult|PIAllowed|XI863716|AFblocked|AOBiblioTest|AY2AZ0000"
    evergreen_excessive_fines = "64  Y           00020161021    143002000000000000000100000000AA12345|AEBooth Excessive Fines Test|BHUSD|BV100.00|BDChildrens Circ Desk 1 Newtown, CT USA 06470|AQNEWTWN|BLY|PA20191004|PCAdult|PIAllowed|XI863718|AOBiblioTest|AY2AZ0000"
    evergreen_inactive_account = "64YYYY          00020161021    143028000000000000000000000000AE|AA12345|BLN|AOBiblioTest|AY2AZ0000"

    polaris_valid_pin = "64              00120161121    143327000000000000000000000000AO3|AA25891000331441|AEFalk, Jen|BZ0050|CA0075|CB0075|BLY|CQY|BHUSD|BV9.25|CC9.99|BD123 Charlotte Hall, MD 20622|BEfoo@bar.com|BF501-555-1212|BC19710101    000000|PA1|PEHALL|PSSt. Mary's|U1|U2|U3|U4|U5|PZ20622|PX20180609    235959|PYN|FA0.00|AFPatron status is ok.|AGPatron status is ok.|AY2AZ94F3"
        
    polaris_wrong_pin = "64YYYY          00120161121    143157000000000000000000000000AO3|AA25891000331441|AEFalk, Jen|BZ0050|CA0075|CB0075|BLY|CQN|BHUSD|BV9.25|CC9.99|BD123 Charlotte Hall, MD 20622|BEfoo@bar.com|BF501-555-1212|BC19710101    000000|PA1|PEHALL|PSSt. Mary's|U1|U2|U3|U4|U5|PZ20622|PX20180609    235959|PYN|FA0.00|AFInvalid patron password. Passwords do not match.|AGInvalid patron password.|AY2AZ87B4"

    polaris_expired_card = "64YYYY          00120161121    143430000000000000000000000000AO3|AA25891000224613|AETester, Tess|BZ0050|CA0075|CB0075|BLY|CQY|BHUSD|BV0.00|CC9.99|BD|BEfoo@bar.com|BF|BC19710101    000000|PA1|PELEON|PSSt. Mary's|U1|U2|U3|U4|U5|PZ|PX20161025    235959|PYY|FA0.00|AFPatron has blocks.|AGPatron has blocks.|AY2AZA4F8"

    polaris_excess_fines = "64YYYY      Y   00120161121    144438000000000000000000000000AO3|AA25891000115879|AEFalk, Jen|BZ0050|CA0075|CB0075|BLY|CQY|BHUSD|BV11.50|CC9.99|BD123, Charlotte Hall, MD 20622|BE|BF501-555-1212|BC20140610    000000|PA1|PEHALL|PS|U1No|U2|U3|U4|U5|PZ20622|PX20170424    235959|PYN|FA0.00|AFPatron has blocks.|AGPatron has blocks.|AY2AZA27B"

    polaris_no_such_patron = "64YYYY          00120161121    143126000000000000000000000000AO3|AA1112|AE, |BZ0000|CA0000|CB0000|BLN|CQN|BHUSD|BV0.00|CC0.00|BD|BE|BF|BC|PA0|PE|PS|U1|U2|U3|U4|U5|PZ|PX|PYN|FA0.00|AFPatron does not exist.|AGPatron does not exist.|AY2AZBCF2"
    
    def test_remote_authenticate(self):
        client = MockSIPClient()
        auth = SIP2AuthenticationProvider(
            None, None, None, None, None, None, client=client
        )

        # Some examples taken from a Sierra SIP API.
        client.queue_response(self.sierra_valid_login)
        patrondata = auth.remote_authenticate("user", "pass")
        eq_("12345", patrondata.authorization_identifier)
        eq_("foo@example.com", patrondata.email_address)
        eq_("SHELDON, ALICE", patrondata.personal_name)
        eq_(0, patrondata.fines)
        eq_(None, patrondata.authorization_expires)
        eq_(None, patrondata.external_type)
        
        client.queue_response(self.sierra_invalid_login)
        eq_(None, auth.remote_authenticate("user", "pass"))
        
        # Some examples taken from an Evergreen instance that doesn't
        # use passwords.
        client.queue_response(self.evergreen_active_user)
        patrondata = auth.remote_authenticate("user", "pass")
        eq_("12345", patrondata.authorization_identifier)
        eq_("863715", patrondata.permanent_id)
        eq_("Booth Active Test", patrondata.personal_name)
        eq_(0, patrondata.fines)
        eq_(datetime(2019, 10, 4), patrondata.authorization_expires)
        eq_("Adult", patrondata.external_type)
        
        client.queue_response(self.evergreen_expired_card)
        patrondata = auth.remote_authenticate("user", "pass")
        eq_("12345", patrondata.authorization_identifier)
        # SIP extension field XI becomes sipserver_internal_id which
        # becomes PatronData.permanent_id.
        eq_("863716", patrondata.permanent_id)
        eq_("Booth Expired Test", patrondata.personal_name)
        eq_(0, patrondata.fines)
        eq_(datetime(2008, 9, 7), patrondata.authorization_expires)

        client.queue_response(self.evergreen_excessive_fines)
        patrondata = auth.remote_authenticate("user", "pass")
        eq_("12345", patrondata.authorization_identifier)
        eq_("863718", patrondata.permanent_id)
        eq_("Booth Excessive Fines Test", patrondata.personal_name)
        eq_(100, patrondata.fines)
        eq_(datetime(2019, 10, 04), patrondata.authorization_expires)

        # Some examples taken from a Polaris instance.
        client.queue_response(self.polaris_valid_pin)
        patrondata = auth.remote_authenticate("user", "pass")
        eq_("25891000331441", patrondata.authorization_identifier)
        eq_("foo@bar.com", patrondata.email_address)
        eq_(9.25, patrondata.fines)
        eq_("Falk, Jen", patrondata.personal_name)
        eq_(datetime(2018, 6, 9, 23, 59, 59),
            patrondata.authorization_expires)

        client.queue_response(self.polaris_wrong_pin)
        patrondata = auth.remote_authenticate("user", "pass")
        eq_(None, patrondata)

        client.queue_response(self.polaris_no_such_patron)
        patrondata = auth.remote_authenticate("user", "pass")
        eq_(None, patrondata)
        
        client.queue_response(self.polaris_expired_card)
        patrondata = auth.remote_authenticate("user", "pass")
        eq_(datetime(2016, 10, 25, 23, 59, 59),
            patrondata.authorization_expires)
        
        client.queue_response(self.polaris_excess_fines)
        patrondata = auth.remote_authenticate("user", "pass")
        eq_(11.50, patrondata.fines)

    def test_ioerror_during_connect_becomes_remoteintegrationexception(self):
        """If the IP of the circulation manager has not been whitelisted,
        we generally can't even connect to the server.
        """
        class CannotConnect(MockSIPClient):
            def connect(self):
                raise IOError("Doom!")


        assert_raises_regexp(
            RemoteIntegrationException,
            "Error accessing server.local: Doom!",
            SIP2AuthenticationProvider,
            "server.local", None, None, None, None, client=CannotConnect
        )

    def test_ioerror_during_send_becomes_remoteintegrationexception(self):
        """If there's an IOError communicating with the server,
        it becomes a RemoteIntegrationException.
        """
        class CannotSend(MockSIPClient):
            def do_send(self, data):
                raise IOError("Doom!")
        client = CannotSend()
        client.target_server = 'server.local'
            
        provider = SIP2AuthenticationProvider(
            None, None, None, None, None, client=client
        )
        assert_raises_regexp(
            RemoteIntegrationException,
            "Error accessing server.local: Doom!",
            provider.remote_authenticate,
            "username", "password",
        )
        
    def test_parse_date(self):
        parse = SIP2AuthenticationProvider.parse_date
        eq_(datetime(2011, 1, 2), parse("20110102"))
        eq_(datetime(2011, 1, 2, 10, 20, 30), parse("20110102    102030"))
        eq_(datetime(2011, 1, 2, 10, 20, 30), parse("20110102UTC102030"))
