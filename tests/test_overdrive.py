# encoding: utf-8
from nose.tools import (
    set_trace, eq_,
    assert_raises,
)
import pkgutil
import json
from datetime import (
    datetime,
    timedelta,
)
from api.overdrive import (
    MockOverdriveAPI,
)

from api.circulation import (
    CirculationAPI,
)
from api.circulation_exceptions import *

from . import (
    DatabaseTest,
    sample_data
)

from core.model import (
    DataSource,
    DeliveryMechanism,
    Identifier,
    LicensePool,
    Representation,
    RightsStatus,
)

from api.config import temp_config

class OverdriveAPITest(DatabaseTest):

    @classmethod
    def sample_data(self, filename):
        return sample_data(filename, 'overdrive')

    @classmethod
    def sample_json(self, filename):
        data = self.sample_data(filename)
        return data, json.loads(data)

    def error_message(self, error_code, message=None, token=None):
        """Create a JSON document that simulates the message served by
        Overdrive given a certain error condition.
        """
        message = message or self._str
        token = token or self._str
        data = dict(errorCode=error_code, message=message, token=token)
        return json.dumps(data)

class TestOverdriveAPI(OverdriveAPITest):

    def test_default_notification_email_address(self):
        """Test the ability of the Overdrive API to detect an email address
        previously given by the patron to Overdrive for the purpose of
        notifications.
        """
        ignore, patron_with_email = self.sample_json(
            "patron_info.json"
        )
        api = MockOverdriveAPI(self._db)
        api.queue_response(200, content=patron_with_email)
        patron = self._patron()
        # If the patron has used a particular email address to put
        # books on hold, use that email address, not the site default.
        with temp_config() as config:
            config['default_notification_email_address'] = "notifications@example.com"
            eq_("foo@bar.com", 
                api.default_notification_email_address(patron, 'pin'))

        # If the patron has never before put an Overdrive book on
        # hold, their JSON object has no `lastHoldEmail` key. In this
        # case we use the site default.
        patron_with_no_email = dict(patron_with_email)
        del patron_with_no_email['lastHoldEmail']
        api.queue_response(200, content=patron_with_no_email)
        with temp_config() as config:
            config['default_notification_email_address'] = "notifications@example.com"
            eq_("notifications@example.com", 
                api.default_notification_email_address(patron, 'pin'))

            # If there's an error getting the information, use the
            # site default.
            api.queue_response(404)
            eq_("notifications@example.com", 
                api.default_notification_email_address(patron, 'pin'))

    def test_place_hold_raises_exception_if_patron_over_hold_limit(self):
        over_hold_limit = self.error_message(
            "PatronExceededHoldLimit",
            "Patron cannot place any more holds, already has maximum holds placed."
        )

        edition, pool = self._edition(
            identifier_type=Identifier.OVERDRIVE_ID,
            data_source_name=DataSource.OVERDRIVE,
            with_license_pool=True
        )
        api = MockOverdriveAPI(self._db)
        api.queue_response(400, content=over_hold_limit)
        assert_raises(
            PatronHoldLimitReached,
            api.place_hold, self._patron(), 'pin', pool, 
            notification_email_address='foo@bar.com'
        )

    def test_place_hold_looks_up_notification_address(self):
        edition, pool = self._edition(
            identifier_type=Identifier.OVERDRIVE_ID,
            data_source_name=DataSource.OVERDRIVE,
            with_license_pool=True
        )

        # The first request we make will be to get patron info,
        # so that we know that the most recent email address used
        # to put a book on hold is foo@bar.com.
        ignore, patron_with_email = self.sample_json(
            "patron_info.json"
        )
        
        # The second request we make will be to put a book on hold,
        # and when we do so we will ask for the notification to be
        # sent to foo@bar.com.
        ignore, successful_hold = self.sample_json(
            "successful_hold.json"
        )

        api = MockOverdriveAPI(self._db)
        api.queue_response(200, content=patron_with_email)
        api.queue_response(200, content=successful_hold)
        with temp_config() as config:
            config['default_notification_email_address'] = "notifications@example.com"
            hold = api.place_hold(self._patron(), 'pin', pool, 
                                  notification_email_address=None)

        # The book was placed on hold.
        eq_(1, hold.hold_position)
        eq_(pool.identifier.identifier, hold.identifier)

        # And when we placed it on hold, we passed in foo@bar.com
        # as the email address -- not notifications@example.com.
        url, positional_args, kwargs = api.requests[-1]
        headers, body = positional_args
        assert '{"name": "emailAddress", "value": "foo@bar.com"}' in body

    def test_fulfill_raises_exception_and_updates_formats_for_outdated_format(self):
        edition, pool = self._edition(
            identifier_type=Identifier.OVERDRIVE_ID,
            data_source_name=DataSource.OVERDRIVE,
            with_license_pool=True
        )

        # This pool has a format that's no longer available from overdrive.
        pool.set_delivery_mechanism(Representation.PDF_MEDIA_TYPE, DeliveryMechanism.ADOBE_DRM,
                                    RightsStatus.IN_COPYRIGHT, None)

        ignore, loan = self.sample_json(
            "single_loan.json"
        )

        ignore, lock_in_format_not_available = self.sample_json(
            "lock_in_format_not_available.json"
        )

        # We will get the loan, try to lock in the format, and fail.
        api = MockOverdriveAPI(self._db)
        api.queue_response(200, content=loan)
        api.queue_response(400, content=lock_in_format_not_available)
        
        # Trying to get a fulfillment link raises an exception.
        assert_raises(
            FormatNotAvailable,
            api.get_fulfillment_link,
            self._patron(), 'pin', pool.identifier.identifier,
            'ebook-epub-adobe'
        )

        # Fulfill will also update the formats.
        ignore, bibliographic = self.sample_json(
            "bibliographic_information.json"
        )

        # If we have the LicensePool available (as opposed to just the
        # identifier), we will get the loan, try to lock in the
        # format, fail, and then update the bibliographic information.
        api.queue_response(200, content=loan)
        api.queue_response(400, content=lock_in_format_not_available)
        api.queue_response(200, content=bibliographic)
        
        assert_raises(
            FormatNotAvailable,
            api.fulfill,
            self._patron(), 'pin', pool,
            'ebook-epub-adobe'
        )

        # The delivery mechanisms have been updated.
        eq_(3, len(pool.delivery_mechanisms))
        eq_(set([Representation.EPUB_MEDIA_TYPE, DeliveryMechanism.KINDLE_CONTENT_TYPE, DeliveryMechanism.STREAMING_TEXT_CONTENT_TYPE]),
            set([lpdm.delivery_mechanism.content_type for lpdm in pool.delivery_mechanisms]))
        eq_(set([DeliveryMechanism.ADOBE_DRM, DeliveryMechanism.KINDLE_DRM, DeliveryMechanism.OVERDRIVE_DRM]),
            set([lpdm.delivery_mechanism.drm_scheme for lpdm in pool.delivery_mechanisms]))

    def test_get_fulfillment_link_from_download_link(self):
        patron = self._patron()

        ignore, streaming_fulfill_link = self.sample_json(
            "streaming_fulfill_link_response.json"
        )

        api = MockOverdriveAPI(self._db)
        api.queue_response(200, content=streaming_fulfill_link)

        href, type = api.get_fulfillment_link_from_download_link(patron, '1234', "http://download-link", fulfill_url="http://fulfill")
        eq_("https://fulfill.contentreserve.com/PerfectLife9780345530967.epub-sample.overdrive.com?RetailerID=nypl&Expires=1469825647&Token=dd0e19b4-eb70-439d-8c50-a65201060f4c&Signature=asl67/G154KeeUsL1mHPwEbZfgc=",
            href)
        eq_("text/html", type)

    def test_update_formats(self):
        # Create a LicensePool with an inaccurate delivery mechanism.
        edition, pool = self._edition(
            identifier_type=Identifier.OVERDRIVE_ID,
            with_license_pool=True
        )

        # Add the bad delivery mechanism.
        pool.set_delivery_mechanism(Representation.PDF_MEDIA_TYPE, DeliveryMechanism.ADOBE_DRM,
                                    RightsStatus.IN_COPYRIGHT, None)

        # Prepare the bibliographic information.
        ignore, bibliographic = self.sample_json(
            "bibliographic_information.json"
        )

        # To avoid a mismatch, make it look like the information is
        # for the new pool's Identifier.
        bibliographic['id'] = pool.identifier.identifier

        api = MockOverdriveAPI(self._db)
        api.queue_response(200, content=bibliographic)

        api.update_formats(pool)

        # The delivery mechanisms have been updated.
        eq_(3, len(pool.delivery_mechanisms))
        eq_(set([Representation.EPUB_MEDIA_TYPE, DeliveryMechanism.KINDLE_CONTENT_TYPE, DeliveryMechanism.STREAMING_TEXT_CONTENT_TYPE]),
            set([lpdm.delivery_mechanism.content_type for lpdm in pool.delivery_mechanisms]))
        eq_(set([DeliveryMechanism.ADOBE_DRM, DeliveryMechanism.KINDLE_DRM, DeliveryMechanism.OVERDRIVE_DRM]),
            set([lpdm.delivery_mechanism.drm_scheme for lpdm in pool.delivery_mechanisms]))

    def test_update_availability(self):
        """Test the Overdrive implementation of the update_availability
        method defined by the CirculationAPI interface.
        """

        # Create a LicensePool that needs updating.
        edition, pool = self._edition(
            identifier_type=Identifier.OVERDRIVE_ID,
            # TODO: If this line is commented out, we get an error later
            # on which might or might not be worrisome.
            data_source_name=DataSource.OVERDRIVE,
            with_license_pool=True
        )

        # We have never checked the circulation information for this
        # LicensePool. Put some random junk in the pool to make sure
        # it gets replaced.
        pool.licenses_owned = 10
        pool.licenses_available = 4
        pool.patrons_in_hold_queue = 3
        eq_(None, pool.last_checked)

        # Prepare availability information.
        ignore, availability = self.sample_json(
            "overdrive_availability_information.json"
        )
        # Since this is the first time we've seen this book,
        # we'll also be updating the bibliographic information.
        ignore, bibliographic = self.sample_json(
            "bibliographic_information.json"
        )

        # To avoid a mismatch, make it look like the information is
        # for the new pool's Identifier.
        availability['id'] = pool.identifier.identifier
        bibliographic['id'] = pool.identifier.identifier

        api = MockOverdriveAPI(self._db)
        api.queue_response(200, content=availability)
        api.queue_response(200, content=bibliographic)

        api.update_availability(pool)

        # The availability information has been udpated, as has the
        # date the availability information was last checked.
        eq_(5, pool.licenses_owned)
        eq_(5, pool.licenses_available)
        eq_(0, pool.patrons_in_hold_queue)
        assert pool.last_checked is not None

    def test_circulation_lookup(self):
        """Test the method that actually looks up Overdrive circulation
        information.
        """
        api = MockOverdriveAPI(self._db)
        api.queue_response(200, content="foo")

        book, (status_code, headers, content) = api.circulation_lookup(
            "an identifier"
        )
        eq_(dict(id="an identifier"), book)
        eq_(200, status_code)
        eq_("foo", content)
        
    def test_update_licensepool_error(self):
        # Create an identifier.
        identifier = self._identifier(
            identifier_type=Identifier.OVERDRIVE_ID
        )
        ignore, availability = self.sample_json(
            "overdrive_availability_information.json"
        )
        api = MockOverdriveAPI(self._db)
        api.queue_response(500, content="An error occured.")
        book = dict(id=identifier.identifier, availability_link=self._url)
        pool, was_new, changed = api.update_licensepool(book)
        eq_(None, pool)

    def test_update_licensepool_provides_bibliographic_coverage(self):
        # Create an identifier.
        identifier = self._identifier(
            identifier_type=Identifier.OVERDRIVE_ID
        )

        # Prepare bibliographic and availability information 
        # for this identifier.
        ignore, availability = self.sample_json(
            "overdrive_availability_information.json"
        )
        ignore, bibliographic = self.sample_json(
            "bibliographic_information.json"
        )

        # To avoid a mismatch, make it look like the information is
        # for the newly created Identifier.
        availability['id'] = identifier.identifier
        bibliographic['id'] = identifier.identifier

        api = MockOverdriveAPI(self._db)
        api.queue_response(200, content=availability)
        api.queue_response(200, content=bibliographic)

        # Now we're ready. When we call update_licensepool, the
        # OverdriveAPI will retrieve the availability information,
        # then the bibliographic information. It will then trigger the
        # OverdriveBibliographicCoverageProvider, which will
        # create an Edition and a presentation-ready Work.
        pool, was_new, changed = api.update_licensepool(identifier.identifier)
        eq_(True, was_new)        
        eq_(availability['copiesOwned'], pool.licenses_owned)

        edition = pool.presentation_edition
        eq_("Ancillary Justice", edition.title)

        eq_(True, pool.work.presentation_ready)
        assert pool.work.cover_thumbnail_url.startswith(
            'http://images.contentreserve.com/'
        )

        # The book has been run through the bibliographic coverage
        # provider.
        coverage = [
            x for x in identifier.coverage_records 
            if x.operation is None
            and x.data_source.name == DataSource.OVERDRIVE
        ]
        eq_(1, len(coverage))

    def test_update_new_licensepool(self):
        data, raw = self.sample_json("overdrive_availability_information.json")

        # Create an identifier
        identifier = self._identifier(
            identifier_type=Identifier.OVERDRIVE_ID
        )

        # Make it look like the availability information is for the
        # newly created Identifier.
        raw['id'] = identifier.identifier

        api = MockOverdriveAPI(self._db)
        pool, was_new = LicensePool.for_foreign_id(
            self._db, DataSource.OVERDRIVE, 
            identifier.type, identifier.identifier
        )
        
        pool, was_new, changed = api.update_licensepool_with_book_info(
            raw, pool, was_new
        )
        eq_(True, was_new)
        eq_(True, changed)

        self._db.commit()

        eq_(raw['copiesOwned'], pool.licenses_owned)
        eq_(raw['copiesAvailable'], pool.licenses_available)
        eq_(0, pool.licenses_reserved)
        eq_(raw['numberOfHolds'], pool.patrons_in_hold_queue)

    def test_update_existing_licensepool(self):
        data, raw = self.sample_json("overdrive_availability_information.json")

        # Create a LicensePool.
        wr, pool = self._edition(
            data_source_name=DataSource.OVERDRIVE,
            identifier_type=Identifier.OVERDRIVE_ID,
            with_license_pool=True
        )

        # Make it look like the availability information is for the
        # newly created LicensePool.
        raw['id'] = pool.identifier.identifier

        wr.title = "The real title."
        eq_(1, pool.licenses_owned)
        eq_(1, pool.licenses_available)
        eq_(0, pool.licenses_reserved)
        eq_(0, pool.patrons_in_hold_queue)

        api = MockOverdriveAPI(self._db)
        p2, was_new, changed = api.update_licensepool_with_book_info(
            raw, pool, False
        )
        eq_(False, was_new)
        eq_(True, changed)
        eq_(p2, pool)
        # The title didn't change to that title given in the availability
        # information, because we already set a title for that work.
        eq_("The real title.", wr.title)
        eq_(raw['copiesOwned'], pool.licenses_owned)
        eq_(raw['copiesAvailable'], pool.licenses_available)
        eq_(0, pool.licenses_reserved)
        eq_(raw['numberOfHolds'], pool.patrons_in_hold_queue)

    def test_update_licensepool_with_holds(self):
        data, raw = self.sample_json("overdrive_availability_information_holds.json")
        identifier = self._identifier(
            identifier_type=Identifier.OVERDRIVE_ID
        )
        raw['id'] = identifier.identifier

        api = MockOverdriveAPI(self._db)
        license_pool, is_new = LicensePool.for_foreign_id(
            self._db, DataSource.OVERDRIVE, identifier.type, 
            identifier.identifier
        )
        pool, was_new, changed = api.update_licensepool_with_book_info(
            raw, license_pool, is_new
        )
        eq_(10, pool.patrons_in_hold_queue)
        eq_(True, changed)

    def test_refresh_patron_access_token(self):
        """Verify that patron information is included in the request
        when refreshing a patron access token.
        """
        api = MockOverdriveAPI(self._db)
        patron = self._patron()
        patron.authorization_identifier = 'barcode'
        credential = self._credential(patron=patron)

        data, raw = self.sample_json("patron_token.json")
        api.queue_response(200, content=raw)
        
        # Try to refresh the patron access token with a PIN, and
        # then without a PIN.
        api.refresh_patron_access_token(credential, patron, "a pin")

        api.refresh_patron_access_token(credential, patron, None)

        # Verify that the requests that were made correspond to what
        # Overdrive is expecting.
        initial, with_pin, without_pin = api.access_token_requests
        url, payload, headers, kwargs = with_pin
        eq_("https://oauth-patron.overdrive.com/patrontoken", url)
        eq_("barcode", payload['username'])
        eq_("websiteid:d authorizationname:default", payload['scope'])
        eq_("a pin", payload['password'])
        assert not 'password_required' in payload

        url, payload, headers, kwargs = without_pin
        eq_("https://oauth-patron.overdrive.com/patrontoken", url)
        eq_("barcode", payload['username'])
        eq_("websiteid:d authorizationname:default", payload['scope'])
        eq_("false", payload['password_required'])
        eq_("[ignore]", payload['password'])
        
class TestExtractData(OverdriveAPITest):

    def test_get_download_link(self):
        data, json = self.sample_json("checkout_response_locked_in_format.json")
        url = MockOverdriveAPI.get_download_link(
            json, "ebook-epub-adobe", "http://foo.com/")
        eq_("http://patron.api.overdrive.com/v1/patrons/me/checkouts/76C1B7D0-17F4-4C05-8397-C66C17411584/formats/ebook-epub-adobe/downloadlink?errorpageurl=http://foo.com/", url)
        
        assert_raises(
            NoAcceptableFormat, 
            MockOverdriveAPI.get_download_link,
            json, "no-such-format", "http://foo.com/"
        )

    def test_get_download_link_raises_exception_if_loan_fulfilled_on_incompatible_platform(self):
        data, json = self.sample_json("checkout_response_book_fulfilled_on_kindle.json")
        assert_raises(
            FulfilledOnIncompatiblePlatform,
            MockOverdriveAPI.get_download_link,
            json, "ebook-epub-adobe", "http://foo.com/"            
        )

    def test_extract_data_from_checkout_resource(self):
        data, json = self.sample_json("checkout_response_locked_in_format.json")
        expires, url = MockOverdriveAPI.extract_data_from_checkout_response(
            json, "ebook-epub-adobe", "http://foo.com/")
        eq_(2013, expires.year)
        eq_(10, expires.month)
        eq_(4, expires.day)
        eq_("http://patron.api.overdrive.com/v1/patrons/me/checkouts/76C1B7D0-17F4-4C05-8397-C66C17411584/formats/ebook-epub-adobe/downloadlink?errorpageurl=http://foo.com/", url)

    def test_process_checkout_data(self):
        data, json = self.sample_json("shelf_with_book_already_fulfilled_on_kindle.json")
        [on_kindle, not_on_kindle] = json["checkouts"]

        # The book already fulfilled on Kindle doesn't get turned into
        # LoanInfo.
        eq_(None, MockOverdriveAPI.process_checkout_data(on_kindle))

        # The book not yet fulfilled does show up as a LoanInfo.
        loan_info = MockOverdriveAPI.process_checkout_data(not_on_kindle)
        eq_("2fadd2ac-a8ec-4938-a369-4c3260e8922b", loan_info.identifier)

        data, format_locked_in = self.sample_json("checkout_response_locked_in_format.json")

        # A book that's on loan with a format locked in shows up.
        loan_info = MockOverdriveAPI.process_checkout_data(format_locked_in)
        assert loan_info != None

        data, no_format_locked_in = self.sample_json("checkout_response_no_format_locked_in.json")

        # A book that's on loan with no format locked in also shows up.
        loan_info = MockOverdriveAPI.process_checkout_data(no_format_locked_in)
        assert loan_info != None

        # TODO: In the future both of these tests should return a
        # LoanInfo with appropriate FulfillmentInfo. The calling code
        # would then decide whether or not to show the loan.

class TestSyncBookshelf(OverdriveAPITest):

    def test_sync_bookshelf_creates_local_loans(self):
        loans_data, json_loans = self.sample_json("shelf_with_some_checked_out_books.json")
        holds_data, json_holds = self.sample_json("no_holds.json")

        overdrive = MockOverdriveAPI(self._db)
        overdrive.queue_response(200, content=loans_data)
        overdrive.queue_response(200, content=holds_data)

        patron = self._patron()
        circulation = CirculationAPI(self._db, overdrive=overdrive)
        loans, holds = circulation.sync_bookshelf(patron, "dummy pin")

        # All four loans in the sample data were created.
        eq_(4, len(loans))
        eq_(loans, patron.loans)

        eq_([], holds)

        # Running the sync again leaves all four loans in place.
        overdrive.queue_response(200, content=loans_data)
        overdrive.queue_response(200, content=holds_data)
        loans, holds = circulation.sync_bookshelf(patron, "dummy pin")
        eq_(4, len(loans))
        eq_(loans, patron.loans)        

    def test_sync_bookshelf_removes_loans_not_present_on_remote(self):
        loans_data, json_loans = self.sample_json("shelf_with_some_checked_out_books.json")
        holds_data, json_holds = self.sample_json("no_holds.json")

        overdrive = MockOverdriveAPI(self._db)
        overdrive.queue_response(200, content=loans_data)
        overdrive.queue_response(200, content=holds_data)

        # Create a loan not present in the sample data.
        patron = self._patron()
        overdrive_edition, new = self._edition(
            data_source_name=DataSource.OVERDRIVE,
            with_license_pool=True
        )
        overdrive_loan, new = overdrive_edition.license_pool.loan_to(patron)
        yesterday = datetime.utcnow() - timedelta(days=1)
        overdrive_loan.start = yesterday

        # Sync with Overdrive, and the loan not present in the sample
        # data is removed.
        circulation = CirculationAPI(self._db, overdrive=overdrive)
        loans, holds = circulation.sync_bookshelf(patron, "dummy pin")

        eq_(4, len(loans))
        eq_(loans, patron.loans)
        assert overdrive_loan not in patron.loans

    def test_sync_bookshelf_ignores_loans_from_other_sources(self):
        patron = self._patron()
        gutenberg, new = self._edition(data_source_name=DataSource.GUTENBERG,
                                       with_license_pool=True)
        gutenberg_loan, new = gutenberg.license_pool.loan_to(patron)
        loans_data, json_loans = self.sample_json("shelf_with_some_checked_out_books.json")
        holds_data, json_holds = self.sample_json("no_holds.json")     
   
        # Overdrive doesn't know about the Gutenberg loan, but it was
        # not destroyed, because it came from another source.
        overdrive = MockOverdriveAPI(self._db)
        overdrive.queue_response(200, content=loans_data)
        overdrive.queue_response(200, content=holds_data)
        circulation = CirculationAPI(self._db, overdrive=overdrive)

        loans, holds = circulation.sync_bookshelf(patron, "dummy pin")
        eq_(5, len(patron.loans))
        assert gutenberg_loan in patron.loans

    def test_sync_bookshelf_creates_local_holds(self):
        
        loans_data, json_loans = self.sample_json("no_loans.json")
        holds_data, json_holds = self.sample_json("holds.json")

        overdrive = MockOverdriveAPI(self._db)
        overdrive.queue_response(200, content=loans_data)
        overdrive.queue_response(200, content=holds_data)
        circulation = CirculationAPI(self._db, overdrive=overdrive)
        patron = self._patron()

        loans, holds = circulation.sync_bookshelf(patron, "dummy pin")
        # All four loans in the sample data were created.
        eq_(4, len(holds))
        eq_(holds, patron.holds)

        # Running the sync again leaves all four holds in place.
        overdrive.queue_response(200, content=loans_data)
        overdrive.queue_response(200, content=holds_data)
        circulation = CirculationAPI(self._db, overdrive=overdrive)
        loans, holds = circulation.sync_bookshelf(patron, "dummy pin")
        eq_(4, len(holds))
        eq_(holds, patron.holds)        

    def test_sync_bookshelf_removes_holds_not_present_on_remote(self):
        loans_data, json_loans = self.sample_json("no_loans.json")
        holds_data, json_holds = self.sample_json("holds.json")
        
        patron = self._patron()
        overdrive_edition, new = self._edition(data_source_name=DataSource.OVERDRIVE,
                                       with_license_pool=True)
        overdrive_hold, new = overdrive_edition.license_pool.on_hold_to(patron)


        overdrive = MockOverdriveAPI(self._db)
        overdrive.queue_response(200, content=loans_data)
        overdrive.queue_response(200, content=holds_data)

        # The hold not present in the sample data has been removed
        circulation = CirculationAPI(self._db, overdrive=overdrive)
        loans, holds = circulation.sync_bookshelf(patron, "dummy pin")
        eq_(4, len(holds))
        eq_(holds, patron.holds)
        assert overdrive_hold not in patron.loans

    def test_sync_bookshelf_ignores_holds_from_other_sources(self):
        loans_data, json_loans = self.sample_json("no_loans.json")
        holds_data, json_holds = self.sample_json("holds.json")

        patron = self._patron()
        threem, new = self._edition(data_source_name=DataSource.THREEM,
                                    with_license_pool=True)
        threem_hold, new = threem.license_pool.on_hold_to(patron)
   
        overdrive = MockOverdriveAPI(self._db)
        overdrive.queue_response(200, content=loans_data)
        overdrive.queue_response(200, content=holds_data)

        # Overdrive doesn't know about the 3M hold, but it was
        # not destroyed, because it came from another source.
        circulation = CirculationAPI(self._db, overdrive=overdrive)
        loans, holds = circulation.sync_bookshelf(patron, "dummy pin")
        eq_(5, len(patron.holds))
        assert threem_hold in patron.holds
