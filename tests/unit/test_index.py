import logging
import os.path

import pytest
from mock import Mock
from pip._vendor import html5lib, requests

from pip._internal.download import PipSession
from pip._internal.index import (
    CandidateEvaluator, HTMLPage, Link, PackageFinder, Search,
    _check_link_requires_python, _clean_link, _determine_base_url,
    _egg_info_matches, _find_name_version_sep, _get_html_page,
)
from pip._internal.models.candidate import InstallationCandidate
from pip._internal.models.search_scope import SearchScope
from pip._internal.models.target_python import TargetPython
from tests.lib import CURRENT_PY_VERSION_INFO, make_test_finder


@pytest.mark.parametrize('requires_python, expected', [
    ('== 3.6.4', False),
    ('== 3.6.5', True),
    # Test an invalid Requires-Python value.
    ('invalid', True),
])
def test_check_link_requires_python(requires_python, expected):
    version_info = (3, 6, 5)
    link = Link('https://example.com', requires_python=requires_python)
    actual = _check_link_requires_python(link, version_info)
    assert actual == expected


def check_caplog(caplog, expected_level, expected_message):
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelname == expected_level
    assert record.message == expected_message


@pytest.mark.parametrize('ignore_requires_python, expected', [
    (None, (
        False, 'DEBUG',
        "Link requires a different Python (3.6.5 not in: '== 3.6.4'): "
        "https://example.com"
    )),
    (True, (
        True, 'DEBUG',
        "Ignoring failed Requires-Python check (3.6.5 not in: '== 3.6.4') "
        "for link: https://example.com"
    )),
])
def test_check_link_requires_python__incompatible_python(
    caplog, ignore_requires_python, expected,
):
    """
    Test an incompatible Python.
    """
    expected_return, expected_level, expected_message = expected
    link = Link('https://example.com', requires_python='== 3.6.4')
    caplog.set_level(logging.DEBUG)
    actual = _check_link_requires_python(
        link, version_info=(3, 6, 5),
        ignore_requires_python=ignore_requires_python,
    )
    assert actual == expected_return

    check_caplog(caplog, expected_level, expected_message)


def test_check_link_requires_python__invalid_requires(caplog):
    """
    Test the log message for an invalid Requires-Python.
    """
    link = Link('https://example.com', requires_python='invalid')
    caplog.set_level(logging.DEBUG)
    actual = _check_link_requires_python(link, version_info=(3, 6, 5))
    assert actual

    expected_message = (
        "Ignoring invalid Requires-Python ('invalid') for link: "
        "https://example.com"
    )
    check_caplog(caplog, 'DEBUG', expected_message)


class TestCandidateEvaluator:

    def test_init__target_python(self):
        """
        Test the target_python argument.
        """
        target_python = TargetPython(py_version_info=(3, 7, 3))
        evaluator = CandidateEvaluator(
            allow_yanked=True,
            target_python=target_python,
        )
        # The target_python attribute should be set as is.
        assert evaluator._target_python is target_python

    def test_init__target_python_none(self):
        """
        Test passing None for the target_python argument.
        """
        evaluator = CandidateEvaluator(
            allow_yanked=True,
            target_python=None,
        )
        # Spot-check the default TargetPython object.
        actual_target_python = evaluator._target_python
        assert actual_target_python._given_py_version_info is None
        assert actual_target_python.py_version_info == CURRENT_PY_VERSION_INFO

    @pytest.mark.parametrize(
        'py_version_info,ignore_requires_python,expected', [
            ((3, 6, 5), None, (True, '1.12')),
            # Test an incompatible Python.
            ((3, 6, 4), None, (False, None)),
            # Test an incompatible Python with ignore_requires_python=True.
            ((3, 6, 4), True, (True, '1.12')),
        ],
    )
    def test_evaluate_link(
        self, py_version_info, ignore_requires_python, expected,
    ):
        target_python = TargetPython(py_version_info=py_version_info)
        evaluator = CandidateEvaluator(
            allow_yanked=True,
            target_python=target_python,
            ignore_requires_python=ignore_requires_python,
        )
        link = Link(
            'https://example.com/#egg=twine-1.12',
            requires_python='== 3.6.5',
        )
        search = Search(
            supplied='twine', canonical='twine', formats=['source'],
        )
        actual = evaluator.evaluate_link(link, search=search)
        assert actual == expected

    @pytest.mark.parametrize('yanked_reason, allow_yanked, expected', [
        (None, True, (True, '1.12')),
        (None, False, (True, '1.12')),
        ('', True, (True, '1.12')),
        ('', False, (False, 'yanked for reason: <none given>')),
        ('bad metadata', True, (True, '1.12')),
        ('bad metadata', False,
         (False, 'yanked for reason: bad metadata')),
        # Test a unicode string with a non-ascii character.
        (u'curly quote: \u2018', True, (True, '1.12')),
        (u'curly quote: \u2018', False,
         (False, u'yanked for reason: curly quote: \u2018')),
    ])
    def test_evaluate_link__allow_yanked(
        self, yanked_reason, allow_yanked, expected,
    ):
        evaluator = CandidateEvaluator(allow_yanked=allow_yanked)
        link = Link(
            'https://example.com/#egg=twine-1.12',
            yanked_reason=yanked_reason,
        )
        search = Search(
            supplied='twine', canonical='twine', formats=['source'],
        )
        actual = evaluator.evaluate_link(link, search=search)
        assert actual == expected

    def test_evaluate_link__incompatible_wheel(self):
        """
        Test an incompatible wheel.
        """
        target_python = TargetPython(py_version_info=(3, 6, 4))
        # Set the valid tags to an empty list to make sure nothing matches.
        target_python._valid_tags = []
        evaluator = CandidateEvaluator(
            allow_yanked=True,
            target_python=target_python,
        )
        link = Link('https://example.com/sample-1.0-py2.py3-none-any.whl')
        search = Search(
            supplied='sample', canonical='sample', formats=['binary'],
        )
        actual = evaluator.evaluate_link(link, search=search)
        expected = (
            False, "none of the wheel's tags match: py2-none-any, py3-none-any"
        )
        assert actual == expected

    @pytest.mark.parametrize('yanked_reason, expected', [
        # Test a non-yanked file.
        (None, 0),
        # Test a yanked file (has a lower value than non-yanked).
        ('bad metadata', -1),
    ])
    def test_sort_key__is_yanked(self, yanked_reason, expected):
        """
        Test the effect of is_yanked on _sort_key()'s return value.
        """
        url = 'https://example.com/mypackage.tar.gz'
        link = Link(url, yanked_reason=yanked_reason)
        candidate = InstallationCandidate('mypackage', '1.0', link)

        evaluator = CandidateEvaluator(allow_yanked=True)
        sort_value = evaluator._sort_key(candidate)
        # Yanked / non-yanked is reflected in the first element of the tuple.
        actual = sort_value[0]
        assert actual == expected

    def make_mock_candidate(self, version, yanked_reason=None):
        url = 'https://example.com/pkg-{}.tar.gz'.format(version)
        link = Link(url, yanked_reason=yanked_reason)
        candidate = InstallationCandidate('mypackage', version, link)

        return candidate

    def test_get_best_candidate__no_candidates(self):
        """
        Test passing an empty list.
        """
        evaluator = CandidateEvaluator(allow_yanked=True)
        actual = evaluator.get_best_candidate([])
        assert actual is None

    def test_get_best_candidate__all_yanked(self, caplog):
        """
        Test all candidates yanked.
        """
        candidates = [
            self.make_mock_candidate('1.0', yanked_reason='bad metadata #1'),
            # Put the best candidate in the middle, to test sorting.
            self.make_mock_candidate('3.0', yanked_reason='bad metadata #3'),
            self.make_mock_candidate('2.0', yanked_reason='bad metadata #2'),
        ]
        expected_best = candidates[1]
        evaluator = CandidateEvaluator(allow_yanked=True)
        actual = evaluator.get_best_candidate(candidates)
        assert actual is expected_best
        assert str(actual.version) == '3.0'

        # Check the log messages.
        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.levelname == 'WARNING'
        assert record.message == (
            'The candidate selected for download or install is a yanked '
            "version: 'mypackage' candidate "
            '(version 3.0 at https://example.com/pkg-3.0.tar.gz)\n'
            'Reason for being yanked: bad metadata #3'
        )

    @pytest.mark.parametrize('yanked_reason, expected_reason', [
        # Test no reason given.
        ('', '<none given>'),
        # Test a unicode string with a non-ascii character.
        (u'curly quote: \u2018', u'curly quote: \u2018'),
    ])
    def test_get_best_candidate__yanked_reason(
        self, caplog, yanked_reason, expected_reason,
    ):
        """
        Test the log message with various reason strings.
        """
        candidates = [
            self.make_mock_candidate('1.0', yanked_reason=yanked_reason),
        ]
        evaluator = CandidateEvaluator(allow_yanked=True)
        actual = evaluator.get_best_candidate(candidates)
        assert str(actual.version) == '1.0'

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.levelname == 'WARNING'
        expected_message = (
            'The candidate selected for download or install is a yanked '
            "version: 'mypackage' candidate "
            '(version 1.0 at https://example.com/pkg-1.0.tar.gz)\n'
            'Reason for being yanked: '
        ) + expected_reason
        assert record.message == expected_message

    def test_get_best_candidate__best_yanked_but_not_all(self, caplog):
        """
        Test the best candidates being yanked, but not all.
        """
        candidates = [
            self.make_mock_candidate('4.0', yanked_reason='bad metadata #4'),
            # Put the best candidate in the middle, to test sorting.
            self.make_mock_candidate('2.0'),
            self.make_mock_candidate('3.0', yanked_reason='bad metadata #3'),
            self.make_mock_candidate('1.0'),
        ]
        expected_best = candidates[1]
        evaluator = CandidateEvaluator(allow_yanked=True)
        actual = evaluator.get_best_candidate(candidates)
        assert actual is expected_best
        assert str(actual.version) == '2.0'

        # Check the log messages.
        assert len(caplog.records) == 0


class TestPackageFinder:

    @pytest.mark.parametrize('allow_yanked', [False, True])
    def test_create__allow_yanked(self, allow_yanked):
        """
        Test that allow_yanked is passed to CandidateEvaluator.
        """
        search_scope = SearchScope([], [])
        finder = PackageFinder.create(
            search_scope=search_scope,
            allow_yanked=allow_yanked,
            session=object(),
        )
        evaluator = finder.candidate_evaluator
        assert evaluator._allow_yanked == allow_yanked

    def test_create__target_python(self):
        """
        Test that target_python is passed to CandidateEvaluator as is.
        """
        search_scope = SearchScope([], [])
        target_python = TargetPython(py_version_info=(3, 7, 3))
        finder = PackageFinder.create(
            search_scope=search_scope,
            allow_yanked=True,
            session=object(),
            target_python=target_python,
        )
        evaluator = finder.candidate_evaluator
        actual_target_python = evaluator._target_python
        assert actual_target_python is target_python
        assert actual_target_python.py_version_info == (3, 7, 3)

    def test_add_trusted_host(self):
        # Leave a gap to test how the ordering is affected.
        trusted_hosts = ['host1', 'host3']
        session = PipSession(insecure_hosts=trusted_hosts)
        finder = make_test_finder(
            session=session,
            trusted_hosts=trusted_hosts,
        )
        insecure_adapter = session._insecure_adapter
        prefix2 = 'https://host2/'
        prefix3 = 'https://host3/'

        # Confirm some initial conditions as a baseline.
        assert finder.trusted_hosts == ['host1', 'host3']
        assert session.adapters[prefix3] is insecure_adapter
        assert prefix2 not in session.adapters

        # Test adding a new host.
        finder.add_trusted_host('host2')
        assert finder.trusted_hosts == ['host1', 'host3', 'host2']
        # Check that prefix3 is still present.
        assert session.adapters[prefix3] is insecure_adapter
        assert session.adapters[prefix2] is insecure_adapter

        # Test that adding the same host doesn't create a duplicate.
        finder.add_trusted_host('host3')
        assert finder.trusted_hosts == ['host1', 'host3', 'host2'], (
            'actual: {}'.format(finder.trusted_hosts)
        )

    def test_add_trusted_host__logging(self, caplog):
        """
        Test logging when add_trusted_host() is called.
        """
        trusted_hosts = ['host1']
        session = PipSession(insecure_hosts=trusted_hosts)
        finder = make_test_finder(
            session=session,
            trusted_hosts=trusted_hosts,
        )
        with caplog.at_level(logging.INFO):
            # Test adding an existing host.
            finder.add_trusted_host('host1', source='somewhere')
            finder.add_trusted_host('host2')
            # Test calling add_trusted_host() on the same host twice.
            finder.add_trusted_host('host2')

        actual = [(r.levelname, r.message) for r in caplog.records]
        expected = [
            ('INFO', "adding trusted host: 'host1' (from somewhere)"),
            ('INFO', "adding trusted host: 'host2'"),
            ('INFO', "adding trusted host: 'host2'"),
        ]
        assert actual == expected

    def test_iter_secure_origins(self):
        trusted_hosts = ['host1', 'host2']
        finder = make_test_finder(trusted_hosts=trusted_hosts)

        actual = list(finder.iter_secure_origins())
        assert len(actual) == 8
        # Spot-check that SECURE_ORIGINS is included.
        assert actual[0] == ('https', '*', '*')
        assert actual[-2:] == [
            ('*', 'host1', '*'),
            ('*', 'host2', '*'),
        ]

    def test_iter_secure_origins__none_trusted_hosts(self):
        """
        Test iter_secure_origins() after passing trusted_hosts=None.
        """
        # Use PackageFinder.create() rather than make_test_finder()
        # to make sure we're really passing trusted_hosts=None.
        search_scope = SearchScope([], [])
        finder = PackageFinder.create(
            search_scope=search_scope,
            allow_yanked=True,
            trusted_hosts=None,
            session=object(),
        )

        actual = list(finder.iter_secure_origins())
        assert len(actual) == 6
        # Spot-check that SECURE_ORIGINS is included.
        assert actual[0] == ('https', '*', '*')


def test_sort_locations_file_expand_dir(data):
    """
    Test that a file:// dir gets listdir run with expand_dir
    """
    finder = make_test_finder(find_links=[data.find_links])
    files, urls = finder._sort_locations([data.find_links], expand_dir=True)
    assert files and not urls, (
        "files and not urls should have been found at find-links url: %s" %
        data.find_links
    )


def test_sort_locations_file_not_find_link(data):
    """
    Test that a file:// url dir that's not a find-link, doesn't get a listdir
    run
    """
    finder = make_test_finder()
    files, urls = finder._sort_locations([data.index_url("empty_with_pkg")])
    assert urls and not files, "urls, but not files should have been found"


def test_sort_locations_non_existing_path():
    """
    Test that a non-existing path is ignored.
    """
    finder = make_test_finder()
    files, urls = finder._sort_locations(
        [os.path.join('this', 'doesnt', 'exist')])
    assert not urls and not files, "nothing should have been found"


@pytest.mark.parametrize(
    ("html", "url", "expected"),
    [
        (b"<html></html>", "https://example.com/", "https://example.com/"),
        (
            b"<html><head>"
            b"<base href=\"https://foo.example.com/\">"
            b"</head></html>",
            "https://example.com/",
            "https://foo.example.com/",
        ),
        (
            b"<html><head>"
            b"<base><base href=\"https://foo.example.com/\">"
            b"</head></html>",
            "https://example.com/",
            "https://foo.example.com/",
        ),
    ],
)
def test_determine_base_url(html, url, expected):
    document = html5lib.parse(
        html, transport_encoding=None, namespaceHTMLElements=False,
    )
    assert _determine_base_url(document, url) == expected


class MockLogger(object):
    def __init__(self):
        self.called = False

    def warning(self, *args, **kwargs):
        self.called = True


@pytest.mark.parametrize(
    ("location", "trusted", "expected"),
    [
        ("http://pypi.org/something", [], True),
        ("https://pypi.org/something", [], False),
        ("git+http://pypi.org/something", [], True),
        ("git+https://pypi.org/something", [], False),
        ("git+ssh://git@pypi.org/something", [], False),
        ("http://localhost", [], False),
        ("http://127.0.0.1", [], False),
        ("http://example.com/something/", [], True),
        ("http://example.com/something/", ["example.com"], False),
        ("http://eXample.com/something/", ["example.cOm"], False),
    ],
)
def test_secure_origin(location, trusted, expected):
    finder = make_test_finder(trusted_hosts=trusted)
    logger = MockLogger()
    finder._validate_secure_origin(logger, location)
    assert logger.called == expected


@pytest.mark.parametrize(
    ("egg_info", "canonical_name", "expected"),
    [
        # Trivial.
        ("pip-18.0", "pip", 3),
        ("zope-interface-4.5.0", "zope-interface", 14),

        # Canonicalized name match non-canonicalized egg info. (pypa/pip#5870)
        ("Jinja2-2.10", "jinja2", 6),
        ("zope.interface-4.5.0", "zope-interface", 14),
        ("zope_interface-4.5.0", "zope-interface", 14),

        # Should be smart enough to parse ambiguous names from the provided
        # package name.
        ("foo-2-2", "foo", 3),
        ("foo-2-2", "foo-2", 5),

        # Should be able to detect collapsed characters in the egg info.
        ("foo--bar-1.0", "foo-bar", 8),
        ("foo-_bar-1.0", "foo-bar", 8),

        # The package name must not ends with a dash (PEP 508), so the first
        # dash would be the separator, not the second.
        ("zope.interface--4.5.0", "zope-interface", 14),
        ("zope.interface--", "zope-interface", 14),

        # The version part is missing, but the split function does not care.
        ("zope.interface-", "zope-interface", 14),
    ],
)
def test_find_name_version_sep(egg_info, canonical_name, expected):
    index = _find_name_version_sep(egg_info, canonical_name)
    assert index == expected


@pytest.mark.parametrize(
    ("egg_info", "canonical_name"),
    [
        # A dash must follow the package name.
        ("zope.interface4.5.0", "zope-interface"),
        ("zope.interface.4.5.0", "zope-interface"),
        ("zope.interface.-4.5.0", "zope-interface"),
        ("zope.interface", "zope-interface"),
    ],
)
def test_find_name_version_sep_failure(egg_info, canonical_name):
    with pytest.raises(ValueError) as ctx:
        _find_name_version_sep(egg_info, canonical_name)
    message = "{} does not match {}".format(egg_info, canonical_name)
    assert str(ctx.value) == message


@pytest.mark.parametrize(
    ("egg_info", "canonical_name", "expected"),
    [
        # Trivial.
        ("pip-18.0", "pip", "18.0"),
        ("zope-interface-4.5.0", "zope-interface", "4.5.0"),

        # Canonicalized name match non-canonicalized egg info. (pypa/pip#5870)
        ("Jinja2-2.10", "jinja2", "2.10"),
        ("zope.interface-4.5.0", "zope-interface", "4.5.0"),
        ("zope_interface-4.5.0", "zope-interface", "4.5.0"),

        # Should be smart enough to parse ambiguous names from the provided
        # package name.
        ("foo-2-2", "foo", "2-2"),
        ("foo-2-2", "foo-2", "2"),
        ("zope.interface--4.5.0", "zope-interface", "-4.5.0"),
        ("zope.interface--", "zope-interface", "-"),

        # Should be able to detect collapsed characters in the egg info.
        ("foo--bar-1.0", "foo-bar", "1.0"),
        ("foo-_bar-1.0", "foo-bar", "1.0"),

        # Invalid.
        ("the-package-name-8.19", "does-not-match", None),
        ("zope.interface.-4.5.0", "zope.interface", None),
        ("zope.interface-", "zope-interface", None),
        ("zope.interface4.5.0", "zope-interface", None),
        ("zope.interface.4.5.0", "zope-interface", None),
        ("zope.interface.-4.5.0", "zope-interface", None),
        ("zope.interface", "zope-interface", None),
    ],
)
def test_egg_info_matches(egg_info, canonical_name, expected):
    version = _egg_info_matches(egg_info, canonical_name)
    assert version == expected


def test_request_http_error(caplog):
    caplog.set_level(logging.DEBUG)
    link = Link('http://localhost')
    session = Mock(PipSession)
    session.get.return_value = resp = Mock()
    resp.raise_for_status.side_effect = requests.HTTPError('Http error')
    assert _get_html_page(link, session=session) is None
    assert (
        'Could not fetch URL http://localhost: Http error - skipping'
        in caplog.text
    )


def test_request_retries(caplog):
    caplog.set_level(logging.DEBUG)
    link = Link('http://localhost')
    session = Mock(PipSession)
    session.get.side_effect = requests.exceptions.RetryError('Retry error')
    assert _get_html_page(link, session=session) is None
    assert (
        'Could not fetch URL http://localhost: Retry error - skipping'
        in caplog.text
    )


@pytest.mark.parametrize(
    ("url", "clean_url"),
    [
        # URL with hostname and port. Port separator should not be quoted.
        ("https://localhost.localdomain:8181/path/with space/",
         "https://localhost.localdomain:8181/path/with%20space/"),
        # URL that is already properly quoted. The quoting `%`
        # characters should not be quoted again.
        ("https://localhost.localdomain:8181/path/with%20quoted%20space/",
         "https://localhost.localdomain:8181/path/with%20quoted%20space/"),
        # URL with IPv4 address and port.
        ("https://127.0.0.1:8181/path/with space/",
         "https://127.0.0.1:8181/path/with%20space/"),
        # URL with IPv6 address and port. The `[]` brackets around the
        # IPv6 address should not be quoted.
        ("https://[fd00:0:0:236::100]:8181/path/with space/",
         "https://[fd00:0:0:236::100]:8181/path/with%20space/"),
        # URL with query. The leading `?` should not be quoted.
        ("https://localhost.localdomain:8181/path/with/query?request=test",
         "https://localhost.localdomain:8181/path/with/query?request=test"),
        # URL with colon in the path portion.
        ("https://localhost.localdomain:8181/path:/with:/colon",
         "https://localhost.localdomain:8181/path%3A/with%3A/colon"),
        # URL with something that looks like a drive letter, but is
        # not. The `:` should be quoted.
        ("https://localhost.localdomain/T:/path/",
         "https://localhost.localdomain/T%3A/path/"),
        # VCS URL containing revision string.
        ("git+ssh://example.com/path to/repo.git@1.0#egg=my-package-1.0",
         "git+ssh://example.com/path%20to/repo.git@1.0#egg=my-package-1.0")
    ]
)
def test_clean_link(url, clean_url):
    assert(_clean_link(url) == clean_url)


@pytest.mark.parametrize(
    ("url", "clean_url"),
    [
        # URL with Windows drive letter. The `:` after the drive
        # letter should not be quoted. The trailing `/` should be
        # removed.
        ("file:///T:/path/with spaces/",
         "file:///T:/path/with%20spaces")
    ]
)
@pytest.mark.skipif("sys.platform != 'win32'")
def test_clean_link_windows(url, clean_url):
    assert(_clean_link(url) == clean_url)


@pytest.mark.parametrize(
    ("url", "clean_url"),
    [
        # URL with Windows drive letter, running on non-windows
        # platform. The `:` after the drive should be quoted.
        ("file:///T:/path/with spaces/",
         "file:///T%3A/path/with%20spaces/")
    ]
)
@pytest.mark.skipif("sys.platform == 'win32'")
def test_clean_link_non_windows(url, clean_url):
    assert(_clean_link(url) == clean_url)


class TestHTMLPage:

    @pytest.mark.parametrize(
        ('anchor_html, expected'),
        [
            # Test not present.
            ('<a href="/pkg1-1.0.tar.gz"></a>', None),
            # Test present with no value.
            ('<a href="/pkg2-1.0.tar.gz" data-yanked></a>', ''),
            # Test the empty string.
            ('<a href="/pkg3-1.0.tar.gz" data-yanked=""></a>', ''),
            # Test a non-empty string.
            ('<a href="/pkg4-1.0.tar.gz" data-yanked="error"></a>', 'error'),
            # Test a value with an escaped character.
            ('<a href="/pkg4-1.0.tar.gz" data-yanked="version &lt 1"></a>',
                'version < 1'),
            # Test a yanked reason with a non-ascii character.
            (u'<a href="/pkg-1.0.tar.gz" data-yanked="curlyquote \u2018"></a>',
                u'curlyquote \u2018'),
        ]
    )
    def test_iter_links__yanked_reason(self, anchor_html, expected):
        html = (
            # Mark this as a unicode string for Python 2 since anchor_html
            # can contain non-ascii.
            u'<html><head><meta charset="utf-8"><head>'
            '<body>{}</body></html>'
        ).format(anchor_html)
        html_bytes = html.encode('utf-8')
        page = HTMLPage(html_bytes, url='https://example.com/simple/')
        links = list(page.iter_links())
        link, = links
        actual = link.yanked_reason
        assert actual == expected
