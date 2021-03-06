import json
from nose.tools import (
    eq_,
    set_trace,
)

from . import DatabaseTest
from core.util.problem_detail import ProblemDetail

from api.admin.oauth import (
    GoogleAuthService,
    DummyGoogleClient,
)
from core.model import AdminAuthenticationService, create

class TestGoogleAuthService(DatabaseTest):

    def test_callback(self):
        super(TestGoogleAuthService, self).setup()
        auth_service, ignore = create(
            self._db, AdminAuthenticationService,
            name="Google", provider=AdminAuthenticationService.GOOGLE_OAUTH,
        )
        self.google = GoogleAuthService(auth_service, "", test_mode=True)

        # Returns a problem detail when Google returns an error.
        error_response = self.google.callback({'error' : 'access_denied'})
        eq_(True, isinstance(error_response, ProblemDetail))
        eq_(400, error_response.status_code)
        eq_(True, error_response.detail.endswith('access_denied'))

        # Successful case creates a dict of admin details
        success, redirect = self.google.callback({'code' : 'abc'})
        eq_('example@nypl.org', success['email'])
        eq_('opensesame', success['access_token'])
        default_credentials = {"id_token": {"email": "example@nypl.org", "hd": "nypl.org"}}
        eq_(default_credentials, success['credentials'])

    def test_domains(self):
        super(TestGoogleAuthService, self).setup()
        auth_service, ignore = create(
            self._db, AdminAuthenticationService,
            name="Google", provider=AdminAuthenticationService.GOOGLE_OAUTH,
        )
        auth_service.external_integration.set_setting("domains", json.dumps(["nypl.org"]))
        
        google = GoogleAuthService(auth_service, "", test_mode=True)

        eq_(["nypl.org"], google.domains)
