import copy
from django import template
from django.utils.translation import ugettext as _
from django.utils.safestring import mark_safe
from django.utils.html import escape

import datetime
import pytz
from casexml.apps.case.models import CommCareCase
from django.template.loader import render_to_string
from corehq.apps.reports.templatetags.timezone_tags import utc_to_timezone
from django.template.defaultfilters import yesno

import itertools

def chunks(l, n):
    """Yield successive n-sized chunks from l."""
    for i in xrange(0, len(l), n):
        yield l[i:i+n]

register = template.Library()

DYNAMIC_PROPERTIES_COLUMNS = 4

def build_tables(data, definition, processors=None, timezone=pytz.utc):
    processors = processors or {
        'utc_to_timezone': lambda d: utc_to_timezone(d, timezone=timezone),
        'yesno': yesno
    }

    def get_value(data, expr):
        # todo: nested attributes, support for both getitem and getattr

        return data.get(expr, None)

    def get_display_tuple(prop):
        expr = prop['expr']
        name = prop.get('name', expr)
        format = prop.get('format')
        process = prop.get('process')

        val = get_value(data, expr)

        if not process and isinstance(val, datetime.datetime):
            process = 'utc_to_timezone'

        if process:
            val = processors[process](val)

        val = escape(val)

        if format:
            val = mark_safe(format.format(val))

        return (expr, name, val)

    sections = []

    for section_name, rows in definition:
        processed_rows = [[get_display_tuple(prop) for prop in row]
                          for row in rows]

        columns = list(itertools.izip_longest(*processed_rows))
        sections.append((_(section_name) if section_name else "", columns))

    return sections

@register.simple_tag
def render_tables(tables, collapsible=False):
    return render_to_string("case/partials/property_table.html", {
        "tables": tables
    })

@register.simple_tag
def render_form(form):
    data = form.to_json()

    definition = [
        (None, chunks(
            [{"expr": prop} for prop in data.keys()],
            DYNAMIC_PROPERTIES_COLUMNS)
        )
    ]

    tables = build_tables(data, definition=definition)

    return render_tables(tables)


@register.simple_tag
def render_case(case, timezone=pytz.utc, display=None):
    if isinstance(case, basestring):
        # we were given an ID, fetch the case
        case = CommCareCase.get(case)

    display = display or [
        (_("Basic Data"), [
            [
                {
                    "expr": "name",
                    "name": _("Name"),
                },
                {
                    "expr": "closed",
                    "name": _("Closed?"),
                    "process": "yesno",
                },
            ],
            [
                {
                    "expr": "type",
                    "name": _("Type"),
                    "format": '<code>{0}</code>',
                },
                {
                    "expr": "external_id",
                    "name": _("External ID"),
                },
            ],
            [
                {
                    "expr": "case_id",
                    "name": _("Case ID"),
                },
                {
                    "expr": "domain",
                    "name": _("Domain"),
                },
            ],
        ]),
        (_("Submission Info"), [
            [
                {
                    "expr": "opened_on",
                    "name": _("Opened On"),
                },
                {
                    "expr": "user_id",
                    "name": _("User ID"),
                    "format": '<span data-field="user_id">{0}</span>',
                },
            ],
            [
                {
                    "expr": "modified_on",
                    "name": _("Modified On"),
                },
                {
                    "expr": "owner_id",
                    "name": _("Owner ID"),
                    "format": '<span data-field="owner_id">{0}</span>',
                },
            ],
            [
                {
                    "expr": "closed_on",
                    "name": _("Closed On"),
                },
            ],
        ])
    ]

    data = copy.deepcopy(case.to_json())
    default_properties = build_tables(
            data, definition=display, timezone=timezone)

    dynamic_data = dict((k, v) for (k, v) in case.dynamic_case_properties()
                        if k in data)
    definition = [
        (None, chunks(
            [{"expr": prop} for prop in dynamic_data.keys()],
            DYNAMIC_PROPERTIES_COLUMNS)
        )
    ]

    dynamic_properties = build_tables(
            dynamic_data, definition=definition, timezone=timezone)

    return render_to_string("case/partials/single_case.html", {
        "default_properties": default_properties,
        "dynamic_properties": dynamic_properties,
        "case": case, 
        "timezone": timezone
    })
    
    
@register.simple_tag
def case_inline_display(case):
    """
    Given a case id, make a best effort at displaying it.
    """
    if case:
        if case.opened_on:
            return "%s (opened: %s)" % (case.name, case.opened_on.date())
        else:
            return case.name
    return "empty case" 
    
