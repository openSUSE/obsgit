import argparse
import asyncio
import collections
import configparser
import csv
import datetime
import fnmatch
import functools
import getpass
import hashlib
import http
import itertools
import logging
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import aiohttp
import chardet
import pygit2

LOG = logging.getLogger(__name__)


def retry(func):
    async def wrapper(*args, **kwargs):
        to_exception = None
        retry = 0
        while retry < 5:
            try:
                return await func(*args, **kwargs)
            except asyncio.TimeoutError as e:
                to_exception = e
                retry += 1
                LOG.warning(f"TimeoutError: retry #{retry}")
                await asyncio.sleep(0.5)
            except http.client.HTTPException as e:
                to_exception = e
                # Try only one more time
                retry = 6
                LOG.error("HTTPException: retry one more time")
                await asyncio.sleep(0.5)
        raise to_exception

    return wrapper


# Class based on BasicAuth from aiohttp
class SSHAuth(aiohttp.BasicAuth):
    """Http SSH authentication helper."""

    def __new__(cls, login, password="", ssh_key="", encoding="latin1"):
        if login is None:
            raise ValueError("None is not allowed as login value")

        if password is None:
            raise ValueError("None is not allowed as password value")

        if ssh_key is None:
            raise ValueError("None is not allowed as ssh_key value")

        if ":" in login:
            raise ValueError('A ":" is not allowed in login (RFC 1945#section-11.1)')

        auth = super().__new__(cls, login, password, encoding)
        auth.ssh_key = ssh_key
        auth.authorization = ""
        auth.already_auth = False

        return auth

    def encode(self):
        """Encode credentials."""
        if self.authorization and not self.already_auth:
            self.already_auth = True
        return self.authorization

    def assert_signature_header(self, headers):
        header = [h for h in headers.getall("WWW-Authenticate") if "Signature" in h]
        if not header:
            raise Exception("Signature authentication not supported in the server")
        header = header[0]

        if "Use your developer account" not in header:
            raise Exception("Signature realm not expected")
        if "(created)" not in header:
            raise Exception("Signature header not expected")

    def ssh_sign(self, namespace, data):
        cmd = [
            "ssh-keygen",
            "-Y",
            "sign",
            "-f",
            self.ssh_key,
            "-q",
            "-n",
            namespace,
            "-P",
            self.password,
        ]
        out = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            input=data,
            stderr=subprocess.STDOUT,
            encoding=self.encoding,
        )
        lines = out.stdout.splitlines()

        # The signature has a header and a footer.  Extract them and
        # validate the output.
        header, signature, footer = lines[0], lines[1:-1], lines[-1]
        if header != "-----BEGIN SSH SIGNATURE-----":
            raise Exception(f"Error signing the data: {out.stdout}")
        if footer != "-----END SSH SIGNATURE-----":
            raise Exception(f"Error signing the data: {out.stdout}")

        return "".join(signature)

    def set_challenge(self, headers):
        # TODO: the specification support different headers, and the
        # real / namespace should be extracted from the headers
        #
        # For more complete implementations, check:
        #
        #  * https://datatracker.ietf.org/doc/draft-ietf-httpbis-message-signatures/
        #  * https://github.com/openSUSE/osc/pull/1032
        #  * https://github.com/crazyscientist/osc-tiny
        #
        self.assert_signature_header(headers)
        created = int(time.time())
        namespace = "Use your developer account"
        data = f"(created): {created}"
        signature = self.ssh_sign(namespace, data)

        self.authorization = (
            f'Signature KeyId="{self.login}",algorithm="ssh",signature={signature},'
            f'headers="(created)",created={created}'
        )


class ClientRequest(aiohttp.ClientRequest):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def update_auth(self, auth):
        if auth and isinstance(auth, SSHAuth):
            # We do not want to include the authorization header in
            # each request, as it would overload the server.  In fact,
            # we already have a cookie in the session that will
            # validate subsequent requests.
            if auth.already_auth:
                return
        super().update_auth(auth)


class AsyncOBS:
    """Minimal asynchronous interface for OBS"""

    def __init__(
        self, url, username, password, ssh_key=None, link="auto", verify_ssl=True
    ):
        self.url = url
        self.username = username
        self.link = link

        # The key can come from a parameter or from the config file.
        # This last one only accept "strings", as is a ConfigParser
        if ssh_key:
            ssh_key = pathlib.Path(ssh_key)

        if ssh_key and not ssh_key.exists():
            # Give a second chance in user directory
            new_ssh_key = pathlib.Path.home() / ".ssh" / ssh_key
            if not new_ssh_key.exists():
                raise Exception(f"SSH key not found: {ssh_key}")
            ssh_key = new_ssh_key

        conn = aiohttp.TCPConnector(limit=5, limit_per_host=5, verify_ssl=verify_ssl)
        if ssh_key:
            auth = SSHAuth(username, password, ssh_key)
        else:
            auth = aiohttp.BasicAuth(username, password)
        self.client = aiohttp.ClientSession(
            connector=conn, request_class=ClientRequest, auth=auth
        )

    async def close(self):
        """Close the client session"""

        # This method must be called at the end of the object
        # livecycle.  Check aiohttp documentation for details
        if self.client:
            await self.client.close()
            self.client = None

    @retry
    async def create(self, project, package=None, disabled=False):
        """Create a project and / or package"""
        if await self.authorized(project) and not await self.exists(project):
            # TODO: generate the XML via ElementTree and ET.dump(root)
            if not disabled:
                data = (
                    f'<project name="{project}"><title/><description/>'
                    f'<person userid="{self.username}" role="maintainer"/>'
                    "</project>"
                )
            else:
                data = (
                    f'<project name="{project}"><title/><description/>'
                    f'<person userid="{self.username}" role="maintainer"/>'
                    "<build><disable/></build><publish><disable/></publish>"
                    "<useforbuild><disable/></useforbuild></project>"
                )
            LOG.debug(f"Creating remote project {project} [disabled: {disabled}]")
            await self.client.put(f"{self.url}/source/{project}/_meta", data=data)

        if (
            package
            and await self.authorized(project, package)
            and not await self.exists(project, package)
        ):
            if not disabled:
                data = (
                    f'<package name="{package}" project="{project}"><title/>'
                    "<description/></package>"
                )
            else:
                data = (
                    f'<package name="{package}" project="{project}"><title/>'
                    "<description/><build><disable/></build><publish><disable/>"
                    "</publish><useforbuild><disable/></useforbuild></package>"
                )
            LOG.debug(
                f"Creating remote package {project}/{package} [disabled: {disabled}]"
            )
            await self.client.put(
                f"{self.url}/source/{project}/{package}/_meta", data=data
            )

    @retry
    async def _download(self, url_path, filename_path, **params):
        LOG.debug(f"Start download {url_path} to {filename_path}")
        async with self.client.get(f"{self.url}/{url_path}", params=params) as resp:
            with filename_path.open("wb") as f:
                while True:
                    chunk = await resp.content.read(1024 * 4)
                    if not chunk:
                        break
                    f.write(chunk)
        LOG.debug(f"End download {url_path} to {filename_path}")

    async def download(self, project, *path, filename_path, **params):
        """Download a file from a project or package"""
        url_path = "/".join(("source", project, *path))
        await self._download(url_path, filename_path, **params)

    @retry
    async def _upload(
        self, url_path, filename_path=None, data=None, headers=None, **params
    ):
        if filename_path:
            LOG.debug(f"Start upload {filename_path} to {url_path}")
            with filename_path.open("rb") as f:
                resp = await self.client.put(
                    f"{self.url}/{url_path}", data=f, headers=headers, params=params
                )
            LOG.debug(f"End upload {filename_path} to {url_path}")
        elif data is not None:
            LOG.debug(f"Start upload to {url_path}")
            resp = await self.client.put(
                f"{self.url}/{url_path}", data=data, headers=headers, params=params
            )
            LOG.debug(f"End upload to {url_path}")
        else:
            resp = None
            LOG.warning("Filename nor data provided. Nothing to upload")

        if resp and resp.status >= 400:
            raise http.client.HTTPException(f"PUT {resp.status} on {url_path}")

    async def upload(
        self, project, *path, filename_path=None, data=None, headers=None, **params
    ):
        """Upload a file to a project or package"""
        url_path = "/".join(("source", project, *path))
        await self._upload(
            url_path, filename_path=filename_path, data=data, headers=headers, **params
        )

    @retry
    async def _delete(self, url_path, **params):
        LOG.debug(f"Delete {url_path}")
        await self.client.delete(f"{self.url}/{url_path}", params=params)

    async def delete(self, project, *path, **params):
        """Delete a file, project or package"""
        url_path = "/".join(("source", project, *path))
        await self._delete(url_path, **params)

    @retry
    async def _command(self, url_path, cmd, filename_path=None, data=None, **params):
        params["cmd"] = cmd
        if filename_path:
            LOG.debug(f"Start command {cmd} {filename_path} to {url_path}")
            with filename_path.open("rb") as f:
                await self.client.post(f"{self.url}/{url_path}", data=f, params=params)
            LOG.debug(f"End command {cmd} {filename_path} to {url_path}")
        elif data:
            LOG.debug(f"Start command {cmd} to {url_path}")
            await self.client.post(f"{self.url}/{url_path}", data=data, params=params)
            LOG.debug(f"End command {cmd} to {url_path}")

    async def command(
        self, project, *path, cmd, filename_path=None, data=None, **params
    ):
        """Send a command to a project or package"""
        url_path = "/".join(("source", project, *path))
        await self._command(
            url_path, cmd, filename_path=filename_path, data=data, **params
        )

    @retry
    async def _transfer(self, url_path, to_url_path, to_obs=None, **params):
        to_obs = to_obs if to_obs else self
        LOG.debug(f"Start transfer from {url_path} to {to_url_path}")
        resp = await self.client.get(f"{self.url}/{url_path}")
        to_url = to_obs.url if to_obs else self.url
        await to_obs.client.put(
            f"{to_url}/{to_url_path}", data=resp.content, params=params
        )
        LOG.debug(f"End transfer from {url_path} to {to_url_path}")

    async def transfer(
        self,
        project,
        package,
        filename,
        to_project,
        to_package=None,
        to_filename=None,
        to_obs=None,
        **params,
    ):
        """Copy a file between (two) OBS instances"""
        to_package = to_package if to_package else package
        to_filename = to_filename if to_filename else filename
        await self._transfer(
            f"source/{project}/{package}/{filename}",
            f"source/{to_project}/{to_package}/{to_filename}",
            to_obs,
            **params,
        )

    @retry
    async def _xml(self, url_path, **params):
        LOG.debug(f"Fetching XML {url_path}")
        try:
            async with self.client.get(f"{self.url}/{url_path}", params=params) as resp:
                return ET.fromstring(await resp.read())
        except Exception:
            return ET.fromstring('<directory rev="latest"/>')

    async def packages(self, project):
        """List of packages inside an OBS project"""
        root = await self._xml(f"source/{project}")
        return [entry.get("name") for entry in root.findall(".//entry")]

    async def files_md5_revision(self, project, package):
        """List of (filename, md5) for a package, and the active revision"""
        root = await self._xml(f"/source/{project}/{package}", rev="latest")

        revision = root.get("rev")

        if root.find(".//entry[@name='_link']") is not None:
            project_link = (
                await self._xml(f"/source/{project}/{package}/_link", rev="latest")
            ).get("project")

            if project_link and project_link != project and self.link == "never":
                LOG.error(
                    f"ERROR: Link {project}/{package} pointing outside ({project_link})"
                )
                return [], None

            if (
                project_link and project_link != project and self.link == "auto"
            ) or self.link == "always":
                revision = root.find(".//linkinfo").get("xsrcmd5")
                root = await self._xml(f"/source/{project}/{package}", rev=revision)

        files_md5 = [
            (entry.get("name"), entry.get("md5")) for entry in root.findall(".//entry")
        ]

        return files_md5, revision

    async def revision(self, project, package):
        """Return the active revision of a package"""
        root = await self._xml(f"/source/{project}/{package}", rev="latest")

        revision = root.get("rev")

        if root.find(".//entry[@name='_link']") is not None:
            project_link = (
                await self._xml(f"/source/{project}/{package}/_link", rev="latest")
            ).get("project")

            if project_link and project_link != project and self.link == "never":
                LOG.error(
                    f"ERROR: Link {project}/{package} pointing outside ({project_link})"
                )
                return None

            project_link = project_link if project_link else project
            package_link = root.find(".//linkinfo").get("package")
            root = await self._xml(
                f"/source/{project_link}/{package_link}", rev="latest"
            )
            revision = root.get("rev")

        return revision

    @retry
    async def exists(self, project, package=None):
        """Check if a project or package exists in OBS"""
        url = (
            f"{self.url}/source/{project}/{package}"
            if package
            else f"{self.url}/source/{project}"
        )
        async with self.client.head(url) as resp:
            return resp.status != 404

    @retry
    async def authorized(self, project, package=None):
        """Check if the user is authorized to access the project or package"""
        url = (
            f"{self.url}/source/{project}/{package}"
            if package
            else f"{self.url}/source/{project}"
        )
        async with self.client.head(url) as resp:
            if isinstance(self.client._default_auth, SSHAuth):
                if not self.client._default_auth.authorization:
                    self.client._default_auth.set_challenge(resp.headers)
                    return await self.authorized(project, package)
            return resp.status != 401


class Git:
    """Local git repository"""

    def __init__(self, path, prefix=None):
        self.path = pathlib.Path(path)
        self.prefix = self.path / prefix if prefix else self.path
        self.first_entry = {}

    # TODO: Extend it to packages and files
    def exists(self):
        """Check if the path is a valid git repository"""
        return (self.path / ".git").exists()

    def create(self):
        """Create a local git repository"""
        self.prefix.mkdir(parents=True, exist_ok=True)
        # Convert the path to string, to avoid some limitations in
        # older pygit2
        pygit2.init_repository(str(self.path))

    async def delete(self, package, filename=None):
        """Delete a package or a file from a git repository"""
        loop = asyncio.get_running_loop()
        if filename:
            await loop.run_in_executor(None, (self.prefix / package / filename).unlink)
        else:
            await loop.run_in_executor(None, shutil.rmtree, self.prefix / package)

    def packages(self):
        """List of packages in the git repository"""
        return [
            package.parts[-1]
            for package in self.prefix.iterdir()
            if package.is_dir() and package.parts[-1] not in (".git", ".obs")
        ]

    def _md5(self, package, filename):
        md5 = hashlib.md5()
        with (self.prefix / package / filename).open("rb") as f:
            while True:
                chunk = f.read(1024 * 4)
                if not chunk:
                    break
                md5.update(chunk)
        return md5.hexdigest()

    async def files_md5(self, package):
        """List of (filename, md5) for a package"""
        # TODO: For Python >= 3.7 use get_running_loop()
        loop = asyncio.get_event_loop()
        files = [
            file_.parts[-1]
            for file_ in (self.prefix / package).iterdir()
            if file_.is_file()
        ]
        md5s = await asyncio.gather(
            *(
                loop.run_in_executor(None, self._md5, package, filename)
                for filename in files
            )
        )
        return zip(files, md5s)

    def head_hash(self):
        return pygit2.Repository(str(self.path)).head.target

    def _patches(self):
        repo = pygit2.Repository(str(self.path))
        last = repo[repo.head.target]
        for commit in repo.walk(
            last.id, pygit2.GIT_SORT_TOPOLOGICAL | pygit2.GIT_SORT_TIME
        ):
            if len(commit.parents) == 1:
                for patch in commit.tree.diff_to_tree(commit.parents[0].tree):
                    yield commit, patch
            elif len(commit.parents) == 0:
                for patch in commit.tree.diff_to_tree():
                    yield commit, patch

    def analyze_history(self):
        packages_path = {
            (self.prefix / package).relative_to(self.path)
            for package in self.packages()
        }

        for commit, patch in self._patches():
            packages = packages_path & set(
                pathlib.Path(patch.delta.new_file.path).parents
            )
            assert len(packages) <= 1
            if packages:
                package = packages.pop()
                self.first_entry.setdefault(
                    package,
                    (
                        commit.oid,
                        commit.author.name,
                        commit.author.email,
                        datetime.datetime.utcfromtimestamp(commit.commit_time),
                    ),
                )

    def last_revision_to(self, package):
        package_path = (self.prefix / package).relative_to(self.path)
        return self.first_entry.get(package_path)


class StorageOBS:
    """File storage in OBS"""

    async def __new__(cls, *args, **kwargs):
        instance = super().__new__(cls)
        await instance.__init__(*args, **kwargs)
        return instance

    async def __init__(self, obs, project, package, git):
        self.obs = obs
        self.project = project
        self.package = package
        self.git = git

        self.index = set()
        self.sync = True

        await self._update_index()

    async def _update_index(self):
        # TODO: we do not clean the index, we only add elements
        files_md5, _ = await self.obs.files_md5_revision(self.project, self.package)
        for filename, md5 in files_md5:
            assert filename == md5, f"Storage {self.project}/{self.package} not valid"
            self.index.add(filename)

    async def transfer(self, md5, project, package, filename, obs, **params):
        """Copy a file to the file storage from a remote OBS"""
        assert (
            md5 in self.index
        ), f"File {package}/{filename} ({md5}) missing from storage"
        # TODO: replace "transfer" with copy_to and copy_from.
        # TODO: when both OBS services are the same, use the copy pack
        #       / commit trick from
        #       https://github.com/openSUSE/open-build-service/issues/9615
        print(f"(StorageOBS) transfering {project}/{package}/{filename}")
        await self.obs.transfer(
            self.project, self.package, md5, project, package, filename, obs, **params
        )
        print(f"(StorageOBS) transferred {project}/{package}/{filename}")

    async def _store(self, filename_path, md5):
        """Store a file with md5 into the storage"""
        self.index.add(md5)
        self.sync = False

        print(f"(StorageOBS) storing {filename_path}")
        await self.obs.upload(
            self.project,
            self.package,
            md5,
            filename_path=filename_path,
            rev="repository",
        )
        print(f"(StorageOBS) stored {filename_path}")

    async def store_files(self, package, files_md5):
        package_path = self.git.prefix / package
        files_md5_exists = [
            (filename, md5)
            for filename, md5 in files_md5
            if (package_path / filename).exists()
        ]

        await asyncio.gather(
            *(
                self._store(package_path / filename, md5)
                for filename, md5 in files_md5_exists
            )
        )

        await asyncio.gather(
            *(self.git.delete(package, filename) for filename, _ in files_md5_exists)
        )

        with (package_path / ".obs" / "files").open("w") as f:
            f.writelines(
                f"{filename}\t\t{md5}\n" for filename, md5 in sorted(files_md5)
            )

    async def fetch(self, md5, filename_path):
        """Download a file from the storage under a different filename"""
        self.obs.download(self.project, self.package, md5, filename_path=filename_path)

    async def commit(self):
        # If the index is still valid, we do not commit a change
        if self.sync:
            return

        directory = ET.Element("directory")
        for md5 in self.index:
            entry = ET.SubElement(directory, "entry")
            entry.attrib["name"] = md5
            entry.attrib["md5"] = md5
        commit_date = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        await self.obs.command(
            self.project,
            self.package,
            cmd="commitfilelist",
            data=ET.tostring(directory),
            user=self.obs.username,
            comment=f"Storage syncronization {commit_date}",
        )
        self.sync = True


class StorageLFS:
    """File storage in git LFS"""

    def __init__(self, git):
        self.git = git
        # When using the OBS storage we can avoid some downloads, but
        # is not the case for LFS.  In this model the index will be
        # empty always.
        self.index = set()
        self.tracked = set()

        self._update_tracked()

    def _update_tracked(self):
        out = subprocess.run(
            ["git", "lfs", "track"],
            cwd=self.git.path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
        )
        for line in out.stdout.splitlines():
            if line.startswith(" " * 4):
                self.tracked.add(line.split()[0])

    async def is_installed(self):
        out = subprocess.run(
            ["git", "lfs", "install"],
            cwd=self.git.path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        is_installed = out.returncode == 0

        # Track the default extensions already, we can later include
        # specific files
        if is_installed:
            for binary in Exporter.BINARY | Exporter.NON_BINARY_EXCEPTIONS:
                await self._store(pathlib.Path(f"*{binary}"))

        return is_installed

    def overlaps(self):
        return [
            (a, b)
            for a, b in itertools.combinations(self.tracked, 2)
            if fnmatch.fnmatch(a, b)
        ]

    def transfer(self, md5, project, package, filename, obs):
        pass

    def _tracked(self, filename):
        return any(fnmatch.fnmatch(filename, track) for track in self.tracked)

    async def _store(self, filename_path):
        # When registering general patterms, like "*.gz" we do not
        # have a path relative to the git repository
        try:
            filename_path = filename_path.relative_to(self.git.path)
        except ValueError:
            pass

        # TODO: we can edit `.gitattributes` manually
        subprocess.run(
            ["git", "lfs", "track", filename_path],
            cwd=self.git.path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.tracked.add(str(filename_path))
        await self.commit()

    async def store_files(self, package, files_md5):
        package_path = self.git.prefix / package
        for filename, _ in files_md5:
            if not self._tracked(filename):
                await self._store(package_path / filename)

    async def fetch(self):
        pass

    async def delete(self, filename_path):
        subprocess.run(
            ["git", "lfs", "untrack", filename_path],
            cwd=self.git.path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    async def commit(self):
        subprocess.run(
            ["git", "add", ".gitattributes"],
            cwd=self.git.path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )


class Exporter:
    """Export projects and packages from OBS to git"""

    BINARY = {
        ".xz",
        ".gz",
        ".bz2",
        ".zip",
        ".gem",
        ".tgz",
        ".png",
        ".pdf",
        ".jar",
        ".oxt",
        ".whl",
        ".rpm",
    }
    NON_BINARY_EXCEPTIONS = {".obscpio"}
    NON_BINARY = {
        ".changes",
        ".spec",
        ".patch",
        ".diff",
        ".conf",
        ".yml",
        ".keyring",
        ".sig",
        ".sh",
        ".dif",
        ".txt",
        ".service",
        ".asc",
        ".cabal",
        ".desktop",
        ".xml",
        ".pom",
        ".SUSE",
        ".in",
        ".obsinfo",
        ".1",
        ".init",
        ".kiwi",
        ".rpmlintrc",
        ".rules",
        ".py",
        ".sysconfig",
        ".logrotate",
        ".pl",
        ".dsc",
        ".c",
        ".install",
        ".8",
        ".md",
        ".html",
        ".script",
        ".xml",
        ".test",
        ".cfg",
        ".el",
        ".pamd",
        ".sign",
        ".macros",
    }

    def __init__(
        self,
        obs,
        git,
        storage,
        skip_project_meta,
        skip_all_project_meta,
        skip_package_meta,
        skip_all_package_meta,
    ):
        self.obs = obs
        self.git = git
        self.storage = storage
        self.skip_project_meta = skip_project_meta
        self.skip_all_project_meta = skip_all_project_meta
        self.skip_package_meta = skip_package_meta
        self.skip_all_package_meta = skip_all_package_meta

    @staticmethod
    def is_binary(filename):
        """Use some heuristics to detect if a file is binary"""
        # Shortcut the detection based on the file extension
        suffix = pathlib.Path(filename).suffix
        if suffix in Exporter.BINARY or suffix in Exporter.NON_BINARY_EXCEPTIONS:
            return True
        if suffix in Exporter.NON_BINARY:
            return False

        # Small (5Kb) files are considered as text
        if filename.stat().st_size < 5 * 1024:
            return False

        # Read a chunk of the file and try to determine the encoding, if
        # the confidence is low we assume binary
        with filename.open("rb") as f:
            chunk = f.read(4 * 1024)
            try:
                chunk.decode("utf-8")
            except UnicodeDecodeError:
                encoding = chardet.detect(chunk)
            else:
                return False
        return encoding["confidence"] < 0.8

    async def project(self, project):
        """Export a project from OBS to git"""
        packages_obs = set(await self.obs.packages(project))
        packages_git = set(self.git.packages())
        packages_delete = packages_git - packages_obs

        if not ((self.git.path / ".obs").exists() and self.skip_all_project_meta):
            await self.project_metadata(project)

        await asyncio.gather(
            *(self.package(project, package) for package in packages_obs),
            *(self.git.delete(package) for package in packages_delete),
        )

        await self.storage.commit()

    async def project_metadata(self, project):
        """Export the project metadata from OBS to git"""
        metadata_path = self.git.path / ".obs"
        metadata_path.mkdir(exist_ok=True)

        metadata = [
            "_project",
            "_attribute",
            "_config",
            "_pattern",
        ]
        if not self.skip_project_meta:
            metadata.append("_meta")

        await asyncio.gather(
            *(
                self.obs.download(project, meta, filename_path=metadata_path / meta)
                for meta in metadata
            )
        )

    async def package(self, project, package):
        """Export a package from OBS to git"""
        package_path = self.git.prefix / package
        package_path.mkdir(exist_ok=True)

        print(f"{project}/{package} ...")

        if not (
            (self.git.prefix / package / ".obs").exists() and self.skip_all_package_meta
        ):
            await self.package_metadata(project, package)

        # We do not know, before downloading, if a file is binary or
        # text.  The strategy for now is to download all the files
        # (except the ones already in the remote storage or in git),
        # and upload later the ones that are binary.  We need to
        # remove those after that

        files_md5_obs, revision = await self.obs.files_md5_revision(project, package)
        files_md5_obs = set(files_md5_obs)
        files_md5_git = set(await self.git.files_md5(package))

        # TODO: one optimization is to detect the files that are
        # stored in the local "files" cache, that we already know that
        # are binary, and do a transfer if the MD5 is different
        files_download = {
            filename
            for filename, md5 in (files_md5_obs - files_md5_git)
            if md5 not in self.storage.index
        }

        files_obs = {filename for filename, _ in files_md5_obs}
        files_git = {filename for filename, _ in files_md5_git}
        files_delete = files_git - files_obs

        await asyncio.gather(
            *(
                self.obs.download(
                    project,
                    package,
                    filename,
                    filename_path=package_path / filename,
                    rev=revision,
                )
                for filename in files_download
            ),
            *(self.git.delete(package, filename) for filename in files_delete),
        )

        # TODO: do not over-optimize here, and detect old binary files
        # Once we download the full package, we store the new binary files
        files_md5_store = [
            (filename, md5)
            for filename, md5 in files_md5_obs
            if filename in files_download
            and Exporter.is_binary(package_path / filename)
        ]
        files_md5_obs_store = [
            (filename, md5)
            for filename, md5 in files_md5_obs
            if md5 in self.storage.index
        ]
        await self.storage.store_files(package, files_md5_store + files_md5_obs_store)

    async def package_metadata(self, project, package):
        metadata_path = self.git.prefix / package / ".obs"
        metadata_path.mkdir(exist_ok=True)

        metadata = [
            "_attribute",
            # "_history",
        ]
        if not self.skip_package_meta:
            metadata.append("_meta")

        await asyncio.gather(
            *(
                self.obs.download(
                    project, package, meta, filename_path=metadata_path / meta
                )
                for meta in metadata
            )
        )

    async def export_revisions(self, project, revisions_csv):
        """Export the packages revision numbers from OBS to git"""
        packages_obs = await self.obs.packages(project)

        revisions = await asyncio.gather(
            *(self.obs.revision(project, package) for package in packages_obs)
        )

        with revisions_csv.open("w") as f:
            writer = csv.writer(f)
            writer.writerows(zip(packages_obs, revisions))


class Importer:
    def __init__(
        self,
        obs,
        git,
        storage,
        remove_role_project_meta,
        skip_project_meta,
        skip_all_project_meta,
        remove_role_package_meta,
        skip_package_meta,
        skip_all_package_meta,
        skip_changes_commit_hash,
    ):
        self.obs = obs
        self.git = git
        self.storage = storage
        self.remove_role_project_meta = remove_role_project_meta
        self.skip_project_meta = skip_project_meta
        self.skip_all_project_meta = skip_all_project_meta
        self.remove_role_package_meta = remove_role_package_meta
        self.skip_package_meta = skip_package_meta
        self.skip_all_package_meta = skip_all_package_meta
        self.skip_changes_commit_hash = skip_changes_commit_hash

        self._revisions = {}

    @functools.lru_cache()
    def project_name(self):
        metadata_path = self.git.path / ".obs" / "_meta"
        return ET.parse(metadata_path).getroot().get("name")

    def adjust_metadata(
        self, filename_path, project, project_name=None, remove_role=False
    ):
        # Replace the package name
        project_name = project_name if project_name else self.project_name()
        with filename_path.open() as f:
            meta = f.read().replace(project_name, project)

        if remove_role:
            meta = re.sub("<person .*?>", "", meta)
            meta = re.sub("<group .*?>", "", meta)
        return meta

    @functools.lru_cache()
    def changes_git_entry(self, package):
        last_revision = self.git.last_revision_to(package)

        if not last_revision:
            LOG.error(f"ERROR: {package} not found in git history")
            return ""

        commit_hash, author, email, commit_date = self.git.last_revision_to(package)
        entry = "-" * 67
        commit_date = commit_date.strftime("%a %b %d %H:%M:%S UTC %Y")
        entry = f"{entry}\n{commit_date} - {author} <{email}>"
        entry = f"{entry}\n\n- Last git synchronization: {commit_hash}\n\n"
        return entry

    def prepend_changes(self, filename_path, package):
        with filename_path.open("rb") as f:
            changes = f.read()
            if not self.skip_changes_commit_hash:
                changes = self.changes_git_entry(package).encode("utf-8") + changes
            return changes

    def load_revisions(self, revisions_csv):
        try:
            with revisions_csv.open() as f:
                reader = csv.reader(f)
                self._revisions = dict(reader)
        except Exception:
            LOG.error(f"ERROR: {revisions_csv} not found or not valid")

    def adjust_release(self, filename_path, package):
        with filename_path.open("rb") as f:
            spec = f.read()
            revision = self._revisions.get(package)
            if revision:
                spec = re.sub(
                    rb"Release\s*:\s*(?:0|<RELEASE>)",
                    f"Release: {revision}".encode("utf-8"),
                    spec,
                )
            return spec

    async def project(self, project):
        # TODO: What if the project in OBS is more modern? Is there a
        # way to detect it?

        # First import the project metadata, as a side effect can
        # create the project
        if not (await self.obs.exists(project) and self.skip_all_project_meta):
            await self.project_metadata(project)

        packages_obs = set(await self.obs.packages(project))
        packages_git = set(self.git.packages())
        packages_delete = packages_obs - packages_git

        # Order the packages, uploading the links the last
        packages_git = sorted(
            packages_git, key=lambda x: (self.git.prefix / x / "_link").exists()
        )

        # To avoid stressing OBS / IBS we group the imports
        # TODO: decide if fully serialize the fetch
        group_size = 4
        packages_git = list(packages_git)
        packages_git_groups = [
            packages_git[i : i + group_size]
            for i in range(0, len(packages_git), group_size)
        ]
        for packages_git_group in packages_git_groups:
            await asyncio.gather(
                *(self.package(project, package) for package in packages_git_group)
            )

        await asyncio.gather(
            *(self.obs.delete(project, package) for package in packages_delete)
        )

    async def project_metadata(self, project):
        metadata_path = self.git.path / ".obs"

        # When creating a new project, we should add first the _meta
        # file, and later the rest
        if not self.skip_project_meta:
            await self.obs.upload(
                project,
                "_meta",
                data=self.adjust_metadata(
                    metadata_path / "_meta",
                    project,
                    remove_role=self.remove_role_project_meta,
                ),
            )

        metadata = [
            # "_project",
            # "_attribute",
            "_config",
            # "_pattern",
        ]

        await asyncio.gather(
            *(
                self.obs.upload(
                    project,
                    meta,
                    data=self.adjust_metadata(
                        metadata_path / meta,
                        project,
                        remove_role=self.remove_role_project_meta,
                    ),
                )
                for meta in metadata
            )
        )

    async def _git_files_md5(self, package):
        files_md5 = []
        for filename, md5 in await self.git.files_md5(package):
            filename_path = self.git.prefix / package / filename
            if filename_path.suffix == ".changes":
                md5 = hashlib.md5()
                md5.update(self.prepend_changes(filename_path, package))
                md5 = md5.hexdigest()
            elif filename_path.suffix == ".spec":
                md5 = hashlib.md5()
                md5.update(self.adjust_release(filename_path, package))
                md5 = md5.hexdigest()
            files_md5.append((filename, md5))
        return files_md5

    async def package(self, project, package):
        print(f"{project}/{package} ...")

        if not (await self.obs.exists(project, package) and self.skip_all_package_meta):
            await self.package_metadata(project, package)

        package_path = self.git.prefix / package

        files_md5_obs, _ = await self.obs.files_md5_revision(project, package)
        files_md5_obs = set(files_md5_obs)
        files_md5_git = set(await self._git_files_md5(package))

        # TODO: reading the files is part of StorageXXX class
        meta_file = package_path / ".obs" / "files"
        if meta_file.exists():
            with (meta_file).open() as f:
                files_md5_git_store = {tuple(line.split()) for line in f.readlines()}
        else:
            files_md5_git_store = set()

        files_md5_upload = files_md5_git - files_md5_obs
        files_md5_transfer = files_md5_git_store - files_md5_obs

        files_obs = {filename for filename, _ in files_md5_obs}
        files_git = {filename for filename, _ in files_md5_git}
        files_git_store = {filename for filename, _ in files_md5_git_store}
        files_delete = files_obs - files_git - files_git_store

        await asyncio.gather(
            *(
                self.obs.upload(
                    project,
                    package,
                    filename,
                    filename_path=package_path / filename,
                    rev="repository",
                )
                for filename, _ in files_md5_upload
                if not filename.endswith((".changes", ".spec", ".json"))
            ),
            *(
                self.obs.upload(
                    project,
                    package,
                    filename,
                    data=self.prepend_changes(package_path / filename, package),
                    rev="repository",
                )
                for filename, _ in files_md5_upload
                if filename.endswith(".changes")
            ),
            *(
                self.obs.upload(
                    project,
                    package,
                    filename,
                    data=self.adjust_release(package_path / filename, package),
                    rev="repository",
                )
                for filename, _ in files_md5_upload
                if filename.endswith(".spec")
            ),
            *(
                self.obs.upload(
                    project,
                    package,
                    filename,
                    filename_path=package_path / filename,
                    headers={"content-type": "text/xml"},
                    rev="repository",
                )
                for filename, _ in files_md5_upload
                if filename.endswith(".json")
            ),
            *(
                self.storage.transfer(
                    md5, project, package, filename, self.obs, rev="repository"
                )
                for filename, md5 in files_md5_transfer
            ),
            *(
                self.obs.delete(project, package, filename, rev="repository")
                for filename in files_delete
            ),
        )

        if files_md5_upload or files_md5_transfer or files_delete:
            # Create the directory XML to generate a commit
            directory = ET.Element("directory")
            for filename, md5 in files_md5_git | files_md5_git_store:
                entry = ET.SubElement(directory, "entry")
                entry.attrib["name"] = filename
                entry.attrib["md5"] = md5

            head_hash = self.git.head_hash()

            await self.obs.command(
                project,
                package,
                cmd="commitfilelist",
                data=ET.tostring(directory),
                user=self.obs.username,
                comment=f"Import {head_hash}",
            )

    async def package_metadata(self, project, package):
        metadata_path = self.git.prefix / package / ".obs"
        metadata = (
            "_meta",
            # "_attribute",
            # "_history",
        )

        # Validate that the metadata can be re-allocated
        project_name = self.project_name()
        package_project_name = (
            ET.parse(metadata_path / "_meta").getroot().get("project")
        )
        if project_name != package_project_name:
            LOG.warning(f"Please, edit the metadata for {package}")

        await asyncio.gather(
            *(
                self.obs.upload(
                    project,
                    package,
                    meta,
                    data=self.adjust_metadata(
                        metadata_path / meta,
                        project,
                        package_project_name,
                        remove_role=self.remove_role_package_meta,
                    ),
                )
                for meta in metadata
            )
        )


def read_config(config_filename):
    """Read or create a configuration file in INI format"""
    if not config_filename:
        print("Configuration file not provided")
        sys.exit(-1)

    if not config_filename.exists():
        print(f"Configuration file {config_filename} not found.")
        print("Use create_config to create a new configuration file")
        sys.exit(-1)

    config = configparser.ConfigParser()
    config.read(config_filename)

    # Old configuration files do not have the new ssh-key parameter.
    # Provide a default value.
    for section in ("export", "import"):
        if "ssh-key" not in config[section]:
            config[section]["ssh-key"] = ""
    if config["storage"]["type"] == "obs" and "ssh-key" not in config["storage"]:
        config["storage"]["ssh-key"] = ""

    return config


def create_config(args):
    if not args.config:
        print("Configuration file not provided")
        sys.exit(-1)

    config = configparser.ConfigParser()

    config["export"] = {
        "url": args.api,
        "username": args.username,
        "password": args.password if args.password else "<password>",
        "ssh-key": args.ssh_key if args.ssh_key else "<ssh-key-path>",
        "link": args.link,
    }

    config["import"] = {
        "url": args.api,
        "username": args.username,
        "password": args.password if args.password else "<password>",
        "ssh-key": args.ssh_key if args.ssh_key else "<ssh-key-path>",
    }

    if args.storage == "obs":
        config["storage"] = {
            "type": "obs",
            "url": args.api,
            "username": args.username,
            "password": args.password if args.password else "<password>",
            "ssh-key": args.ssh_key if args.ssh_key else "<ssh-key-path>",
            "storage": f"home:{args.username}:storage/files",
        }
    elif args.storage == "lfs":
        config["storage"] = {
            "type": "lfs",
        }
    else:
        print(f"Storage type {args.storage} not valid")
        sys.exit(-1)

    config["git"] = {"prefix": args.prefix}

    with args.config.open("w") as f:
        config.write(f)

    # Only the user can read and write the file
    args.config.chmod(stat.S_IRUSR | stat.S_IWUSR)

    print(f"Edit {args.config} to adjust the configuration and passwords")

    return config


async def export(args, config):
    project = args.project
    repository = pathlib.Path(args.repository).expanduser().absolute().resolve()
    package = args.package

    obs = AsyncOBS(
        config["export"]["url"],
        config["export"]["username"],
        config["export"]["password"],
        config["export"]["ssh-key"],
        config["export"]["link"],
        verify_ssl=not args.disable_verify_ssl,
    )

    if not await obs.authorized(project, package):
        print("No authorization to access project or package in build service")
        sys.exit(-1)

    if not await obs.exists(project, package):
        print("Project or package not found in build service")
        sys.exit(-1)

    git = Git(repository, config["git"]["prefix"])
    git.create()
    print("Initialized the git repository")

    storage_type = config["storage"]["type"]
    if storage_type == "obs":
        storage_obs = AsyncOBS(
            config["storage"]["url"],
            config["storage"]["username"],
            config["storage"]["password"],
            config["storage"]["ssh-key"],
            verify_ssl=not args.disable_verify_ssl,
        )
        storage_project, storage_package = pathlib.Path(
            config["storage"]["storage"]
        ).parts
        await storage_obs.create(storage_project, storage_package, disabled=True)
        print("Remote storage in OBS initialized")

        storage = await StorageOBS(storage_obs, storage_project, storage_package, git)
    elif storage_type == "lfs":
        storage = StorageLFS(git)

        if not await storage.is_installed():
            print("LFS extension not installed")
            await obs.close()
            sys.exit(-1)
        print("Git LFS extension enabled in the repository")

        overlaps = storage.overlaps()
        if overlaps:
            print("Multiple LFS tracks are overlaped. Fix them manually.")
            for a, b in overlaps:
                print(f"* {a} - {b}")
    else:
        raise NotImplementedError(f"Storage {storage_type} not implemented")

    exporter = Exporter(
        obs,
        git,
        storage,
        args.skip_project_meta,
        args.skip_all_project_meta,
        args.skip_package_meta,
        args.skip_all_package_meta,
    )
    if args.only_export_revisions:
        await exporter.export_revisions(project, args.only_export_revisions)
    else:
        if package:
            # To have a self consisten unit, maybe we need to export
            # also the project metadata
            if not ((git.path / ".obs").exists() or args.skip_all_project_meta):
                await exporter.project_metadata(project)
            await exporter.package(project, package)
        else:
            await exporter.project(project)

    if storage_type == "obs":
        await storage_obs.close()
    await obs.close()


async def import_(args, config):
    repository = pathlib.Path(args.repository).expanduser().absolute().resolve()
    project = args.project
    package = args.package

    obs = AsyncOBS(
        config["import"]["url"],
        config["import"]["username"],
        config["import"]["password"],
        config["import"]["ssh-key"],
        config["export"]["link"],
        verify_ssl=not args.disable_verify_ssl,
    )

    if not await obs.authorized(project, package):
        print("No authorization to access project or package in build service")
        sys.exit(-1)

    git = Git(repository, config["git"]["prefix"])
    if not git.exists():
        print("Local git repository is not valid")
        sys.exit(-1)
    git.analyze_history()

    storage_type = config["storage"]["type"]
    if storage_type == "obs":
        storage_obs = AsyncOBS(
            config["storage"]["url"],
            config["storage"]["username"],
            config["storage"]["password"],
            config["storage"]["ssh-key"],
            verify_ssl=not args.disable_verify_ssl,
        )
        storage_project, storage_package = pathlib.Path(
            config["storage"]["storage"]
        ).parts

        if not await storage_obs.authorized(storage_project, storage_package):
            print("No authorization to access the file storage in build service")
            sys.exit(-1)

        if not await storage_obs.exists(storage_project, storage_package):
            print("File storage not found in build service")
            sys.exit(-1)

        storage = await StorageOBS(storage_obs, storage_project, storage_package, git)
    elif storage_type == "lfs":
        storage = StorageLFS(git)

        if not await storage.is_installed():
            print("LFS extension not installed")
            sys.exit(-1)
        print("Git LFS extension enabled in the repository")
    else:
        raise NotImplementedError(f"Storage {storage_type} not implemented")

    importer = Importer(
        obs,
        git,
        storage,
        args.remove_role_project_meta,
        args.skip_project_meta,
        args.skip_all_project_meta,
        args.remove_role_package_meta,
        args.skip_package_meta,
        args.skip_all_package_meta,
        args.skip_changes_commit_hash,
    )

    if args.adjust_release:
        importer.load_revisions(args.adjust_release)

    if package:
        # If the project is not present, maybe we want to create it
        if not (await obs.exists(project) or args.skip_all_project_meta):
            await importer.project_metadata(project)
        await importer.package(project, package)
    else:
        await importer.project(project)

    if storage_type == "obs":
        await storage_obs.close()
    await obs.close()


def main():
    parser = argparse.ArgumentParser(description="OBS-git simple bridge tool")
    parser.add_argument(
        "--config",
        "-c",
        type=pathlib.Path,
        default=pathlib.Path("~", ".obsgit").expanduser(),
        help="configuration file",
    )
    parser.add_argument(
        "--level",
        "-l",
        help="logging level",
    )
    parser.add_argument(
        "--disable-verify-ssl",
        action="store_true",
        help="disable SSL verification",
    )

    subparser = parser.add_subparsers()

    parser_create_config = subparser.add_parser(
        "create-config", help="create default config file"
    )
    parser_create_config.add_argument(
        "--api",
        "-a",
        default="https://api.opensuse.org",
        help="url for the api",
    )
    parser_create_config.add_argument(
        "--username",
        "-u",
        default=getpass.getuser(),
        help="username for login",
    )
    parser_create_config.add_argument(
        "--password",
        "-p",
        help="password for login or SSH key passphrase",
    )
    parser_create_config.add_argument(
        "--ssh-key",
        "-k",
        type=pathlib.Path,
        help="SSH key file for login",
    )
    parser_create_config.add_argument(
        "--link",
        "-l",
        choices=["never", "always", "auto"],
        default="never",
        help="expand package links",
    )
    parser_create_config.add_argument(
        "--storage",
        "-s",
        choices=["obs", "lfs"],
        default="lfs",
        help="type of storage for large files",
    )
    parser_create_config.add_argument(
        "--prefix",
        default="packages",
        help="git directory where all the packages will be stored",
    )
    parser_create_config.set_defaults(func=create_config)

    parser_export = subparser.add_parser("export", help="export between OBS and git")
    parser_export.add_argument("project", help="OBS project name")
    parser_export.add_argument(
        "repository", nargs="?", default=".", help="git repository directory"
    )
    parser_export.add_argument("--package", "-p", help="OBS package name")
    parser_export.add_argument(
        "--skip-project-meta",
        action="store_true",
        help="skip update project _meta",
    )
    parser_export.add_argument(
        "--skip-all-project-meta",
        action="store_true",
        help="skip update all project metadata",
    )
    parser_export.add_argument(
        "--skip-package-meta",
        action="store_true",
        help="skip update package _meta",
    )
    parser_export.add_argument(
        "--skip-all-package-meta",
        action="store_true",
        help="skip update all package metadata",
    )
    parser_export.add_argument(
        "--only-export-revisions",
        type=pathlib.Path,
        metavar="REVISION.CSV",
        help="only export the revision numbers from OBS",
    )
    parser_export.set_defaults(func=export)

    parser_import = subparser.add_parser("import", help="import between git and OBS")
    parser_import.add_argument(
        "repository", nargs="?", default=".", help="git repository directory"
    )
    parser_import.add_argument("project", help="OBS project name")
    parser_import.add_argument("--package", "-p", help="OBS package name")
    parser_import.add_argument(
        "--remove-role-project-meta",
        action="store_true",
        help="remove <person> and <group> from project _meta",
    )
    parser_import.add_argument(
        "--skip-project-meta",
        action="store_true",
        help="skip update project _meta",
    )
    parser_import.add_argument(
        "--skip-all-project-meta",
        action="store_true",
        help="skip update all project metadata",
    )
    parser_import.add_argument(
        "--remove-role-package-meta",
        action="store_true",
        help="remove <person> and <group> from package _meta",
    )
    parser_import.add_argument(
        "--skip-package-meta",
        action="store_true",
        help="skip update package _meta",
    )
    parser_import.add_argument(
        "--skip-all-package-meta",
        action="store_true",
        help="skip update all package metadata",
    )
    parser_import.add_argument(
        "--adjust-release",
        type=pathlib.Path,
        metavar="REVISION.CSV",
        help="adjust the release based on the revision history",
    )
    parser_import.add_argument(
        "--skip-changes-commit-hash",
        action="store_true",
        help="do not prepend .changes files with latest git commit hash",
    )

    parser_import.set_defaults(func=import_)

    args = parser.parse_args()

    if args.level:
        numeric_level = getattr(logging, args.level.upper(), None)
        if not isinstance(numeric_level, int):
            print(f"Invalid log level: {args.level}")
            sys.exit(-1)
        logging.basicConfig(level=numeric_level)

    if "func" not in args:
        parser.print_help()
        sys.exit(-1)

    if args.func == create_config:
        args.func(args)
    else:
        config = read_config(args.config)
        # TODO: For Python >= 3.7 use get_running_loop()
        loop = asyncio.get_event_loop()
        loop.run_until_complete(args.func(args, config))
        loop.close()


if __name__ == "__main__":
    main()
