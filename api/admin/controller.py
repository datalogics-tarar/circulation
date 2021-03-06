from nose.tools import set_trace
import logging
import sys
import os
import base64
import random
import uuid
import json

import flask
from flask import (
    Response,
    redirect,
)
from flask.ext.babel import lazy_gettext as _

from core.model import (
    get_one,
    get_one_or_create,
    Admin,
    AdminAuthenticationService,
    CirculationEvent,
    Classification,
    DataSource,
    Edition,
    Genre,
    Hold,
    Hyperlink,
    Identifier,
    LicensePool,
    Loan,
    Patron,
    PresentationCalculationPolicy,
    Subject,
    Work,
    WorkGenre,
)
from core.util.problem_detail import ProblemDetail
from problem_details import *

from api.config import (
    Configuration, 
    CannotLoadConfiguration
)

from oauth import GoogleAuthService

from api.controller import CirculationManagerController
from api.coverage import MetadataWranglerCoverageProvider
from core.app_server import entry_response
from core.app_server import (
    entry_response, 
    feed_response,
    load_pagination_from_request
)
from core.model import (
    Collection,
    Library,
)
from core.opds import AcquisitionFeed
from opds import AdminAnnotator, AdminFeed
from collections import Counter
from core.classifier import (
    genres,
    SimplifiedGenreClassifier,
    NO_NUMBER,
    NO_VALUE
)
from datetime import datetime, timedelta
from sqlalchemy.sql import func
from sqlalchemy.sql.expression import desc, nullslast, or_, and_, distinct, select, join
from sqlalchemy.orm import lazyload


def setup_admin_controllers(manager):
    """Set up all the controllers that will be used by the admin parts of the web app."""
    if not manager.testing:
        try:
            manager.config = Configuration.load()
        except CannotLoadConfiguration, e:
            self.log.error("Could not load configuration file: %s" % e)
            sys.exit()

    manager.admin_work_controller = WorkController(manager)
    manager.admin_sign_in_controller = SignInController(manager)
    manager.admin_feed_controller = FeedController(manager)
    manager.admin_dashboard_controller = DashboardController(manager)
    manager.admin_settings_controller = SettingsController(manager)


class AdminController(object):

    def __init__(self, manager):
        self.manager = manager
        self._db = self.manager._db
        self.url_for = self.manager.url_for
        self.cdn_url_for = self.manager.cdn_url_for

    @property
    def auth(self):
        auth_service = get_one(self._db, AdminAuthenticationService)
        if auth_service and auth_service.provider == AdminAuthenticationService.GOOGLE_OAUTH:
            return GoogleAuthService(
                auth_service,
                self.url_for('google_auth_callback'),
                test_mode=self.manager.testing,
            )
        return None

    def authenticated_admin_from_request(self):
        """Returns an authenticated admin or begins the Google OAuth flow"""
        if not self.auth:
            return ADMIN_AUTH_NOT_CONFIGURED

        access_token = flask.session.get("admin_access_token")
        if access_token:
            admin = get_one(self._db, Admin, access_token=access_token)
            if admin and self.auth.active_credentials(admin):
                return admin
        return INVALID_ADMIN_CREDENTIALS

    def authenticated_admin(self, admin_details):
        """Creates or updates an admin with the given details"""

        admin, ignore = get_one_or_create(
            self._db, Admin, email=admin_details['email']
        )
        admin.update_credentials(
            self._db, admin_details['access_token'], admin_details['credentials']
        )
        return admin

    def check_csrf_token(self):
        """Verifies that the provided CSRF token is valid."""
        token = self.get_csrf_token()
        if not token or token != flask.request.form.get("csrf_token"):
            return INVALID_CSRF_TOKEN
        return token

    def get_csrf_token(self):
        """Returns the CSRF token for the current session."""
        return flask.session.get("csrf_token")

class SignInController(AdminController):

    ERROR_RESPONSE_TEMPLATE = """<!DOCTYPE HTML>
<html lang="en">
<head><meta charset="utf8"></head>
</body>
<p><strong>%(status_code)d ERROR:</strong> %(message)s</p>
</body>
</html>"""

    def sign_in(self):
        """Redirects admin if they're signed in."""
        if not self.auth:
            return ADMIN_AUTH_NOT_CONFIGURED

        admin = self.authenticated_admin_from_request()

        if isinstance(admin, ProblemDetail):
            redirect_url = flask.request.args.get("redirect")
            return redirect(self.auth.auth_uri(redirect_url), Response=Response)
        elif admin:
            return redirect(flask.request.args.get("redirect"), Response=Response)

    def redirect_after_sign_in(self):
        """Uses the Google OAuth client to determine admin details upon
        callback. Barring error, redirects to the provided redirect url.."""
        if not self.auth:
            return ADMIN_AUTH_NOT_CONFIGURED

        admin_details, redirect_url = self.auth.callback(flask.request.args)
        if isinstance(admin_details, ProblemDetail):
            return self.error_response(admin_details)

        if not self.staff_email(admin_details['email']):
            return self.error_response(INVALID_ADMIN_CREDENTIALS)
        else:
            admin = self.authenticated_admin(admin_details)
            flask.session["admin_access_token"] = admin_details.get("access_token")
            flask.session["csrf_token"] = base64.b64encode(os.urandom(24))
            return redirect(redirect_url, Response=Response)
    
    def staff_email(self, email):
        """Checks the domain of an email address against the admin-authorized
        domain"""
        if not self.auth:
            return False

        staff_domains = self.auth.domains
        domain = email[email.index('@')+1:]
        return domain.lower() in [staff_domain.lower() for staff_domain in staff_domains]

    def error_response(self, problem_detail):
        """Returns a problem detail as an HTML response"""
        html = self.ERROR_RESPONSE_TEMPLATE % dict(
            status_code=problem_detail.status_code,
            message=problem_detail.detail
        )
        return Response(html, problem_detail.status_code)

class WorkController(CirculationManagerController):

    STAFF_WEIGHT = 1

    def details(self, data_source, identifier_type, identifier):
        """Return an OPDS entry with detailed information for admins.
        
        This includes relevant links for editing the book.
        """

        pool = self.load_licensepool(data_source, identifier_type, identifier)
        if isinstance(pool, ProblemDetail):
            return pool
        work = pool.work

        annotator = AdminAnnotator(self.circulation)
        return entry_response(
            AcquisitionFeed.single_entry(self._db, work, annotator)
        )
        
    def complaints(self, data_source, identifier_type, identifier):
        """Return detailed complaint information for admins."""
        
        pool = self.load_licensepool(data_source, identifier_type, identifier)
        if isinstance(pool, ProblemDetail):
            return pool
        counter = self._count_complaints_for_licensepool(pool)
        response = dict({
            "book": { 
                "data_source": data_source,
                "identifier_type": identifier_type,
                "identifier": identifier
            },
            "complaints": counter
        })
        
        return response

    def edit(self, data_source, identifier_type, identifier):
        """Edit a work's metadata."""

        pool = self.load_licensepool(data_source, identifier_type, identifier)
        if isinstance(pool, ProblemDetail):
            return pool
        work = pool.work
        changed = False

        staff_data_source = DataSource.lookup(self._db, DataSource.LIBRARY_STAFF)
        primary_identifier = work.presentation_edition.primary_identifier
        staff_edition, is_new = get_one_or_create(
            self._db, Edition,
            primary_identifier_id=primary_identifier.id,
            data_source_id=staff_data_source.id
        )
        self._db.expire(primary_identifier)

        new_title = flask.request.form.get("title")
        if new_title and work.title != new_title:
            staff_edition.title = unicode(new_title)
            changed = True

        new_subtitle = flask.request.form.get("subtitle")
        if work.subtitle != new_subtitle:
            if work.subtitle and not new_subtitle:
                new_subtitle = NO_VALUE
            staff_edition.subtitle = unicode(new_subtitle)
            changed = True

        new_series = flask.request.form.get("series")
        if work.series != new_series:
            if work.series and not new_series:
                new_series = NO_VALUE
            staff_edition.series = unicode(new_series)
            changed = True

        new_series_position = flask.request.form.get("series_position")
        if new_series_position:
            try:
                new_series_position = int(new_series_position)
            except ValueError:
                return INVALID_SERIES_POSITION
        else:
            new_series_position = None
        if work.series_position != new_series_position:
            if work.series_position and not new_series_position:
                new_series_position = NO_NUMBER
            staff_edition.series_position = new_series_position
            changed = True

        new_summary = flask.request.form.get("summary") or ""
        if new_summary != work.summary_text:
            old_summary = None
            if work.summary and work.summary.data_source == staff_data_source:
                old_summary = work.summary

            work.presentation_edition.primary_identifier.add_link(
                Hyperlink.DESCRIPTION, None,
                staff_data_source, content=new_summary)

            # Delete previous staff summary
            if old_summary:
                for link in old_summary.links:
                    self._db.delete(link)
                self._db.delete(old_summary)

            changed = True

        if changed:
            # Even if the presentation doesn't visibly change, we want
            # to regenerate the OPDS entries and update the search
            # index for the work, because that might be the 'real'
            # problem the user is trying to fix.
            policy = PresentationCalculationPolicy(
                classify=True,
                regenerate_opds_entries=True,
                update_search_index=True,
                choose_summary=True
            )
            work.calculate_presentation(policy=policy)
        return Response("", 200)

    def suppress(self, data_source, identifier_type, identifier):
        """Suppress the license pool associated with a book."""
        
        # Turn source + identifier into a LicensePool
        pool = self.load_licensepool(data_source, identifier_type, identifier)
        if isinstance(pool, ProblemDetail):
            # Something went wrong.
            return pool
    
        pool.suppressed = True
        return Response("", 200)

    def unsuppress(self, data_source, identifier_type, identifier):
        """Unsuppress the license pool associated with a book."""
        
        # Turn source + identifier into a LicensePool
        pool = self.load_licensepool(data_source, identifier_type, identifier)
        if isinstance(pool, ProblemDetail):
            # Something went wrong.
            return pool
    
        pool.suppressed = False
        return Response("", 200)

    def refresh_metadata(self, data_source, identifier_type, identifier, provider=None):
        """Refresh the metadata for a book from the content server"""
        if not provider:
            provider = MetadataWranglerCoverageProvider(self._db)

        pool = self.load_licensepool(data_source, identifier_type, identifier)
        if isinstance(pool, ProblemDetail):
            return pool
        try:
            record = provider.ensure_coverage(pool.identifier, force=True)
        except Exception:
            # The coverage provider may raise an HTTPIntegrationException.
            return REMOTE_INTEGRATION_FAILED

        if record.exception:
            # There was a coverage failure.
            if (isinstance(record.exception, int)
                and record.exception in [201, 202]):
                # A 201/202 error means it's never looked up this work before
                # so it's started the resolution process or looking for sources.
                return METADATA_REFRESH_PENDING
            # Otherwise, it just doesn't know anything.
            return METADATA_REFRESH_FAILURE

        return Response("", 200)

    def resolve_complaints(self, data_source, identifier_type, identifier):
        """Resolve all complaints for a particular license pool and complaint type."""

        pool = self.load_licensepool(data_source, identifier_type, identifier)
        if isinstance(pool, ProblemDetail):
            return pool
        work = pool.work
        resolved = False
        found = False

        type = flask.request.form.get("type")
        if type:
            for complaint in pool.complaints:
                if complaint.type == type:
                    found = True
                    if complaint.resolved == None:
                        complaint.resolve()
                        resolved = True

        if not found:
            return UNRECOGNIZED_COMPLAINT
        elif not resolved:
            return COMPLAINT_ALREADY_RESOLVED
        return Response("", 200)

    def classifications(self, data_source, identifier_type, identifier):
        """Return list of this work's classifications."""

        pool = self.load_licensepool(data_source, identifier_type, identifier)
        if isinstance(pool, ProblemDetail):
            return pool

        identifier_id = pool.work.presentation_edition.primary_identifier.id
        results = self._db \
            .query(Classification) \
            .join(Subject) \
            .join(DataSource) \
            .filter(Classification.identifier_id == identifier_id) \
            .order_by(Classification.weight.desc()) \
            .all()

        data = []
        for result in results:
            data.append(dict({
                "type": result.subject.type,
                "name": result.subject.identifier,
                "source": result.data_source.name,
                "weight": result.weight
            }))

        return dict({
            "book": {
                "data_source": data_source,
                "identifier_type": identifier_type,
                "identifier": identifier
            },
            "classifications": data
        })

    def edit_classifications(self, data_source, identifier_type, identifier):
        """Edit a work's audience, target age, fiction status, and genres."""
        
        pool = self.load_licensepool(data_source, identifier_type, identifier)
        if isinstance(pool, ProblemDetail):
            return pool
        work = pool.work
        staff_data_source = DataSource.lookup(self._db, DataSource.LIBRARY_STAFF)

        # Previous staff classifications
        primary_identifier = work.presentation_edition.primary_identifier
        old_classifications = self._db \
            .query(Classification) \
            .join(Subject) \
            .filter(
                Classification.identifier == primary_identifier,
                Classification.data_source == staff_data_source
            )
        old_genre_classifications = old_classifications \
            .filter(Subject.genre_id != None)
        old_staff_genres = [
            c.subject.genre.name 
            for c in old_genre_classifications 
            if c.subject.genre
        ]
        old_computed_genres = [
            work_genre.genre.name
            for work_genre in work.work_genres
        ]

        # New genres should be compared to previously computed genres
        new_genres = flask.request.form.getlist("genres")
        genres_changed = sorted(new_genres) != sorted(old_computed_genres)

        # Update audience
        new_audience = flask.request.form.get("audience")
        if new_audience != work.audience:
            # Delete all previous staff audience classifications
            for c in old_classifications:
                if c.subject.type == Subject.FREEFORM_AUDIENCE:
                    self._db.delete(c)

            # Create a new classification with a high weight
            primary_identifier.classify(
                data_source=staff_data_source,
                subject_type=Subject.FREEFORM_AUDIENCE,
                subject_identifier=new_audience,
                weight=WorkController.STAFF_WEIGHT,
            )

        # Update target age if present
        new_target_age_min = flask.request.form.get("target_age_min")
        new_target_age_min = int(new_target_age_min) if new_target_age_min else None
        new_target_age_max = flask.request.form.get("target_age_max")
        new_target_age_max = int(new_target_age_max) if new_target_age_max else None
        if new_target_age_max < new_target_age_min:
            return INVALID_EDIT.detailed(_("Minimum target age must be less than maximum target age."))

        if work.target_age:
            old_target_age_min = work.target_age.lower
            old_target_age_max = work.target_age.upper
        else:
            old_target_age_min = None
            old_target_age_max = None
        if new_target_age_min != old_target_age_min or new_target_age_max != old_target_age_max:
            # Delete all previous staff target age classifications
            for c in old_classifications:
                if c.subject.type == Subject.AGE_RANGE:
                    self._db.delete(c)

            # Create a new classification with a high weight - higher than audience
            if new_target_age_min and new_target_age_max:
                age_range_identifier = "%s-%s" % (new_target_age_min, new_target_age_max)
                primary_identifier.classify(
                    data_source=staff_data_source,
                    subject_type=Subject.AGE_RANGE,
                    subject_identifier=age_range_identifier,
                    weight=WorkController.STAFF_WEIGHT * 100,
                )

        # Update fiction status
        # If fiction status hasn't changed but genres have changed,
        # we still want to ensure that there's a staff classification
        new_fiction = True if flask.request.form.get("fiction") == "fiction" else False
        if new_fiction != work.fiction or genres_changed:
            # Delete previous staff fiction classifications
            for c in old_classifications:
                if c.subject.type == Subject.SIMPLIFIED_FICTION_STATUS:
                    self._db.delete(c)

            # Create a new classification with a high weight (higher than genre)
            fiction_term = "Fiction" if new_fiction else "Nonfiction"
            classification = primary_identifier.classify(
                data_source=staff_data_source,
                subject_type=Subject.SIMPLIFIED_FICTION_STATUS,
                subject_identifier=fiction_term,
                weight=WorkController.STAFF_WEIGHT,
            )
            classification.subject.fiction = new_fiction

        # Update genres
        # make sure all new genres are legit
        for name in new_genres:
            genre, is_new = Genre.lookup(self._db, name)
            if not isinstance(genre, Genre):
                return GENRE_NOT_FOUND
            if genres[name].is_fiction != new_fiction:
                return INCOMPATIBLE_GENRE
            if name == "Erotica" and new_audience != "Adults Only":
                return EROTICA_FOR_ADULTS_ONLY

        if genres_changed:
            # delete existing staff classifications for genres that aren't being kept
            for c in old_genre_classifications:
                if c.subject.genre.name not in new_genres:
                    self._db.delete(c)

            # add new staff classifications for new genres
            for genre in new_genres:
                if genre not in old_staff_genres:
                    classification = primary_identifier.classify(
                        data_source=staff_data_source,
                        subject_type=Subject.SIMPLIFIED_GENRE,
                        subject_identifier=genre,
                        weight=WorkController.STAFF_WEIGHT
                    )

            # add NONE genre classification if we aren't keeping any genres
            if len(new_genres) == 0:
                primary_identifier.classify(
                    data_source=staff_data_source,
                    subject_type=Subject.SIMPLIFIED_GENRE,
                    subject_identifier=SimplifiedGenreClassifier.NONE,
                    weight=WorkController.STAFF_WEIGHT
                )
            else: 
                # otherwise delete existing NONE genre classification
                none_classifications = self._db \
                    .query(Classification) \
                    .join(Subject) \
                    .filter(
                        Classification.identifier == primary_identifier,
                        Subject.identifier == SimplifiedGenreClassifier.NONE
                    ) \
                    .all()
                for c in none_classifications:
                    self._db.delete(c)

        # Update presentation
        policy = PresentationCalculationPolicy(
            classify=True,
            regenerate_opds_entries=True,
            update_search_index=True
        )
        work.calculate_presentation(policy=policy)

        return Response("", 200)

    def _count_complaints_for_licensepool(self, pool):
        complaint_types = [complaint.type for complaint in pool.complaints if complaint.resolved == None]
        return Counter(complaint_types)

    
class FeedController(CirculationManagerController):

    def complaints(self):
        this_url = self.url_for('complaints')
        annotator = AdminAnnotator(self.circulation)
        pagination = load_pagination_from_request()
        if isinstance(pagination, ProblemDetail):
            return pagination
        opds_feed = AdminFeed.complaints(
            _db=self._db, title="Complaints",
            url=this_url, annotator=annotator,
            pagination=pagination
        )
        return feed_response(opds_feed)    

    def suppressed(self):
        this_url = self.url_for('suppressed')
        annotator = AdminAnnotator(self.circulation)
        pagination = load_pagination_from_request()
        if isinstance(pagination, ProblemDetail):
            return pagination
        opds_feed = AdminFeed.suppressed(
            _db=self._db, title="Hidden Books",
            url=this_url, annotator=annotator,
            pagination=pagination
        )
        return feed_response(opds_feed)

    def genres(self):
        data = dict({
            "Fiction": dict({}),
            "Nonfiction": dict({})
        })
        for name in genres:
            top = "Fiction" if genres[name].is_fiction else "Nonfiction"
            data[top][name] = dict({
                "name": name,
                "parents": [parent.name for parent in genres[name].parents],
                "subgenres": [subgenre.name for subgenre in genres[name].subgenres]
            })
        return data

class DashboardController(CirculationManagerController):

    def stats(self):
        patron_count = self._db.query(Patron).count()

        active_loans_patron_count = self._db.query(
            distinct(Patron.id)
        ).join(
            Patron.loans
        ).filter(
            Loan.end >= datetime.now(),
        ).count()

        active_patrons = select(
            [Patron.id]
        ).select_from(
            join(
                Loan,
                Patron,
                and_(
                    Patron.id == Loan.patron_id,
                    Loan.id != None,
                    Loan.end >= datetime.now()
                )
            )
        ).union(
            select(
                [Patron.id]
            ).select_from(
                join(
                    Hold,
                    Patron,
                    Patron.id == Hold.patron_id
                )
            )
        ).alias()
        

        active_loans_or_holds_patron_count_query = select(
            [func.count(distinct(active_patrons.c.id))]
        ).select_from(
            active_patrons
        )

        result = self._db.execute(active_loans_or_holds_patron_count_query)
        active_loans_or_holds_patron_count = [r[0] for r in result][0]

        loan_count = self._db.query(
            Loan
        ).filter(
            Loan.end >= datetime.now()
        ).count()

        hold_count = self._db.query(Hold).count()

        data_sources = dict(
            overdrive=DataSource.OVERDRIVE,
            bibliotheca=DataSource.BIBLIOTHECA,
            axis360=DataSource.AXIS_360,
        )
        vendor_counts = dict()

        for key, data_source in data_sources.iteritems():
            data_source_count = self._db.query(
                LicensePool
            ).join(
                DataSource
            ).filter(
                LicensePool.licenses_owned > 0
            ).filter(
                DataSource.name == data_source
            ).count()

            if data_source_count > 0:
                vendor_counts[key] = data_source_count

        open_access_count = self._db.query(
            LicensePool
         ).filter(
            LicensePool.open_access == True
         ).count()

        if open_access_count > 0:
            vendor_counts['open_access'] = open_access_count

        title_count = self._db.query(LicensePool).count()

        # The sum queries return None instead of 0 if there are
        # no license pools in the db.

        license_count = self._db.query(
            func.sum(LicensePool.licenses_owned)
        ).filter(
            LicensePool.open_access == False,
        ).all()[0][0] or 0

        available_license_count = self._db.query(
            func.sum(LicensePool.licenses_available)
        ).filter(
            LicensePool.open_access == False,
        ).all()[0][0] or 0

        return dict(
            patrons=dict(
                total=patron_count,
                with_active_loans=active_loans_patron_count,
                with_active_loans_or_holds=active_loans_or_holds_patron_count,
                loans=loan_count,
                holds=hold_count,
            ),
            inventory=dict(
                titles=title_count,
                licenses=license_count,
                available_licenses=available_license_count,
            ),
            vendors=vendor_counts,
        )

    def circulation_events(self):
        annotator = AdminAnnotator(self.circulation)
        num = min(int(flask.request.args.get("num", "100")), 500)

        results = self._db.query(CirculationEvent) \
            .join(LicensePool) \
            .join(Work) \
            .join(DataSource) \
            .join(Identifier) \
            .order_by(nullslast(desc(CirculationEvent.start))) \
            .limit(num) \
            .all()

        events = map(lambda result: {
            "id": result.id,
            "type": result.type,
            "patron_id": result.foreign_patron_id,
            "time": result.start,
            "book": {
                "title": result.license_pool.work.title,
                "url": annotator.permalink_for(result.license_pool.work, result.license_pool, result.license_pool.identifier)
            }
        }, results)

        return dict({ "circulation_events": events })

    def bulk_circulation_events(self):
        default = str(datetime.today()).split(" ")[0]
        date = flask.request.args.get("date", default)
        next_date = datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)
            
        query = self._db.query(
                CirculationEvent, Identifier, Work, Edition
            ) \
            .join(LicensePool, LicensePool.id == CirculationEvent.license_pool_id) \
            .join(Identifier, Identifier.id == LicensePool.identifier_id) \
            .join(Work, Work.id == LicensePool.work_id) \
            .join(Edition, Edition.id == Work.presentation_edition_id) \
            .filter(CirculationEvent.start >= date) \
            .filter(CirculationEvent.start < next_date) \
            .order_by(CirculationEvent.start.asc())
        query = query \
            .options(lazyload(Identifier.licensed_through)) \
            .options(lazyload(Work.license_pools))
        results = query.all()
        
        work_ids = map(lambda result: result[2].id, results)

        subquery = self._db \
            .query(WorkGenre.work_id, Genre.name) \
            .join(Genre) \
            .filter(WorkGenre.work_id.in_(work_ids)) \
            .order_by(WorkGenre.affinity.desc()) \
            .subquery()
        genre_query = self._db \
            .query(subquery.c.work_id, func.string_agg(subquery.c.name, ",")) \
            .select_from(subquery) \
            .group_by(subquery.c.work_id)
        genres = dict(genre_query.all())

        header = [
            "time", "event", "identifier", "identifier_type", "title", "author", 
            "fiction", "audience", "publisher", "language", "target_age", "genres"
        ]

        def result_to_row(result):
            (event, identifier, work, edition) = result
            return [
                str(event.start) or "",
                event.type,
                identifier.identifier,
                identifier.type,
                edition.title,
                edition.author,
                "fiction" if work.fiction else "nonfiction",
                work.audience,
                edition.publisher,
                edition.language,
                work.target_age_string,
                genres.get(work.id)
            ]

        return [header] + map(result_to_row, results), date

class SettingsController(CirculationManagerController):

    def libraries(self):
        if flask.request.method == 'GET':
            libraries = [
                dict(
                    uuid=library.uuid,
                    name=library.name,
                    short_name=library.short_name,
                    library_registry_short_name=library.library_registry_short_name,
                    library_registry_shared_secret=library.library_registry_shared_secret
                )
                for library in self._db.query(Library).order_by(Library.name).all()
            ]
        
            return dict(libraries=libraries)


        library_uuid = flask.request.form.get("uuid")
        name = flask.request.form.get("name")
        short_name = flask.request.form.get("short_name")
        registry_short_name = flask.request.form.get("library_registry_short_name")
        registry_shared_secret = flask.request.form.get("library_registry_shared_secret")
        use_random_registry_shared_secret = "random_library_registry_shared_secret" in flask.request.form

        libraries = self._db.query(Library).all()
        is_new = False

        if libraries:
            # Currently there can only be one library, and one already exists.
            [library] = libraries
            if library.uuid != library_uuid:
                return LIBRARY_NOT_FOUND
        else:
            library, is_new = get_one_or_create(
                self._db, Library, create_method_kwargs=dict(
                    uuid=str(uuid.uuid4())
                )
            )

        if registry_shared_secret and use_random_registry_shared_secret:
            return CANNOT_SET_BOTH_RANDOM_AND_SPECIFIC_SECRET

        if use_random_registry_shared_secret:
            if library.library_registry_shared_secret:
                return CANNOT_REPLACE_EXISTING_SECRET_WITH_RANDOM_SECRET
            registry_shared_secret = "".join(
                [random.choice('1234567890abcdef') for x in range(32)]
            )

        if name:
            library.name = name
        if short_name:
            library.short_name = short_name
        if registry_short_name:
            library.library_registry_short_name = registry_short_name
        if registry_shared_secret:
            library.library_registry_shared_secret = registry_shared_secret

        if is_new:
            return Response(unicode(_("Success")), 201)
        else:
            return Response(unicode(_("Success")), 200)

    def collections(self):
        protocols = []
        
        protocols.append({
            "name": Collection.OPDS_IMPORT,
            "fields": [
                { "key": "external_account_id", "label": _("URL") },
            ],
        })

        protocols.append({
            "name": Collection.OVERDRIVE,
            "fields": [
                { "key": "external_account_id", "label": _("Library ID") },
                { "key": "website_id", "label": _("Website ID") },
                { "key": "username", "label": _("Client Key") },
                { "key": "password", "label": _("Client Secret") },
            ],
        })

        protocols.append({
            "name": Collection.BIBLIOTHECA,
            "fields": [
                { "key": "username", "label": _("Account ID") },
                { "key": "password", "label": _("Account Key") },
                { "key": "external_account_id", "label": _("Library ID") },
            ],
        })

        protocols.append({
            "name": Collection.AXIS_360,
            "fields": [
                { "key": "username", "label": _("Username") },
                { "key": "password", "label": _("Password") },
                { "key": "external_account_id", "label": _("Library ID") },
                { "key": "url", "label": _("Server") },
            ],
        })

        protocols.append({
            "name": Collection.ONE_CLICK,
            "fields": [
                { "key": "password", "label": _("Basic Token") },
                { "key": "external_account_id", "label": _("Library ID") },
                { "key": "url", "label": _("URL") },
                { "key": "ebook_loan_length", "label": _("eBook Loan Length") },
                { "key": "eaudio_loan_length", "label": _("eAudio Loan Length") },
            ],
        })

        if flask.request.method == 'GET':
            collections = []
            for c in self._db.query(Collection).order_by(Collection.name).all():
                collection = dict(
                    name=c.name,
                    protocol=c.protocol,
                    libraries=[library.short_name for library in c.libraries],
                    external_account_id=c.external_account_id,
                    url=c.external_integration.url,
                    username=c.external_integration.username,
                    password=c.external_integration.password,
                )
                if c.protocol in [p.get("name") for p in protocols]:
                    [protocol] = [p for p in protocols if p.get("name") == c.protocol]
                    for field in protocol.get("fields"):
                        key = field.get("key")
                        if key not in collection:
                            collection[key] = c.external_integration.setting(key).value
                collections.append(collection)

            return dict(
                collections=collections,
                protocols=protocols,
            )


        name = flask.request.form.get("name")
        if not name:
            return MISSING_COLLECTION_NAME

        protocol = flask.request.form.get("protocol")

        if protocol and protocol not in [p.get("name") for p in protocols]:
            return UNKNOWN_COLLECTION_PROTOCOL

        is_new = False
        collection = get_one(self._db, Collection, name=name)
        if collection:
            if protocol != collection.protocol:
                return CANNOT_CHANGE_COLLECTION_PROTOCOL

        else:
            if protocol:
                collection, is_new = get_one_or_create(
                    self._db, Collection, name=name, protocol=protocol
                )
            else:
                return NO_PROTOCOL_FOR_NEW_COLLECTION

        [protocol] = [p for p in protocols if p.get("name") == protocol]
        fields = protocol.get("fields")

        for field in fields:
            key = field.get("key")
            value = flask.request.form.get(key)
            if not value:
                # Roll back any changes to the collection that have already been made.
                self._db.rollback()
                return INCOMPLETE_COLLECTION_CONFIGURATION.detailed(
                    _("The collection configuration is missing a required field: %(field)s",
                      field=field.get("label")))

            if key == "external_account_id":
                collection.external_account_id = value
            elif key == "username":
                collection.external_integration.username = value
            elif key == "password":
                collection.external_integration.password = value
            elif key == "url":
                collection.external_integration.url = value
            else:
                collection.external_integration.setting(key).value = value

        libraries = []
        if flask.request.form.get("libraries"):
            libraries = json.loads(flask.request.form.get("libraries"))

        for short_name in libraries:
            library = get_one(self._db, Library, short_name=short_name)
            if not library:
                return NO_SUCH_LIBRARY.detailed(_("You attempted to add the collection to %(library_short_name)s, but it does not exist.", library_short_name=short_name))
            if collection not in library.collections:
                library.collections.append(collection)
        for library in collection.libraries:
            if library.short_name not in libraries:
                library.collections.remove(collection)

        if is_new:
            return Response(unicode(_("Success")), 201)
        else:
            return Response(unicode(_("Success")), 200)

    def admin_auth_services(self):
        if flask.request.method == 'GET':
            auth_services = []
            auth_service = get_one(self._db, AdminAuthenticationService)
            if auth_service and auth_service.provider == AdminAuthenticationService.GOOGLE_OAUTH:
                auth_services = [
                    dict(
                        name=auth_service.name,
                        provider=auth_service.provider,
                        url=auth_service.external_integration.url,
                        username=auth_service.external_integration.username,
                        password=auth_service.external_integration.password,
                        domains=json.loads(auth_service.external_integration.setting("domains").value),
                    )
                ]

            return dict(
                admin_auth_services=auth_services,
                providers=AdminAuthenticationService.PROVIDERS,
            )

        name = flask.request.form.get("name")
        if not name:
            return MISSING_ADMIN_AUTH_SERVICE_NAME

        provider = flask.request.form.get("provider")

        if provider and provider not in AdminAuthenticationService.PROVIDERS:
            return UNKNOWN_ADMIN_AUTH_SERVICE_PROVIDER

        is_new = False
        auth_service = get_one(self._db, AdminAuthenticationService)
        if auth_service:
            # Currently there can only be one admin auth service, and one already exists.
            if name != auth_service.name:
                return ADMIN_AUTH_SERVICE_NOT_FOUND

            if provider != auth_service.provider:
                return CANNOT_CHANGE_ADMIN_AUTH_SERVICE_PROVIDER

        else:
            if provider:
                auth_service, is_new = get_one_or_create(
                    self._db, AdminAuthenticationService, name=name, provider=provider
                )
            else:
                return NO_PROVIDER_FOR_NEW_ADMIN_AUTH_SERVICE

        # Only Google OAuth is supported for now.
        url = flask.request.form.get("url")
        username = flask.request.form.get("username")
        password = flask.request.form.get("password")
        domains = flask.request.form.get("domains")
        
        if not url or not username or not password or not domains:
            # If an admin auth service was created, make sure it
            # isn't saved in a incomplete state.
            self._db.rollback()
            return INCOMPLETE_ADMIN_AUTH_SERVICE_CONFIGURATION

        # Also make sure the domain list is valid JSON.
        try:
            json.loads(domains)
        except Exception:
            self._db.rollback()
            return INVALID_ADMIN_AUTH_DOMAIN_LIST

        integration = auth_service.external_integration
        integration.url = url
        integration.username = username
        integration.password = password
        integration.set_setting("domains", domains)

        if is_new:
            return Response(unicode(_("Success")), 201)
        else:
            return Response(unicode(_("Success")), 200)
