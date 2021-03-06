import json
import logging
import urllib
from collections import Counter
from nose.tools import set_trace

from core.config import Configuration
from core.coverage import (
    CoverageFailure,
    CoverageProvider,
)
from core.metadata_layer import (
    ContributorData,
    IdentifierData,
    LinkData,
    MeasurementData,
    Metadata,
    SubjectData,
)
from core.model import (
    DataSource,
    Hyperlink,
    Identifier,
    Measurement,
    Representation,
    Subject,
)
from core.util import TitleProcessor

class NoveListAPI(object):

    IS_CONFIGURED = None
    log = logging.getLogger("NoveList API")
    version = "2.2"

    NO_ISBN_EQUIVALENCY = "No clear ISBN equivalency: %r"

    # While the NoveList API doesn't require parameters to be passed via URL,
    # the Representation object needs a unique URL to return the proper data
    # from the database.
    QUERY_ENDPOINT = "http://novselect.ebscohost.com/Data/ContentByQuery?\
            ISBN=%(ISBN)s&ClientIdentifier=%(ClientIdentifier)s&version=%(version)s"
    MAX_REPRESENTATION_AGE = 14*24*60*60      # two weeks

    @classmethod
    def from_config(cls, _db):
        config = Configuration.integration(Configuration.NOVELIST_INTEGRATION)
        profile, password = cls.values()
        if not (profile and password):
            raise ValueError("No NoveList client configured.")
        return cls(_db, profile, password)

    @classmethod
    def values(cls):
        config = Configuration.integration(Configuration.NOVELIST_INTEGRATION)
        profile = config.get(Configuration.NOVELIST_PROFILE)
        password = config.get(Configuration.NOVELIST_PASSWORD)
        return (profile, password)

    @classmethod
    def is_configured(cls):
        if cls.IS_CONFIGURED is None:
            profile, password = cls.values()
            cls.IS_CONFIGURED = bool(profile and password)
        return cls.IS_CONFIGURED

    def __init__(self, _db, profile, password):
        self._db = _db
        self.profile = profile
        self.password = password

    @property
    def source(self):
        return DataSource.lookup(self._db, DataSource.NOVELIST)

    def lookup_equivalent_isbns(self, identifier):
        """Finds NoveList data for all ISBNs equivalent to an identifier.

        :return: Metadata object or None
        """
        lookup_metadata = []
        license_sources = DataSource.license_sources_for(self._db, identifier)
        # Look up strong ISBN equivalents.
        for license_source in license_sources:
            lookup_metadata += [self.lookup(eq.output)
                for eq in identifier.equivalencies
                if (eq.data_source==license_source and eq.strength==1
                    and eq.output.type==Identifier.ISBN)]

        if not lookup_metadata:
            self.log.error(
                "Identifiers without an ISBN equivalent can't \
                be looked up with NoveList: %r", identifier
            )
            return None

        # Remove None values.
        lookup_metadata = [metadata for metadata in lookup_metadata if metadata]
        if not lookup_metadata:
            return None

        best_metadata = self.choose_best_metadata(lookup_metadata, identifier)
        if not best_metadata:
            metadata, confidence = best_metadata
            if round(confidence, 2) < 0.5:
                self.log.warn(self.NO_ISBN_EQUIVALENCY, identifier)
                return None
        return metadata

    @classmethod
    def _confirm_same_identifier(self, metadata_objects):
        """Ensures that all metadata objects have the same NoveList ID"""

        novelist_ids = set([
            metadata.primary_identifier.identifier
            for metadata in metadata_objects
        ])
        return len(novelist_ids)==1

    def choose_best_metadata(self, metadata_objects, identifier):
        """Chooses the most likely book metadata from a list of Metadata objects

        Given several Metadata objects with different NoveList IDs, this
        method returns the metadata of the ID with the highest representation
        and a float representing confidence in the result.
        """
        if self._confirm_same_identifier(metadata_objects):
            # Metadata with the same NoveList ID will be identical. Take one.
            return metadata_objects[0]

        # One or more of the equivalents did not return the same NoveList work
        self.log.warn("%r has inaccurate ISBN equivalents", identifier)
        counter = Counter()
        for metadata in metadata_objects:
            counter[metadata.primary_identifier] += 1

        [(target_identifier, most_amount),
        (ignore, secondmost)] = counter.most_common(2)
        if most_amount==secondmost:
            # The counts are the same, and neither can be trusted.
            self.log.warn(self.NO_ISBN_EQUIVALENCY, identifier)
            return None
        confidence = most_amount / float(len(metadata_objects))
        target_metadata = filter(
            lambda m: m.primary_identifier==target_identifier, metadata_objects
        )
        return target_metadata[0], confidence

    def lookup(self, identifier):
        """Requests NoveList metadata for a particular identifier

        :return: Metadata object or None
        """

        client_identifier = identifier.urn
        if identifier.type != Identifier.ISBN:
            return self.lookup_equivalent_isbns(identifier)

        params = dict(
            ClientIdentifier=client_identifier, ISBN=identifier.identifier,
            version=self.version, profile=self.profile, password=self.password
        )
        url = self._build_query(params)
        self.log.debug("NoveList lookup: %s", url)
        representation, from_cache = Representation.cacheable_post(
            self._db, unicode(url), params,
            max_age=self.MAX_REPRESENTATION_AGE,
            response_reviewer=self.review_response
        )

        return self.lookup_info_to_metadata(representation)

    @classmethod
    def review_response(cls, response):
        """Performs NoveList-specific error review of the request response"""
        status_code, headers, content = response
        if status_code == 403:
            raise Exception("Invalid NoveList credentials")
        if content.startswith('"Missing'):
            raise Exception("Invalid NoveList parameters: %s" % content)
        return response

    @classmethod
    def _scrub_subtitle(cls, subtitle):
        """Removes common NoveList subtitle annoyances"""
        if subtitle:
            subtitle = subtitle.replace('[electronic resource]', '')
            # Then get rid of any leading whitespace or punctuation.
            subtitle = TitleProcessor.extract_subtitle('', subtitle)
        return subtitle

    @classmethod
    def _build_query(cls, params):
        """Builds a unique and url-encoded query endpoint"""

        for name, value in params.items():
            params[name] = urllib.quote(value)
        return (cls.QUERY_ENDPOINT % params).replace(" ", "")

    def lookup_info_to_metadata(self, lookup_representation):
        """Transforms a NoveList JSON representation into a Metadata object"""

        if not lookup_representation.content:
            return None

        lookup_info = json.loads(lookup_representation.content)
        book_info = lookup_info['TitleInfo']
        if book_info:
            novelist_identifier = book_info.get('ui')
        if not book_info or not novelist_identifier:
            # NoveList didn't know the ISBN.
            return None

        primary_identifier, ignore = Identifier.for_foreign_id(
            self._db, Identifier.NOVELIST_ID, novelist_identifier
        )
        metadata = Metadata(self.source, primary_identifier=primary_identifier)

        # Get the equivalent ISBN identifiers.
        metadata.identifiers += self._extract_isbns(book_info)

        author = book_info.get('author')
        if author:
            metadata.contributors.append(ContributorData(sort_name=author))

        description = book_info.get('description')
        if description:
            metadata.links.append(LinkData(
                rel=Hyperlink.DESCRIPTION, content=description,
                media_type=Representation.TEXT_PLAIN
            ))

        audience_level = book_info.get('audience_level')
        if audience_level:
            metadata.subjects.append(SubjectData(
                Subject.FREEFORM_AUDIENCE, audience_level
            ))

        novelist_rating = book_info.get('rating')
        if novelist_rating:
            metadata.measurements.append(MeasurementData(
                Measurement.RATING, novelist_rating
            ))

        # Extract feature content if it is available.
        series_info = None
        appeals_info = None
        lexile_info = None
        goodreads_info = None
        recommendations_info = None
        feature_content = lookup_info.get('FeatureContent')
        if feature_content:
            series_info = feature_content.get('SeriesInfo')
            appeals_info = feature_content.get('Appeals')
            lexile_info = feature_content.get('LexileInfo')
            goodreads_info = feature_content.get('GoodReads')
            recommendations_info = feature_content.get('SimilarTitles')

        metadata, title_key = self.get_series_information(
            metadata, series_info, book_info
        )
        metadata.title = book_info.get(title_key)
        subtitle = TitleProcessor.extract_subtitle(
            metadata.title, book_info.get('full_title')
        )
        metadata.subtitle = self._scrub_subtitle(subtitle)

        if appeals_info:
            extracted_genres = False
            for appeal in appeals_info:
                genres = appeal.get('genres')
                if genres:
                    for genre in genres:
                        metadata.subjects.append(SubjectData(
                            Subject.TAG, genre['Name']
                        ))
                        extracted_genres = True
                if extracted_genres:
                    break

        if lexile_info:
            metadata.subjects.append(SubjectData(
                Subject.LEXILE_SCORE, lexile_info['Lexile']
            ))

        if goodreads_info:
            metadata.measurements.append(MeasurementData(
                Measurement.RATING, goodreads_info['average_rating']
            ))

        metadata = self.get_recommendations(metadata, recommendations_info)

        # If nothing interesting comes from the API, ignore it.
        if not (metadata.measurements or metadata.series_position or
                metadata.series or metadata.subjects or metadata.links or
                metadata.subtitle or metadata.recommendations):
            metadata = None
        return metadata

    def get_series_information(self, metadata, series_info, book_info):
        """Returns metadata object with series info and optimal title key"""

        title_key = 'main_title'
        if series_info:
            metadata.series = series_info['full_title']
            series_titles = series_info.get('series_titles')
            if series_titles:
                matching_series_volume = [volume for volume in series_titles
                        if volume.get('full_title')==book_info.get('full_title')]
                if not matching_series_volume:
                    # If there's no full_title match, try the main_title.
                    matching_series_volume = [volume for volume in series_titles
                        if volume.get('main_title')==book_info.get('main_title')]
                if len(matching_series_volume) > 1:
                    # This probably won't happen, but if it does, it will be
                    # difficult to debug without an error.
                    raise ValueError("Multiple matching volumes found.")
                series_position = matching_series_volume[0].get('volume')
                if series_position:
                    if series_position.endswith('.'):
                        series_position = series_position[:-1]
                    metadata.series_position = int(series_position)

                # Sometimes all of the volumes in a series have the same
                # main_title so using the full_title is preferred.
                main_titles = [volume.get(title_key) for volume in series_titles]
                if len(main_titles) > 1 and len(set(main_titles))==1:
                    title_key = 'full_title'

        return metadata, title_key

    def _extract_isbns(self, book_info):
        isbns = []

        synonymous_ids = book_info.get('manifestations')
        for synonymous_id in synonymous_ids:
            isbn = synonymous_id.get('ISBN')
            if isbn:
                isbn_data = IdentifierData(Identifier.ISBN, isbn)
                isbns.append(isbn_data)

        return isbns

    def get_recommendations(self, metadata, recommendations_info):
        if not recommendations_info:
            return None

        related_books = recommendations_info.get('titles')
        related_books = filter(lambda b: b.get('is_held_locally'), related_books)
        if related_books:
            for book_info in related_books:
                metadata.recommendations += self._extract_isbns(book_info)
        return metadata


class MockNoveListAPI(object):

    def __init__(self):
        self.responses = []

    def setup(self, *args):
        self.responses = self.responses + list(args)

    def lookup(self, identifier):
        response = self.responses[0]
        self.responses = self.responses[1:]
        return response


class NoveListCoverageProvider(CoverageProvider):

    def __init__(self, _db, cutoff_time=None):
        self._db = _db
        self.api = NoveListAPI.from_config(self._db)

        super(NoveListCoverageProvider, self).__init__(
            "NoveList Coverage Provider", [Identifier.ISBN],
            self.source, batch_size=25
        )

    @property
    def source(self):
        return DataSource.lookup(self._db, DataSource.NOVELIST)

    def process_item(self, identifier):
        metadata = self.api.lookup(identifier)
        if not metadata:
            # Either NoveList didn't recognize the identifier or
            # no interesting data came of this. Consider it covered.
            return identifier

        # Set identifier equivalent to its NoveList ID.
        identifier.equivalent_to(
            self.output_source, metadata.primary_identifier,
            strength=1
        )
        # Create an edition with the NoveList metadata & NoveList identifier.
        # This will capture equivalent ISBNs and less appealing metadata in
        # its unfettered state on the NoveList identifier alone.
        edition, ignore = metadata.edition(self._db)
        metadata.apply(edition)

        if edition.series or edition.series_position:
            metadata.primary_identifier = identifier
            # Series data from NoveList is appealing, but we need to avoid
            # creating any potentially-inaccurate ISBN equivalencies on the
            # license source identifier.
            #
            # So just remove the identifiers from the metadata object entirely.
            metadata.identifiers = []
            # Before creating an edition for the original identifier.
            novelist_edition, ignore = metadata.edition(self._db)
            metadata.apply(novelist_edition)

        return identifier
