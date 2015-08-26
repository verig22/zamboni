# -*- coding: utf-8 -*-
import json
import os.path
from uuid import UUID

from django.conf import settings
from django.core.urlresolvers import reverse
from django.db import models
from django.utils.functional import lazy

import commonware.log
from django_statsd.clients import statsd
from uuidfield.fields import UUIDField

from lib.crypto.packaged import sign_app, SigningError
from mkt.files.models import cleanup_file, nfd_str
from mkt.langpacks.utils import LanguagePackParser
from mkt.translations.utils import to_language
from mkt.site.helpers import absolutify
from mkt.site.models import ModelBase
from mkt.site.storage_utils import private_storage, public_storage
from mkt.site.utils import smart_path
from mkt.webapps.models import get_cached_minifest


log = commonware.log.getLogger('z.versions')


def _make_language_choices(languages):
    return [(to_language(lang_code), lang_name)
            for lang_code, lang_name in languages.items()]


LANGUAGE_CHOICES = lazy(_make_language_choices, list)(settings.LANGUAGES)


class LangPack(ModelBase):
    # Primary key is a uuid in order to be able to set it in advance (we need
    # something unique for the filename, and we don't have a slug).
    uuid = UUIDField(primary_key=True, auto=True)

    # Fields for which the manifest is the source of truth - can't be
    # overridden by the API.
    language = models.CharField(choices=LANGUAGE_CHOICES,
                                default=settings.LANGUAGE_CODE,
                                max_length=10)
    fxos_version = models.CharField(max_length=255, default='')
    version = models.CharField(max_length=255, default='')
    manifest = models.TextField()

    # Fields automatically set when uploading files.
    file_version = models.PositiveIntegerField(default=0)

    # Fields that can be modified using the API.
    active = models.BooleanField(default=False)

    # Note: we don't need to link a LangPack to an user right now, but in the
    # future, if we want to do that, call it user (single owner) or authors
    # (multiple authors) to be compatible with the API permission classes.

    class Meta:
        ordering = (('language'), )
        index_together = (('fxos_version', 'active', 'language'),)

    @property
    def filename(self):
        return '%s-%s.zip' % (self.uuid, self.version)

    @property
    def path_prefix(self):
        return os.path.join(settings.ADDONS_PATH, 'langpacks', str(self.pk))

    @property
    def file_path(self):
        return os.path.join(self.path_prefix, nfd_str(self.filename))

    @property
    def download_url(self):
        url = ('%s/langpack.zip' %
               reverse('downloads.langpack', args=[unicode(self.pk)]))
        return absolutify(url)

    @property
    def manifest_url(self):
        """Return URL to the minifest for the langpack"""
        if self.active:
            return absolutify(
                reverse('langpack.manifest', args=[unicode(UUID(self.pk))]))
        return ''

    def __unicode__(self):
        return u'%s (%s)' % (self.get_language_display(), self.fxos_version)

    def is_public(self):
        return self.active

    def get_package_path(self):
        return self.download_url

    def get_minifest_contents(self, force=False):
        """Return the "mini" manifest + etag for this langpack, caching it in
        the process.

        Call this with `force=True` whenever we need to update the cached
        version of this manifest, e.g., when a new version of the langpack
        has been pushed."""
        return get_cached_minifest(self, force=force)

    def get_manifest_json(self):
        """Return the json representation of the (full) manifest for this
        langpack, as stored when it was uploaded."""
        return json.loads(self.manifest)

    def reset_uuid(self):
        self.uuid = self._meta.get_field('uuid')._create_uuid()

    def handle_file_operations(self, upload):
        """Handle file operations on an instance by using the FileUpload object
        passed to set filename, file_version on the LangPack instance, and
        moving the temporary file to its final destination."""
        upload.path = smart_path(nfd_str(upload.path))
        if not self.uuid:
            self.reset_uuid()
        if public_storage.exists(self.file_path):
            # The filename should not exist. If it does, it means we are trying
            # to re-upload the same version. This should have been caught
            # before, so just raise an exception.
            raise RuntimeError(
                'Trying to upload a file to a destination that already exists')

        self.file_version = self.file_version + 1

        # Because we are only dealing with langpacks generated by Mozilla atm,
        # we can directly sign the file before copying it to its final
        # destination. The filename changes with the version, so when a new
        # file is uploaded we should still be able to serve the old one until
        # the new info is stored in the db.
        self.sign_and_move_file(upload)

    def sign_and_move_file(self, upload):
        ids = json.dumps({
            # 'id' needs to be an unique identifier not shared with anything
            # else (other langpacks, webapps, extensions...), but should not
            # change when there is an update. Since our PKs are uuid it's the
            # best choice.
            'id': self.pk,
            # 'version' should be an integer and should be monotonically
            # increasing.
            'version': self.file_version
        })
        with statsd.timer('langpacks.sign'):
            try:
                # This will read the upload.path file, generate a signature
                # and write the signed file to self.file_path.
                sign_app(private_storage.open(upload.path),
                         self.file_path, ids)
            except SigningError:
                log.info('[LangPack:%s] Signing failed' % self.pk)
                if public_storage.exists(self.file_path):
                    public_storage.delete(self.file_path)
                raise

    @classmethod
    def from_upload(cls, upload, instance=None):
        """Handle creating/editing the LangPack instance and saving it to db,
        as well as file operations, from a FileUpload instance. Can throw
        a ValidationError or SigningError, so should always be called within a
        try/except."""
        parser = LanguagePackParser(instance=instance)
        data = parser.parse(upload)
        allowed_fields = ('language', 'fxos_version', 'version')
        data = dict((k, v) for k, v in data.items() if k in allowed_fields)
        data['manifest'] = json.dumps(parser.get_json_data(upload))
        if instance:
            # If we were passed an instance, override fields on it using the
            # data from the uploaded package.
            instance.__dict__.update(**data)
        else:
            # Build a new instance.
            instance = cls(**data)
        # Do last-minute validation that requires an instance.
        cls._meta.get_field('language').validate(instance.language, instance)
        # Fill in fields depending on the file contents, and move the file.
        instance.handle_file_operations(upload)
        # Save!
        instance.save()
        # Bust caching of manifest by passing force=True.
        instance.get_minifest_contents(force=True)
        return instance


models.signals.post_delete.connect(cleanup_file, sender=LangPack,
                                   dispatch_uid='langpack_cleanup_file')
