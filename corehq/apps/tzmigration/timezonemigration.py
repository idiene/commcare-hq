import collections
from copy import deepcopy
import os

from couchdbkit import ResourceNotFound
from django.conf import settings

from casexml.apps.case.xform import get_case_updates
from dimagi.utils.couch.database import iter_docs
from corehq.apps.tzmigration import set_migration_started, \
    set_migration_complete, get_migration_status
from corehq.apps.tzmigration.planning import PlanningDB
from couchforms.dbaccessors import get_form_ids_by_type
from couchforms import convert_xform_to_json
from couchforms.models import XFormInstance
from couchforms.util import adjust_datetimes, scrub_meta
from corehq.apps.tzmigration import force_phone_timezones_should_be_processed


def run_timezone_migration_for_domain(domain):
    print get_migration_status(domain, strict=True)
    set_migration_started(domain)
    _run_timezone_migration_for_domain(domain)
    set_migration_complete(domain)


FormJsonDiff = collections.namedtuple('FormJsonDiff', [
    'diff_type', 'path', 'old_value', 'new_value'])


def _json_diff(obj1, obj2, path):
    if isinstance(obj1, str):
        obj1 = unicode(obj1)
    if isinstance(obj2, str):
        obj2 = unicode(obj2)

    if obj1 == obj2:
        return
    elif Ellipsis in (obj1, obj2):
        yield FormJsonDiff('missing', path, obj1, obj2)
    elif type(obj1) != type(obj2):
        yield FormJsonDiff('type',  path, obj1, obj2)
    elif isinstance(obj1, dict):
        keys = set(obj1.keys()) | set(obj2.keys())

        def value_or_ellipsis(obj, key):
            return obj.get(key, Ellipsis)

        for key in keys:
            for result in _json_diff(value_or_ellipsis(obj1, key),
                                     value_or_ellipsis(obj2, key),
                                     path=path + (key,)):
                yield result
    elif isinstance(obj1, list):

        def value_or_ellipsis(obj, i):
            try:
                return obj[i]
            except IndexError:
                return Ellipsis

        for i in range(max(len(obj1), len(obj2))):
            for result in _json_diff(value_or_ellipsis(obj1, i),
                                     value_or_ellipsis(obj2, i),
                                     path=path + (i,)):
                yield result
    else:
        yield FormJsonDiff('diff', path, obj1, obj2)


def json_diff(obj1, obj2):
    return list(_json_diff(obj1, obj2, path=()))


def _run_timezone_migration_for_domain(domain):
    pass


def _get_submission_xml(xform_id):
    try:
        xml = XFormInstance.get_db().fetch_attachment(xform_id, 'form.xml')
    except ResourceNotFound:
        raise
    else:
        if isinstance(xml, unicode):
            xml = xml.encode('utf-8')
    return xml


def _get_new_form_json(xml, xform_id):
    form_json = convert_xform_to_json(xml)
    with force_phone_timezones_should_be_processed():
        adjust_datetimes(form_json)
    # this is actually in-place because of how jsonobject works
    scrub_meta(XFormInstance.wrap({'form': form_json, '_id': xform_id}))
    return form_json


def get_planning_db_filepath(domain):
    return os.path.join(settings.SHARED_DRIVE_CONF.restore_dir,
                        '{}-tzmigration.db'.format(domain))


def get_planning_db(domain):
    db_filepath = get_planning_db_filepath(domain)
    return PlanningDB.open(db_filepath)


def delete_planning_db(domain):
    db_filepath = get_planning_db_filepath(domain)
    os.remove(db_filepath)


def prepare_planning_db(domain):
    db_filepath = get_planning_db_filepath(domain)
    planning_db = PlanningDB.init(db_filepath)
    xform_ids = get_form_ids_by_type(domain, 'XFormInstance')

    for i, xform in enumerate(iter_docs(XFormInstance.get_db(), xform_ids)):
        xform_id = xform['_id']
        case_actions_by_case_id = collections.defaultdict(list)
        xml = _get_submission_xml(xform_id)
        form_json = _get_new_form_json(xml, xform_id)
        planning_db.add_form(xform_id, form_json)
        planning_db.add_form_diffs(xform_id,
                                   json_diff(xform['form'], form_json))

        case_updates = get_case_updates(form_json)
        xform_copy = deepcopy(xform)
        xform_copy['form'] = form_json
        xformdoc = XFormInstance.wrap(xform_copy)

        case_actions = [
            (case_update.id, action.xform_id, action.to_json())
            for case_update in case_updates
            for action in case_update.get_case_actions(xformdoc)
        ]

        for case_id, xform_id, case_action in case_actions:
            case_actions_by_case_id[case_id].append((xform_id, case_action))

        for case_id, case_actions in case_actions_by_case_id.items():
            planning_db.ensure_case(case_id)
            planning_db.add_case_actions(case_id, case_actions)
