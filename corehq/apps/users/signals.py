from __future__ import absolute_import
import logging
from datetime import datetime
from django.db.models.signals import post_save
from django.conf import settings
from django.contrib.auth.models import SiteProfileNotAvailable, User
from djangocouchuser.signals import couch_user_post_save
from couchforms.models import XFormInstance
from corehq.apps.receiver.signals import post_received, ReceiverResult
from corehq.apps.users.models import HqUserProfile, CouchUser, COUCH_USER_AUTOCREATED_STATUS,\
    create_hq_user_from_commcare_registration_info
from corehq.util.xforms import get_unique_value
from corehq.apps.users.util import format_username
from dimagi.utils.logging import log_exception

# xmlns that registrations and backups come in as, respectively. 
REGISTRATION_XMLNS = "http://openrosa.org/user-registration"

"""
Case 1: 
This section automatically creates Couch users whenever a web user is created
"""
def create_user_from_django_user(sender, instance, created, **kwargs): 
    """
    The user post save signal, to auto-create our Profile
    """
    if not created:
        try:
            instance.get_profile().save()
            return
        except HqUserProfile.DoesNotExist:
            logging.warn("There should have been a profile for "
                         "%s but wasn't.  Creating one now." % instance)
        except SiteProfileNotAvailable:
            raise
    
    if hasattr(instance, 'is_commcare_user'):
        profile, created = HqUserProfile.objects.get_or_create(user=instance, is_commcare_user=instance.is_commcare_user)
    else:
        profile, created = HqUserProfile.objects.get_or_create(user=instance)

    if not created:
        # magically calls our other save signal
        profile.save()
        
        """ 
        TODO: remove this, we must explicitly create the 
        couch user when we want to
        # save updated django model data to couch model
        couch_user = profile.get_couch_user()
        for i in couch_user.django_user:
            couch_user.django_user[i] = getattr(instance, i)
        couch_user.save()
        """
        
post_save.connect(create_user_from_django_user, User)        
post_save.connect(couch_user_post_save, HqUserProfile)


"""
Case 2: 
This section automatically creates Couch users whenever a registration xform is received

Question: is it possible to receive registration data from the phone after Case 3?
If so, we need to check for a user created via Case 3 and link them to this account
automatically
"""
def create_user_from_commcare_registration(sender, xform, **kwargs):
    """
    # this comes in as xml that looks like:
    # <n0:registration xmlns:n0="openrosa.org/user-registration">
    # <username>user</username>
    # <password>pw</password>
    # <uuid>MTBZJTDO3SCT2ONXAQ88WM0CH</uuid>
    # <date>2008-01-07</date>
    # <registering_phone_id>NRPHIOUSVEA215AJL8FFKGTVR</registering_phone_id>
    # <user_data> ... some custom stuff </user_data>
    """
    try:
        if xform.xmlns != REGISTRATION_XMLNS:
            return False
        if not ('username' in xform.form and 
                'password' in xform.form and 
                'uuid' in xform.form and 
                'date' in xform.form and 
                'registering_phone_id' in xform.form):
                    raise Exception("Poorly configured registration XML")
        username = xform.form['username']
        password = xform.form['password']
        uuid = xform.form['uuid']
        date = xform.form['date']
        imei = xform.form['registering_phone_id']
        # TODO: implement this properly, more like xml_to_json(user_data)
        domain = xform.domain
        # we need to check for username conflicts, other issues
        # and make sure we send the appropriate conflict response to the
        # phone.
        username = format_username(username, domain)
        print username
        try: 
            User.objects.get(username=username)
            prefix, suffix = username.split("@") 
            username = get_unique_value(User.objects, "username", prefix, sep="", suffix="@%s" % suffix)
            print username
        except User.DoesNotExist:
            # they didn't exist, so we can use this username
            pass
        
        couch_user = create_hq_user_from_commcare_registration_info(domain, username, password, uuid, imei, date)
        print couch_user
        print couch_user.get_id
        return ReceiverResult("I'm rick james bitch!")
    except Exception, e:
        import traceback, sys
        #exc_type, exc_value, exc_traceback = sys.exc_info()
        #traceback.print_tb(exc_traceback, limit=1, file=sys.stdout)
        logging.exception(e)
        raise

post_received.connect(create_user_from_commcare_registration)

