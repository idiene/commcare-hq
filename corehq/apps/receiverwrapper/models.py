from collections import defaultdict, namedtuple
from datetime import datetime, timedelta
import json
import urllib
import urlparse

from couchdbkit.ext.django.schema import *
from couchdbkit.exceptions import ResourceNotFound
from django.core.cache import cache
from django.conf import settings
import socket
import hashlib

from casexml.apps.case.models import CommCareCase
from casexml.apps.case.xml import V2, LEGAL_VERSIONS

from couchforms.models import XFormInstance
from dimagi.utils.decorators.memoized import memoized
from dimagi.utils.modules import to_function
from dimagi.utils.parsing import json_format_datetime
from dimagi.utils.mixins import UnicodeMixIn
from dimagi.utils.post import simple_post
from dimagi.utils.couch import LockableMixIn


repeater_types = {}


def register_repeater_type(cls):
    repeater_types[cls.__name__] = cls
    return cls


def simple_post_with_cached_timeout(data, url, expiry=60 * 60, *args, **kwargs):
    # no control characters (e.g. '/') in keys
    key = hashlib.md5(
        '{0} timeout {1}'.format(__name__, url)
    ).hexdigest()

    cache_value = cache.get(key)

    if cache_value == 'timeout':
        raise socket.timeout('recently timed out, not retrying')
    elif cache_value == 'error':
        raise socket.timeout('recently errored, not retrying')

    try:
        resp = simple_post(data, url, *args, **kwargs)
    except socket.timeout:
        cache.set(key, 'timeout', expiry)
        raise

    if not 200 <= resp.status < 300:
        cache.set(key, 'error', expiry)
    return resp


DELETED = "-Deleted"

FormatInfo = namedtuple('FormatInfo', 'name label generator_class')


class GeneratorCollection():

    def __init__(self, repeater_class):
        self.repeater_class = repeater_class
        self.default_format = ''
        self.format_generator_map = defaultdict(dict)

    def add_new_format(self, format_name, format_label, generator_class, is_default=False):
        if is_default and self.default_format:
            raise Exception("default format already exists for this repeater")
        elif is_default:
            self.default_format = format_name
        self.format_generator_map[format_name] = FormatInfo(
            name=format_name,
            label=format_label,
            generator_class=generator_class
        )

    def get_default_format(self):
        return self.default_format

    def get_default_generator(self):
        raise self.format_generator_map[self.default_format].generator_class

    def get_all_formats(self, for_domain=None):
        return [(name, format.label) for name, format in self.format_generator_map.iteritems()
                if not for_domain or format.generator_class.enabled_for_domain(for_domain)]

    def get_generator_by_format(self, format):
        return self.format_generator_map[format].generator_class


class RegisterGenerator(object):

    generators = {}

    def __init__(self, repeater_cls, format_name, format_label, is_default=False):
        self.format_name = format_name
        self.format_label = format_label
        self.repeater_cls = repeater_cls
        self.label = format_label
        self.is_default = is_default

    def __call__(self, generator_class):
        RegisterGenerator.generators[self.repeater_cls] = GeneratorCollection(self.repeater_cls)
        RegisterGenerator.generators[self.repeater_cls].add_new_format(
            self.format_name,
            self.format_label,
            generator_class,
            is_default=self.is_default
        )

    @classmethod
    def generator_class_by_repeater_format(cls, repeater_class, format_name):
        generator_collection = cls.generators[repeater_class]
        return generator_collection.get_generator_by_format(format_name)

    @classmethod
    def all_formats_by_repeater(cls, repeater_class, for_domain=None):
        generator_collection = cls.generators[repeater_class]
        return generator_collection.get_all_formats(for_domain=None)

    @classmethod
    def default_format_by_repeater(cls, repeater_class):
        generator_collection = cls.generators[repeater_class]
        return generator_collection.get_default_format()


class Repeater(Document, UnicodeMixIn):
    base_doc = 'Repeater'

    domain = StringProperty()
    url = StringProperty()
    format = StringProperty()

    def format_or_default_format(self):
        return self.format or RegisterGenerator.default_format_by_repeater(self.__class__)

    def get_payload_generator(self, payload_format):
        gen = RegisterGenerator.generator_class_by_repeater_format(self.__class__, payload_format)
        return gen(self)

    def get_payload(self, repeat_record):
        generator = self.get_payload_generator(self.format_or_default_format())
        return generator.get_payload(repeat_record)

    def register(self, payload, next_check=None):
        try:
            payload_id = payload.get_id
        except Exception:
            payload_id = payload
        repeat_record = RepeatRecord(
            repeater_id=self.get_id,
            repeater_type=self.doc_type,
            domain=self.domain,
            next_check=next_check or datetime.utcnow(),
            payload_id=payload_id
        )
        repeat_record.save()
        return repeat_record

    @classmethod
    def by_domain(cls, domain):
        key = [domain]
        if repeater_types.has_key(cls.__name__):
            key.append(cls.__name__)
        elif cls.__name__ == Repeater.__name__:
            # In this case the wrap function delegates to the
            # appropriate sub-repeater types.
            pass
        else:
            # Any repeater type can be posted to the API, and the installed apps
            # determine whether we actually know about it.
            # But if we do not know about it, then may as well return nothing now
            return []

        raw_docs = cls.view('receiverwrapper/repeaters',
            startkey=key,
            endkey=key + [{}],
            include_docs=True,
            reduce=False,
            wrap_doc=False
        )

        return [cls.wrap(repeater_doc['doc']) for repeater_doc in raw_docs
                if repeater_doc['doc']['doc_type'].replace(DELETED, '') in repeater_types]

    @classmethod
    def wrap(cls, data):
        doc_type = data['doc_type'].replace(DELETED, '')
        if cls.__name__ == Repeater.__name__:
            if doc_type in repeater_types:
                return repeater_types[doc_type].wrap(data)
            else:
                raise ResourceNotFound('Unknown repeater type: %s', data)
        else:
            return super(Repeater, cls).wrap(data)

    def retire(self):
        if DELETED not in self['doc_type']:
            self['doc_type'] += DELETED
        if DELETED not in self['base_doc']:
            self['base_doc'] += DELETED
        self.save()

    def get_url(self, repeate_record):
        # to be overridden
        return self.url

    def get_headers(self, repeat_record):
        # to be overridden
        return {}

@register_repeater_type
class FormRepeater(Repeater):
    """
    Record that forms should be repeated to a new url

    """

    exclude_device_reports = BooleanProperty(default=False)

    @memoized
    def _payload_doc(self, repeat_record):
        return XFormInstance.get(repeat_record.payload_id)

    def get_url(self, repeat_record):
        url = super(FormRepeater, self).get_url(repeat_record)
        # adapted from http://stackoverflow.com/a/2506477/10840
        url_parts = list(urlparse.urlparse(url))
        query = urlparse.parse_qsl(url_parts[4])
        query.append(("app_id", self._payload_doc(repeat_record).app_id))
        url_parts[4] = urllib.urlencode(query)
        return urlparse.urlunparse(url_parts)

    def get_headers(self, repeat_record):
        return {
            "received-on": self._payload_doc(repeat_record).received_on.isoformat()+"Z"
        }

    def __unicode__(self):
        return "forwarding forms to: %s" % self.url


@register_repeater_type
class CaseRepeater(Repeater):
    """
    Record that cases should be repeated to a new url

    """

    version = StringProperty(default=V2, choices=LEGAL_VERSIONS)

    @memoized
    def _payload_doc(self, repeat_record):
        return CommCareCase.get(repeat_record.payload_id)

    def get_headers(self, repeat_record):
        return {
            "server-modified-on": self._payload_doc(repeat_record).server_modified_on.isoformat()+"Z"
        }

    def __unicode__(self):
        return "forwarding cases to: %s" % self.url


@register_repeater_type
class ShortFormRepeater(Repeater):
    """
    Record that form id & case ids should be repeated to a new url

    """

    version = StringProperty(default=V2, choices=LEGAL_VERSIONS)

    @memoized
    def _payload_doc(self, repeat_record):
        return XFormInstance.get(repeat_record.payload_id)

    def get_headers(self, repeat_record):
        return {
            "received-on": self._payload_doc(repeat_record).received_on.isoformat()+"Z"
        }

    def __unicode__(self):
        return "forwarding short form to: %s" % self.url


@register_repeater_type
class AppStructureRepeater(Repeater):
    def get_payload(self, repeat_record):
        generator = self.get_payload_generator(self.format_or_default_format())
        return generator.get_payload(repeat_record, repeater=self)


class RepeatRecord(Document, LockableMixIn):
    """
    An record of a particular instance of something that needs to be forwarded
    with a link to the proper repeater object
    """

    repeater_id = StringProperty()
    repeater_type = StringProperty()
    domain = StringProperty()

    last_checked = DateTimeProperty(exact=True)
    next_check = DateTimeProperty(exact=True)
    succeeded = BooleanProperty(default=False)

    payload_id = StringProperty()

    @classmethod
    def wrap(cls, data):
        for attr in ('last_checked', 'next_check'):
            value = data.get(attr)
            if not value:
                continue
            try:
                dt = datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
                data[attr] = dt.isoformat() + '.000000Z'
                print data[attr]
            except ValueError:
                pass
        return super(RepeatRecord, cls).wrap(data)

    @property
    @memoized
    def repeater(self):
        return Repeater.get(self.repeater_id)

    @property
    def url(self):
        return self.repeater.get_url(self)

    @classmethod
    def all(cls, domain=None, due_before=None, limit=None):
        json_now = json_format_datetime(due_before or datetime.utcnow())
        repeat_records = RepeatRecord.view("receiverwrapper/repeat_records_by_next_check",
            startkey=[domain],
            endkey=[domain, json_now, {}],
            include_docs=True,
            reduce=False,
            limit=limit,
        )
        return repeat_records

    @classmethod
    def count(cls, domain=None):
        results = RepeatRecord.view("receiverwrapper/repeat_records_by_next_check",
            startkey=[domain],
            endkey=[domain, {}],
            reduce=True,
        ).one()
        return results['value'] if results else 0

    def update_success(self):
        self.last_checked = datetime.utcnow()
        self.next_check = None
        self.succeeded = True
    
    def update_failure(self):
        # we use an exponential back-off to avoid submitting to bad urls
        # too frequently.
        assert(self.succeeded == False)
        assert(self.next_check is not None)
        now = datetime.utcnow()
        window = timedelta(minutes=0)
        if self.last_checked:
            window = self.next_check - self.last_checked
            window += (window // 2) # window *= 1.5
        if window < timedelta(minutes=60):
            window = timedelta(minutes=60)

        self.last_checked = now
        self.next_check = self.last_checked + window

    def try_now(self):
        # try when we haven't succeeded and either we've
        # never checked, or it's time to check again
        return not self.succeeded

    def get_payload(self):
        return self.repeater.get_payload(self)

    def fire(self, max_tries=3, post_fn=None):
        payload = self.get_payload()
        post_fn = post_fn or simple_post_with_cached_timeout
        headers = self.repeater.get_headers(self)
        if self.try_now():
            # we don't use celery's version of retry because
            # we want to override the success/fail each try
            for i in range(max_tries):
                try:
                    resp = post_fn(payload, self.url, headers=headers)
                    if 200 <= resp.status < 300:
                        self.update_success()
                        break
                except Exception, e:
                    pass # some other connection issue probably
            if not self.succeeded:
                # mark it failed for later and give up
                self.update_failure()

# import signals
from corehq.apps.receiverwrapper import signals
