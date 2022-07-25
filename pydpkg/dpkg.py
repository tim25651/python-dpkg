""" pydpkg.dpkg.Dpkg: a class to represent dpkg files """

# stdlib imports
import hashlib
import io
import logging
import lzma
import os
import tarfile
from functools import cmp_to_key
from email import message_from_string
from gzip import GzipFile

# pypi imports
import six
import zstandard
from arpy import Archive

# local imports
from pydpkg.exceptions import (
    DpkgError,
    DpkgVersionError,
    DpkgMissingControlFile,
    DpkgMissingControlGzipFile,
    DpkgMissingRequiredHeaderError,
)
from pydpkg.base import _Dbase

REQUIRED_HEADERS = ("package", "version", "architecture")


# pylint: disable=too-many-instance-attributes,too-many-public-methods
class Dpkg(_Dbase):

    """Class allowing import and manipulation of a debian package file."""

    def __init__(self, filename=None, ignore_missing=False, logger=None):
        """Constructor for Dpkg object

        :param filename: string
        :param ignore_missing: bool
        :param logger: logging.Logger
        """
        self.filename = os.path.expanduser(filename)
        self.ignore_missing = ignore_missing
        if not isinstance(self.filename, six.string_types):
            raise DpkgError("filename argument must be a string")
        if not os.path.isfile(self.filename):
            raise DpkgError(f"filename '{filename}' does not exist")
        self._log = logger or logging.getLogger(__name__)
        self._fileinfo = None
        self._control_str = None
        self._headers = None
        self._message = None
        self._upstream_version = None
        self._debian_revision = None
        self._epoch = None

    def __repr__(self):
        return repr(self.control_str)

    def __str__(self):
        return six.text_type(self.control_str)

    def __getattr__(self, attr):
        """Overload getattr to treat control message headers as object
        attributes (so long as they do not conflict with an existing
        attribute).

        :param attr: string
        :returns: string
        :raises: AttributeError
        """
        # beware: email.Message[nonexistent] returns None not KeyError
        if attr in self.message:
            return self.message[attr]
        raise AttributeError(f"'Dpkg' object has no attribute '{attr}'")

    @property
    def message(self):
        """Return an email.Message object containing the package control
        structure.

        :returns: email.Message
        """
        if self._message is None:
            self._message = self._process_dpkg_file(self.filename)
        return self._message

    @property
    def control_str(self):
        """Return the control message as a string

        :returns: string
        """
        if self._control_str is None:
            self._control_str = self.message.as_string()
        return self._control_str

    @property
    def headers(self):
        """Return the control message headers as a dict

        :returns: dict
        """
        if self._headers is None:
            self._headers = dict(self.message.items())
        return self._headers

    @property
    def fileinfo(self):
        """Return a dictionary containing md5/sha1/sha256 checksums
        and the size in bytes of our target file.

        :returns: dict
        """
        if self._fileinfo is None:
            h_md5 = hashlib.md5()
            h_sha1 = hashlib.sha1()
            h_sha256 = hashlib.sha256()
            with open(self.filename, "rb") as dpkg_file:
                for chunk in iter(lambda: dpkg_file.read(128), b""):
                    h_md5.update(chunk)
                    h_sha1.update(chunk)
                    h_sha256.update(chunk)
            self._fileinfo = {
                "md5": h_md5.hexdigest(),
                "sha1": h_sha1.hexdigest(),
                "sha256": h_sha256.hexdigest(),
                "filesize": os.path.getsize(self.filename),
            }
        return self._fileinfo

    @property
    def md5(self):
        """Return the md5 hash of our target file

        :returns: string
        """
        return self.fileinfo["md5"]

    @property
    def sha1(self):
        """Return the sha1 hash of our target file

        :returns: string
        """
        return self.fileinfo["sha1"]

    @property
    def sha256(self):
        """Return the sha256 hash of our target file

        :returns: string
        """
        return self.fileinfo["sha256"]

    @property
    def filesize(self):
        """Return the size of our target file

        :returns: string
        """
        return self.fileinfo["filesize"]

    @property
    def epoch(self):
        """Return the epoch portion of the package version string

        :returns: int
        """
        if self._epoch is None:
            self._epoch = self.split_full_version(self.version)[0]
        return self._epoch

    @property
    def upstream_version(self):
        """Return the upstream portion of the package version string

        :returns: string
        """
        if self._upstream_version is None:
            self._upstream_version = self.split_full_version(self.version)[1]
        return self._upstream_version

    @property
    def debian_revision(self):
        """Return the debian revision portion of the package version string

        :returns: string
        """
        if self._debian_revision is None:
            self._debian_revision = self.split_full_version(self.version)[2]
        return self._debian_revision

    def get(self, item, default=None):
        """Return an object property, a message header, None or the caller-
        provided default.

        :param item: string
        :param default:
        :returns: string
        """
        try:
            return self.__getitem__(item)
        except KeyError:
            return default

    def get_header(self, header):
        """Return an individual control message header

        :returns: string or None
        """
        return self.message.get(header)

    def compare_version_with(self, version_str):
        """Compare my version to an arbitrary version"""
        return Dpkg.compare_versions(self.get_header("version"), version_str)

    @staticmethod
    def _force_encoding(obj, encoding="utf-8"):
        """Enforce uniform text encoding"""
        if isinstance(obj, six.string_types):
            if not isinstance(obj, six.text_type):
                obj = six.text_type(obj, encoding)
        return obj

    def _extract_message(self, ctar):
        # pathname in the tar could be ./control, or just control
        # (there would never be two control files...right?)
        tar_members = [os.path.basename(x.name) for x in ctar.getmembers()]
        self._log.debug("got tar members: %s", tar_members)
        if "control" not in tar_members:
            raise DpkgMissingControlFile(
                "Corrupt dpkg file: no control file in control.tar.gz"
            )
        control_idx = tar_members.index("control")
        self._log.debug("got control index: %s", control_idx)
        # at last!
        control_file = ctar.extractfile(ctar.getmembers()[control_idx])
        self._log.debug("got control file: %s", control_file)
        message_body = control_file.read()
        # py27 lacks email.message_from_bytes, so...
        if isinstance(message_body, bytes):
            message_body = message_body.decode("utf-8")
        message = message_from_string(message_body)
        self._log.debug("got control message: %s", message)
        return message

    def _process_dpkg_file(self, filename):
        dpkg_archive = Archive(filename)
        dpkg_archive.read_all_headers()
        if b"control.tar.gz" in dpkg_archive.archived_files:
            control_archive = dpkg_archive.archived_files[b"control.tar.gz"]
            control_archive_type = "gz"
        elif b"control.tar.xz" in dpkg_archive.archived_files:
            control_archive = dpkg_archive.archived_files[b"control.tar.xz"]
            control_archive_type = "xz"
        elif b"control.tar.zst" in dpkg_archive.archived_files:
            control_archive = dpkg_archive.archived_files[b"control.tar.zst"]
            control_archive_type = "zst"
        else:
            raise DpkgMissingControlGzipFile(
                "Corrupt dpkg file: no control.tar.gz/xz/zst file in ar archive."
            )
        self._log.debug("found controlgz: %s", control_archive)

        if control_archive_type == "gz":
            with GzipFile(fileobj=control_archive) as gzf:
                self._log.debug("opened gzip control archive: %s", gzf)
                with tarfile.open(fileobj=io.BytesIO(gzf.read())) as ctar:
                    self._log.debug("opened tar file: %s", ctar)
                    message = self._extract_message(ctar)
        elif control_archive_type == "xz":
            with lzma.open(control_archive) as xzf:
                self._log.debug("opened xz control archive: %s", xzf)
                with tarfile.open(fileobj=io.BytesIO(xzf.read())) as ctar:
                    self._log.debug("opened tar file: %s", ctar)
                    message = self._extract_message(ctar)
        else:
            zst = zstandard.ZstdDecompressor()
            with zst.stream_reader(control_archive) as reader:
                self._log.debug("opened zst control archive: %s", reader)
                with tarfile.open(fileobj=io.BytesIO(reader.read())) as ctar:
                    self._log.debug("opened tar file: %s", ctar)
                    message = self._extract_message(ctar)

        for req in REQUIRED_HEADERS:
            if req not in list(map(str.lower, message.keys())):
                if self.ignore_missing:
                    self._log.debug('Header "%s" not found in control message', req)
                    continue
                raise DpkgMissingRequiredHeaderError(
                    f"Corrupt control section; header: '{req}' not found"
                )
        self._log.debug("all required headers found")

        for header in message.keys():
            self._log.debug("coercing header to utf8: %s", header)
            message.replace_header(header, self._force_encoding(message[header]))
        self._log.debug("all required headers coerced")

        return message

    @staticmethod
    def get_epoch(version_str):
        """Parse the epoch out of a package version string.
        Return (epoch, version); epoch is zero if not found."""
        try:
            # there could be more than one colon,
            # but we only care about the first
            e_index = version_str.index(":")
        except ValueError:
            # no colons means no epoch; that's valid, man
            return 0, version_str

        try:
            epoch = int(version_str[0:e_index])
        except ValueError as ex:
            raise DpkgVersionError(
                f"Corrupt dpkg version '{version_str}': epochs can only be ints, and "
                "epochless versions cannot use the colon character."
            ) from ex

        return epoch, version_str[e_index + 1 :]

    @staticmethod
    def get_upstream(version_str):
        """Given a version string that could potentially contain both an upstream
        revision and a debian revision, return a tuple of both.  If there is no
        debian revision, return 0 as the second tuple element."""
        try:
            d_index = version_str.rindex("-")
        except ValueError:
            # no hyphens means no debian version, also valid.
            return version_str, "0"

        return version_str[0:d_index], version_str[d_index + 1 :]

    @staticmethod
    def split_full_version(version_str):
        """Split a full version string into epoch, upstream version and
        debian revision.
        :param: version_str
        :returns: tuple"""
        epoch, full_ver = Dpkg.get_epoch(version_str)
        upstream_rev, debian_rev = Dpkg.get_upstream(full_ver)
        return epoch, upstream_rev, debian_rev

    @staticmethod
    def get_alphas(revision_str):
        """Return a tuple of the first non-digit characters of a revision (which
        may be empty) and the remaining characters."""
        # get the index of the first digit
        for i, char in enumerate(revision_str):
            if char.isdigit():
                if i == 0:
                    return "", revision_str
                return revision_str[0:i], revision_str[i:]
        # string is entirely alphas
        return revision_str, ""

    @staticmethod
    def get_digits(revision_str):
        """Return a tuple of the first integer characters of a revision (which
        may be empty) and the remains."""
        # If the string is empty, return (0,'')
        if not revision_str:
            return 0, ""
        # get the index of the first non-digit
        for i, char in enumerate(revision_str):
            if not char.isdigit():
                if i == 0:
                    return 0, revision_str
                return int(revision_str[0:i]), revision_str[i:]
        # string is entirely digits
        return int(revision_str), ""

    @staticmethod
    def listify(revision_str):
        """Split a revision string into a list of alternating between strings and
        numbers, padded on either end to always be "str, int, str, int..." and
        always be of even length.  This allows us to trivially implement the
        comparison algorithm described at section 5.6.12 in:
        https://www.debian.org/doc/debian-policy/ch-controlfields.html#version
        """
        result = []
        while revision_str:
            rev_1, remains = Dpkg.get_alphas(revision_str)
            rev_2, remains = Dpkg.get_digits(remains)
            result.extend([rev_1, rev_2])
            revision_str = remains
        return result

    # pylint: disable=invalid-name,too-many-return-statements
    @staticmethod
    def dstringcmp(a, b):
        """debian package version string section lexical sort algorithm

        "The lexical comparison is a comparison of ASCII values modified so
        that all the letters sort earlier than all the non-letters and so that
        a tilde sorts before anything, even the end of a part."
        """

        if a == b:
            return 0
        try:
            for i, char in enumerate(a):
                if char == b[i]:
                    continue
                # "a tilde sorts before anything, even the end of a part"
                # (emptyness)
                if char == "~":
                    return -1
                if b[i] == "~":
                    return 1
                # "all the letters sort earlier than all the non-letters"
                if char.isalpha() and not b[i].isalpha():
                    return -1
                if not char.isalpha() and b[i].isalpha():
                    return 1
                # otherwise lexical sort
                if ord(char) > ord(b[i]):
                    return 1
                if ord(char) < ord(b[i]):
                    return -1
        except IndexError:
            # a is longer than b but otherwise equal, hence greater
            # ...except for goddamn tildes
            if char == "~":
                return -1
            return 1
        # if we get here, a is shorter than b but otherwise equal, hence lesser
        # ...except for goddamn tildes
        if b[len(a)] == "~":
            return 1
        return -1

    @staticmethod
    def compare_revision_strings(rev1, rev2):
        """Compare two debian revision strings as described at
        https://www.debian.org/doc/debian-policy/ch-controlfields.html#version
        """
        # TODO(memory): this function now fails pylint R0912 too-many-branches
        if rev1 == rev2:
            return 0
        # listify pads results so that we will always be comparing ints to ints
        # and strings to strings (at least until we fall off the end of a list)
        list1 = Dpkg.listify(rev1)
        list2 = Dpkg.listify(rev2)
        if list1 == list2:
            return 0
        try:
            for i, item in enumerate(list1):
                # explicitly raise IndexError if we've fallen off the edge of list2
                if i >= len(list2):
                    raise IndexError
                # just in case
                if not isinstance(item, list2[i].__class__):
                    raise DpkgVersionError(
                        f"Cannot compare '{item}' to {list2[i]}, something has gone horribly awry."
                    )
                # if the items are equal, next
                if item == list2[i]:
                    continue
                # numeric comparison
                if isinstance(item, int):
                    if item > list2[i]:
                        return 1
                    if item < list2[i]:
                        return -1
                else:
                    # string comparison
                    return Dpkg.dstringcmp(item, list2[i])
        except IndexError:
            # rev1 is longer than rev2 but otherwise equal, hence greater
            # ...except for goddamn tildes
            if list1[len(list2)][0][0] == "~":
                return -1
            return 1
        # rev1 is shorter than rev2 but otherwise equal, hence lesser
        # ...except for goddamn tildes
        if list2[len(list1)][0][0] == "~":
            return 1
        return -1

    @staticmethod
    def compare_versions(ver1, ver2):
        """Function to compare two Debian package version strings,
        suitable for passing to list.sort() and friends."""
        if ver1 == ver2:
            return 0

        # note the string conversion: the debian policy here explicitly
        # specifies ASCII string comparisons, so if you are mad enough to
        # actually cram unicode characters into your package name, you are on
        # your own.
        epoch1, upstream1, debian1 = Dpkg.split_full_version(str(ver1))
        epoch2, upstream2, debian2 = Dpkg.split_full_version(str(ver2))

        # if epochs differ, immediately return the newer one
        if epoch1 < epoch2:
            return -1
        if epoch1 > epoch2:
            return 1

        # then, compare the upstream versions
        upstr_res = Dpkg.compare_revision_strings(upstream1, upstream2)
        if upstr_res != 0:
            return upstr_res

        debian_res = Dpkg.compare_revision_strings(debian1, debian2)
        if debian_res != 0:
            return debian_res

        # at this point, the versions are equal, but due to an interpolated
        # zero in either the epoch or the debian version
        return 0

    @staticmethod
    def compare_versions_key(x):
        """Uses functools.cmp_to_key to convert the compare_versions()
        function to a function suitable to passing to sorted() and friends
        as a key."""
        return cmp_to_key(Dpkg.compare_versions)(x)

    @staticmethod
    def dstringcmp_key(x):
        """Uses functools.cmp_to_key to convert the dstringcmp()
        function to a function suitable to passing to sorted() and friends
        as a key."""
        return cmp_to_key(Dpkg.dstringcmp)(x)
