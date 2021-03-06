# -*- Mode:Python; indent-tabs-mode:nil; tab-width:4 -*-
#
# Copyright 2002 Ben Escoto <ben@emerose.org>
# Copyright 2007 Kenneth Loafman <kenneth@loafman.com>
#
# This file is part of duplicity.
#
# Duplicity is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation; either version 2 of the License, or (at your
# option) any later version.
#
# Duplicity is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with duplicity; if not, write to the Free Software Foundation,
# Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA

import os
import time

import duplicity.backend
from duplicity import globals
from duplicity import log
from duplicity.errors import * #@UnusedWildImport
from duplicity.util import exception_traceback
from duplicity.backend import retry
from duplicity import progress

BOTO_MIN_VERSION = "2.1.1"


def get_connection(scheme, parsed_url, storage_uri):
    try:
        from boto.s3.connection import S3Connection
        assert hasattr(S3Connection, 'lookup')

        # Newer versions of boto default to using
        # virtual hosting for buckets as a result of
        # upstream deprecation of the old-style access
        # method by Amazon S3. This change is not
        # backwards compatible (in particular with
        # respect to upper case characters in bucket
        # names); so we default to forcing use of the
        # old-style method unless the user has
        # explicitly asked us to use new-style bucket
        # access.
        #
        # Note that if the user wants to use new-style
        # buckets, we use the subdomain calling form
        # rather than given the option of both
        # subdomain and vhost. The reason being that
        # anything addressable as a vhost, is also
        # addressable as a subdomain. Seeing as the
        # latter is mostly a convenience method of
        # allowing browse:able content semi-invisibly
        # being hosted on S3, the former format makes
        # a lot more sense for us to use - being
        # explicit about what is happening (the fact
        # that we are talking to S3 servers).

        try:
            from boto.s3.connection import OrdinaryCallingFormat
            from boto.s3.connection import SubdomainCallingFormat
            cfs_supported = True
            calling_format = OrdinaryCallingFormat()
        except ImportError:
            cfs_supported = False
            calling_format = None

        if globals.s3_use_new_style:
            if cfs_supported:
                calling_format = SubdomainCallingFormat()
            else:
                log.FatalError("Use of new-style (subdomain) S3 bucket addressing was"
                               "requested, but does not seem to be supported by the "
                               "boto library. Either you need to upgrade your boto "
                               "library or duplicity has failed to correctly detect "
                               "the appropriate support.",
                               log.ErrorCode.boto_old_style)
        else:
            if cfs_supported:
                calling_format = OrdinaryCallingFormat()
            else:
                calling_format = None

    except ImportError:
        log.FatalError("This backend (s3) requires boto library, version %s or later, "
                       "(http://code.google.com/p/boto/)." % BOTO_MIN_VERSION,
                       log.ErrorCode.boto_lib_too_old)

    if not parsed_url.hostname:
        # Use the default host.
        conn = storage_uri.connect(is_secure=(not globals.s3_unencrypted_connection))
    else:
        assert scheme == 's3'
        conn = storage_uri.connect(host=parsed_url.hostname,
                                   is_secure=(not globals.s3_unencrypted_connection))

    if hasattr(conn, 'calling_format'):
        if calling_format is None:
            log.FatalError("It seems we previously failed to detect support for calling "
                           "formats in the boto library, yet the support is there. This is "
                           "almost certainly a duplicity bug.",
                           log.ErrorCode.boto_calling_format)
        else:
            conn.calling_format = calling_format

    else:
        # Duplicity hangs if boto gets a null bucket name.
        # HC: Caught a socket error, trying to recover
        raise BackendException('Boto requires a bucket name.')
    return conn


class BotoBackend(duplicity.backend.Backend):
    """
    Backend for Amazon's Simple Storage System, (aka Amazon S3), though
    the use of the boto module, (http://code.google.com/p/boto/).

    To make use of this backend you must set aws_access_key_id
    and aws_secret_access_key in your ~/.boto or /etc/boto.cfg
    with your Amazon Web Services key id and secret respectively.
    Alternatively you can export the environment variables
    AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY.
    """

    def __init__(self, parsed_url):
        import boto
        duplicity.backend.Backend.__init__(self, parsed_url)

        assert boto.Version >= BOTO_MIN_VERSION

        # This folds the null prefix and all null parts, which means that:
        #  //MyBucket/ and //MyBucket are equivalent.
        #  //MyBucket//My///My/Prefix/ and //MyBucket/My/Prefix are equivalent.
        self.url_parts = filter(lambda x: x != '', parsed_url.path.split('/'))

        if self.url_parts:
            self.bucket_name = self.url_parts.pop(0)
        else:
            # Duplicity hangs if boto gets a null bucket name.
            # HC: Caught a socket error, trying to recover
            raise BackendException('Boto requires a bucket name.')

        self.scheme = parsed_url.scheme

        if self.url_parts:
            self.key_prefix = '%s/' % '/'.join(self.url_parts)
        else:
            self.key_prefix = ''

        self.straight_url = duplicity.backend.strip_auth_from_url(parsed_url)
        self.parsed_url = parsed_url

        # duplicity and boto.storage_uri() have different URI formats.
        # boto uses scheme://bucket[/name] and specifies hostname on connect()
        self.boto_uri_str = '://'.join((parsed_url.scheme[:2],
                                        parsed_url.path.lstrip('/')))
        self.resetConnection()
        self._listed_keys = {}

    def close(self):
        del self._listed_keys
        self._listed_keys = {}
        self.bucket = None
        self.conn = None
        self.storage_uri = None
        del self.conn
        del self.storage_uri

    def resetConnection(self):
        import boto
        if getattr(self, 'conn', False):
            self.conn.close()
        self.bucket = None
        self.conn = None
        self.storage_uri = None
        del self.conn
        del self.storage_uri
        self.storage_uri = boto.storage_uri(self.boto_uri_str)
        self.conn = get_connection(self.scheme, self.parsed_url, self.storage_uri)
        self.bucket = self.conn.lookup(self.bucket_name)

    def put(self, source_path, remote_filename=None):
        from boto.s3.connection import Location
        if globals.s3_european_buckets:
            if not globals.s3_use_new_style:
                log.FatalError("European bucket creation was requested, but not new-style "
                               "bucket addressing (--s3-use-new-style)",
                               log.ErrorCode.s3_bucket_not_style)
        #Network glitch may prevent first few attempts of creating/looking up a bucket
        for n in range(1, globals.num_retries+1):
            if self.bucket:
                break
            if n > 1:
                time.sleep(30)
                self.resetConnection()
            try:
                try:
                    self.bucket = self.conn.get_bucket(self.bucket_name, validate=True)
                except Exception, e:
                    if "NoSuchBucket" in str(e):
                        if globals.s3_european_buckets:
                            self.bucket = self.conn.create_bucket(self.bucket_name,
                                                                  location=Location.EU)
                        else:
                            self.bucket = self.conn.create_bucket(self.bucket_name)
                    else:
                        raise e
            except Exception, e:
                log.Warn("Failed to create bucket (attempt #%d) '%s' failed (reason: %s: %s)"
                         "" % (n, self.bucket_name,
                               e.__class__.__name__,
                               str(e)))

        if not remote_filename:
            remote_filename = source_path.get_filename()
        key = self.bucket.new_key(self.key_prefix + remote_filename)

        for n in range(1, globals.num_retries+1):
            if n > 1:
                # sleep before retry (new connection to a **hopeful** new host, so no need to wait so long)
                time.sleep(10)

            if globals.s3_use_rrs:
                storage_class = 'REDUCED_REDUNDANCY'
            else:
                storage_class = 'STANDARD'
            log.Info("Uploading %s/%s to %s Storage" % (self.straight_url, remote_filename, storage_class))
            try:
                if globals.s3_use_sse:
                    headers = {
                    'Content-Type': 'application/octet-stream',
                    'x-amz-storage-class': storage_class,
                    'x-amz-server-side-encryption': 'AES256'
                }
                else:
                    headers = {
                    'Content-Type': 'application/octet-stream',
                    'x-amz-storage-class': storage_class
                }
                
                upload_start = time.time()
                self.upload(source_path.name, key, headers)
                upload_end = time.time()
                total_s = abs(upload_end-upload_start) or 1  # prevent a zero value!
                rough_upload_speed = os.path.getsize(source_path.name)/total_s
                self.resetConnection()
                log.Debug("Uploaded %s/%s to %s Storage at roughly %f bytes/second" % (self.straight_url, remote_filename, storage_class, rough_upload_speed))
                return
            except Exception, e:
                log.Warn("Upload '%s/%s' failed (attempt #%d, reason: %s: %s)"
                         "" % (self.straight_url,
                               remote_filename,
                               n,
                               e.__class__.__name__,
                               str(e)))
                log.Debug("Backtrace of previous error: %s" % (exception_traceback(),))
                self.resetConnection()
        log.Warn("Giving up trying to upload %s/%s after %d attempts" %
                 (self.straight_url, remote_filename, globals.num_retries))
        raise BackendException("Error uploading %s/%s" % (self.straight_url, remote_filename))

    def get(self, remote_filename, local_path):
        key_name = self.key_prefix + remote_filename
        self.pre_process_download(remote_filename, wait=True)
        key = self._listed_keys[key_name]
        for n in range(1, globals.num_retries+1):
            if n > 1:
                # sleep before retry (new connection to a **hopeful** new host, so no need to wait so long)
                time.sleep(10)
            log.Info("Downloading %s/%s" % (self.straight_url, remote_filename))
            try:
                self.resetConnection()
                key.get_contents_to_filename(local_path.name)
                local_path.setdata()
                return
            except Exception, e:
                log.Warn("Download %s/%s failed (attempt #%d, reason: %s: %s)"
                         "" % (self.straight_url,
                               remote_filename,
                               n,
                               e.__class__.__name__,
                               str(e)), 1)
                log.Debug("Backtrace of previous error: %s" % (exception_traceback(),))

        log.Warn("Giving up trying to download %s/%s after %d attempts" %
                (self.straight_url, remote_filename, globals.num_retries))
        raise BackendException("Error downloading %s/%s" % (self.straight_url, remote_filename))

    def _list(self):
        if not self.bucket:
            raise BackendException("No connection to backend")

        for n in range(1, globals.num_retries+1):
            if n > 1:
                # sleep before retry
                time.sleep(30)
                self.resetConnection()
            log.Info("Listing %s" % self.straight_url)
            try:
                return self._list_filenames_in_bucket()
            except Exception, e:
                log.Warn("List %s failed (attempt #%d, reason: %s: %s)"
                         "" % (self.straight_url,
                               n,
                               e.__class__.__name__,
                               str(e)), 1)
                log.Debug("Backtrace of previous error: %s" % (exception_traceback(),))
        log.Warn("Giving up trying to list %s after %d attempts" %
                (self.straight_url, globals.num_retries))
        raise BackendException("Error listng %s" % self.straight_url)

    def _list_filenames_in_bucket(self):
        # We add a 'd' to the prefix to make sure it is not null (for boto) and
        # to optimize the listing of our filenames, which always begin with 'd'.
        # This will cause a failure in the regression tests as below:
        #   FAIL: Test basic backend operations
        #   <tracback snipped>
        #   AssertionError: Got list: []
        #   Wanted: ['testfile']
        # Because of the need for this optimization, it should be left as is.
        #for k in self.bucket.list(prefix = self.key_prefix + 'd', delimiter = '/'):
        filename_list = []
        for k in self.bucket.list(prefix=self.key_prefix, delimiter='/'):
            try:
                filename = k.key.replace(self.key_prefix, '', 1)
                filename_list.append(filename)
                self._listed_keys[k.key] = k
                log.Debug("Listed %s/%s" % (self.straight_url, filename))
            except AttributeError:
                pass
        return filename_list

    def delete(self, filename_list):
        for filename in filename_list:
            self.bucket.delete_key(self.key_prefix + filename)
            log.Debug("Deleted %s/%s" % (self.straight_url, filename))

    @retry
    def _query_file_info(self, filename, raise_errors=False):
        try:
            key = self.bucket.lookup(self.key_prefix + filename)
            if key is None:
                return {'size': -1}
            return {'size': key.size}
        except Exception, e:
            log.Warn("Query %s/%s failed: %s"
                     "" % (self.straight_url,
                           filename,
                           str(e)))
            self.resetConnection()
            if raise_errors:
                raise e
            else:
                return {'size': None}

    def upload(self, filename, key, headers):
            key.set_contents_from_filename(filename, headers,
                                           cb=progress.report_transfer,
                                           num_cb=(max(2, 8 * globals.volsize / (1024 * 1024)))
                                           )  # Max num of callbacks = 8 times x megabyte
            key.close()

    def pre_process_download(self, files_to_download, wait=False):
        # Used primarily to move files in Glacier to S3
        if isinstance(files_to_download, basestring):
            files_to_download = [files_to_download]

        for remote_filename in files_to_download:
            success = False
            for n in range(1, globals.num_retries+1):
                if n > 1:
                    # sleep before retry (new connection to a **hopeful** new host, so no need to wait so long)
                    time.sleep(10)
                    self.resetConnection()
                try:
                    key_name = self.key_prefix + remote_filename
                    if not self._listed_keys.get(key_name, False):
                        self._listed_keys[key_name] = list(self.bucket.list(key_name))[0]
                    key = self._listed_keys[key_name]

                    if key.storage_class == "GLACIER":
                        # We need to move the file out of glacier
                        if not self.bucket.get_key(key.key).ongoing_restore:
                            log.Info("File %s is in Glacier storage, restoring to S3" % remote_filename)
                            key.restore(days=1)  # Shouldn't need this again after 1 day
                        if wait:
                            log.Info("Waiting for file %s to restore from Glacier" % remote_filename)
                            while self.bucket.get_key(key.key).ongoing_restore:
                                time.sleep(60)
                                self.resetConnection()
                            log.Info("File %s was successfully restored from Glacier" % remote_filename)
                    success = True
                    break
                except Exception, e:
                    log.Warn("Restoration from Glacier for file %s/%s failed (attempt #%d, reason: %s: %s)"
                             "" % (self.straight_url,
                                   remote_filename,
                                   n,
                                   e.__class__.__name__,
                                   str(e)), 1)
                    log.Debug("Backtrace of previous error: %s" % (exception_traceback(),))
            if not success:
                log.Warn("Giving up trying to restore %s/%s after %d attempts" %
                        (self.straight_url, remote_filename, globals.num_retries))
                raise BackendException("Error restoring %s/%s from Glacier to S3" % (self.straight_url, remote_filename))
