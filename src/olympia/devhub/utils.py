import uuid

from django.conf import settings
from django.db.models import Q
from django.forms import ValidationError
from django.utils.translation import ugettext

from celery import chain
from validator.version import Version as VersionString

import olympia.core.logger
from olympia import amo
from olympia.amo.urlresolvers import linkify_escape
from olympia.files.models import File, FileUpload
from olympia.files.utils import parse_addon

from . import tasks

log = olympia.core.logger.getLogger('z.devhub')


def process_validation(validation, is_compatibility=False, file_hash=None):
    """Process validation results into the format expected by the web
    frontend, including transforming certain fields into HTML,  mangling
    compatibility messages, and limiting the number of messages displayed."""
    validation = fix_addons_linter_output(validation)

    if is_compatibility:
        mangle_compatibility_messages(validation)

    # Set an ending tier if we don't have one (which probably means
    # we're dealing with mock validation results or the addons-linter).
    validation.setdefault('ending_tier', 0)

    if not validation['ending_tier'] and validation['messages']:
        validation['ending_tier'] = max(msg.get('tier', -1)
                                        for msg in validation['messages'])

    limit_validation_results(validation)

    htmlify_validation(validation)

    return validation


def mangle_compatibility_messages(validation):
    """Mangle compatibility messages so that the message type matches the
    compatibility type, and alter totals as appropriate."""

    compat = validation['compatibility_summary']
    for k in ('errors', 'warnings', 'notices'):
        validation[k] = compat[k]

    for msg in validation['messages']:
        if msg['compatibility_type']:
            msg['type'] = msg['compatibility_type']


def limit_validation_results(validation):
    """Limit the number of messages displayed in a set of validation results,
    and if truncation has occurred, add a new message explaining so."""

    messages = validation['messages']
    lim = settings.VALIDATOR_MESSAGE_LIMIT
    if lim and len(messages) > lim:
        # Sort messages by severity first so that the most important messages
        # are the one we keep.
        TYPES = {'error': 0, 'warning': 2, 'notice': 3}

        def message_key(message):
            return TYPES.get(message.get('type'))
        messages.sort(key=message_key)

        leftover_count = len(messages) - lim
        del messages[lim:]

        # The type of the truncation message should be the type of the most
        # severe message in the results.
        if validation['errors']:
            msg_type = 'error'
        elif validation['warnings']:
            msg_type = 'warning'
        else:
            msg_type = 'notice'

        compat_type = (msg_type if any(msg.get('compatibility_type')
                                       for msg in messages)
                       else None)

        message = ugettext(
            'Validation generated too many errors/warnings so %s '
            'messages were truncated. After addressing the visible '
            'messages, you\'ll be able to see the others.') % leftover_count

        messages.insert(0, {
            'tier': 1,
            'type': msg_type,
            # To respect the message structure, see bug 1139674.
            'id': ['validation', 'messages', 'truncated'],
            'message': message,
            'description': [],
            'compatibility_type': compat_type})


def htmlify_validation(validation):
    """Process the `message` and `description` fields into
    safe HTML, with URLs turned into links."""

    for msg in validation['messages']:
        msg['message'] = linkify_escape(msg['message'])

        if 'description' in msg:
            # Description may be returned as a single string, or list of
            # strings. Turn it into lists for simplicity on the client side.
            if not isinstance(msg['description'], (list, tuple)):
                msg['description'] = [msg['description']]

            msg['description'] = [
                linkify_escape(text) for text in msg['description']]


def fix_addons_linter_output(validation, listed=True):
    """Make sure the output from the addons-linter is the same as amo-validator
    for backwards compatibility reasons."""
    if 'messages' in validation:
        # addons-linter doesn't contain this, return the original validation
        # untouched
        return validation

    def _merged_messages():
        for type_ in ('errors', 'notices', 'warnings'):
            for msg in validation[type_]:
                # FIXME: Remove `uid` once addons-linter generates it
                msg['uid'] = uuid.uuid4().hex
                msg['type'] = msg.pop('_type')
                msg['id'] = [msg.pop('code')]
                # We don't have the concept of tiers for the addons-linter
                # currently
                msg['tier'] = 1
                yield msg

    identified_files = {
        name: {'path': path}
        for name, path in validation['metadata'].get('jsLibs', {}).items()
    }

    return {
        'success': not validation['errors'],
        'compatibility_summary': {
            'warnings': 0,
            'errors': 0,
            'notices': 0,
        },
        'notices': validation['summary']['notices'],
        'warnings': validation['summary']['warnings'],
        'errors': validation['summary']['errors'],
        'messages': list(_merged_messages()),
        'metadata': {
            'listed': listed,
            'identified_files': identified_files,
            'processed_by_addons_linter': True,
            'is_webextension': True
        },
        # The addons-linter only deals with WebExtensions and no longer
        # outputs this itself, so we hardcode it.
        'detected_type': 'extension',
        'ending_tier': 5,
    }


def find_previous_version(addon, file, version_string, channel):
    """
    Find the most recent previous version of this add-on, prior to
    `version`, that can be used to issue upgrade warnings.
    """
    if not addon or not version_string:
        return

    version = VersionString(version_string)

    # Find any previous version of this add-on with the correct status
    # to match the given file.
    files = File.objects.filter(version__addon=addon, version__channel=channel)

    if (channel == amo.RELEASE_CHANNEL_LISTED and
            (file and file.status != amo.STATUS_BETA)):
        # TODO: We'll also need to implement this for FileUploads
        # when we can accurately determine whether a new upload
        # is a beta version.
        files = files.filter(status=amo.STATUS_PUBLIC)
    else:
        files = files.filter(Q(status=amo.STATUS_PUBLIC) |
                             Q(status=amo.STATUS_BETA, is_signed=True))

    if file:

        # Add some extra filters if we're validating a File instance,
        # to try to get the closest possible match.
        files = (files.exclude(pk=file.pk)
                 # Files which are not for the same platform, but have
                 # other files in the same version which are.
                 .exclude(~Q(platform=file.platform) &
                          Q(version__files__platform=file.platform))
                 # Files which are not for either the same platform or for
                 # all platforms, but have other versions in the same
                 # version which are.
                 .exclude(~Q(platform__in=(file.platform,
                                           amo.PLATFORM_ALL.id)) &
                          Q(version__files__platform=amo.PLATFORM_ALL.id)))

    for file_ in files.order_by('-id'):
        # Only accept versions which come before the one we're validating.
        if VersionString(file_.version.version) < version:
            return file_


class Validator(object):
    """Class which handles creating or fetching validation results for File
    and FileUpload instances."""

    def __init__(self, file_, addon=None, listed=None):
        self.addon = addon
        self.file = None
        self.prev_file = None

        if isinstance(file_, FileUpload):
            assert listed is not None
            channel = (amo.RELEASE_CHANNEL_LISTED if listed else
                       amo.RELEASE_CHANNEL_UNLISTED)
            save = tasks.handle_upload_validation_result
            is_webextension = False
            # We're dealing with a bare file upload. Try to extract the
            # metadata that we need to match it against a previous upload
            # from the file itself.
            try:
                addon_data = parse_addon(file_, check=False)
                is_webextension = addon_data.get('is_webextension', False)
            except ValidationError as form_error:
                log.info('could not parse addon for upload {}: {}'
                         .format(file_.pk, form_error))
                addon_data = None
            else:
                file_.update(version=addon_data.get('version'))

            validate = self.validate_upload(file_, channel, is_webextension)
        elif isinstance(file_, File):
            # The listed flag for a File object should always come from
            # the status of its owner Addon. If the caller tries to override
            # this, something is wrong.
            assert listed is None

            channel = file_.version.channel
            save = tasks.handle_file_validation_result
            validate = self.validate_file(file_)

            self.file = file_
            self.addon = self.file.version.addon
            addon_data = {'guid': self.addon.guid,
                          'version': self.file.version.version}
        else:
            raise ValueError

        # Fallback error handler to save a set of exception results, in case
        # anything unexpected happens during processing.
        on_error = save.subtask(
            [amo.VALIDATOR_SKELETON_EXCEPTION, file_.pk, channel],
            immutable=True)

        # When the validation jobs complete, pass the results to the
        # appropriate save task for the object type.
        self.task = chain(validate, save.subtask([file_.pk, channel],
                                                 link_error=on_error))

        # Create a cache key for the task, so multiple requests to
        # validate the same object do not result in duplicate tasks.
        opts = file_._meta
        self.cache_key = 'validation-task:{0}.{1}:{2}:{3}'.format(
            opts.app_label, opts.object_name, file_.pk, listed)

    @staticmethod
    def validate_file(file):
        """Return a subtask to validate a File instance."""
        kwargs = {
            'hash_': file.original_hash,
            'is_webextension': file.is_webextension}
        return tasks.validate_file.subtask([file.pk], kwargs)

    @staticmethod
    def validate_upload(upload, channel, is_webextension):
        """Return a subtask to validate a FileUpload instance."""
        assert not upload.validation

        kwargs = {
            'hash_': upload.hash,
            'listed': (channel == amo.RELEASE_CHANNEL_LISTED),
            'is_webextension': is_webextension}
        return tasks.validate_file_path.subtask([upload.path], kwargs)
